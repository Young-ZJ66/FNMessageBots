"""SSH journal 采集链路的可用性判定与游标对齐。

背景（真机事故）：容器未挂载 /var/log/journal、/run/log/journal、/etc/machine-id 时，
容器内的 journalctl 读不到任何 journal 文件，**但退出码仍是 0**（仅 stderr 输出
"No journal files were found."）。早期实现只看退出码，于是：

  1. main.py 依据 is_available() 把 db_log_poller.skip_ssh_events 置为 True，
     库表里的 Sshd* 事件被丢弃；
  2. 本轮询器又拿不到游标，一条都不推。

两条路径互相抵消 → SSH 事件完全静默丢失，日志里只留下一条看起来无害的 INFO。
这里的测试锁住正确的行为：
  - 读不到 journal 必须判定为「不可用」，让库表兜底
  - `-n 0 --show-cursor` 拿不到游标时回退 `-n 1 --show-cursor`
  - 「取不到游标」的告警只打一次，不刷屏
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import unittest
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

from monitor import ssh_journal_poller as mod  # noqa: E402
from monitor.ssh_journal_poller import SshJournalPoller  # noqa: E402

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

CURSOR = "s=0011223344556677;i=1a2b;b=aabbccdd;m=ffeedd;t=1234;x=5678"
SHOW_CURSOR_LINE = f"-- cursor: {CURSOR}"
NO_JOURNAL_ERR = "No journal files were found."
NO_ENTRIES = "-- No entries --"

SSH_EVENTS = ["SSH_LOGIN_SUCCESS", "SSH_AUTH_FAILED", "SSH_INVALID_USER", "SSH_DISCONNECTED"]


class FakeJournalctl:
    """按命令行参数返回预设输出，并记录调用。

    注意：这里把**实例**赋给 `poller._run_journalctl`，因此 `__call__` 的第一个参数
    就是实例自身（不会被当作被替换方法那样再额外传 self）。
    """

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.default = (0, NO_ENTRIES + "\n", "")

    def __call__(self, extra, with_units=True):
        self.calls.append((tuple(extra), with_units))
        if with_units:
            return self.responses.get(tuple(extra), self.default)
        # 不带 -u 的调用（可用性探测）用单独的键空间
        key = ("<no-units>",) + tuple(extra)
        return self.responses.get(key, self.default)

    def args_seen(self) -> list[tuple[str, ...]]:
        return [c[0] for c in self.calls]


class ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"sshprobe_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = self.captured.append  # type: ignore[method-assign]
        self.logger = logging.getLogger(f"test.ssh.{uuid.uuid4().hex[:6]}")
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self._orig_which = mod.journalctl_available
        mod.journalctl_available = lambda: True
        self.addCleanup(lambda: setattr(mod, "journalctl_available", self._orig_which))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self) -> SshJournalPoller:
        p = SshJournalPoller(
            cursor_dir=str(self.root / "cursor"),
            poll_interval=1,
            monitor_events=SSH_EVENTS,
        )
        p.logger = self.logger
        return p

    def messages(self, level: int | None = None) -> str:
        rows = [r for r in self.captured if level is None or r.levelno == level]
        return "\n".join(r.getMessage() for r in rows)

    def install_fake(self, responses) -> FakeJournalctl:
        fake = FakeJournalctl(responses)
        self.poller._run_journalctl = fake  # type: ignore[method-assign]
        return fake


class TestAvailabilityProbe(ProbeCase):
    def test_unreadable_journal_is_unavailable(self):
        """读到 0 条且报 No journal files were found → 必须判定不可用（让库表兜底）。"""
        self.poller = self.make()
        self.install_fake({("<no-units>", "-n", "1", "--show-cursor"): (0, NO_ENTRIES + "\n", NO_JOURNAL_ERR)})
        self.assertFalse(self.poller.is_available(), "读不到 journal 时不得判为可用")
        msg = self.messages(logging.WARNING)
        self.assertIn("读不到宿主机的 journal", msg)
        self.assertIn("/etc/machine-id", msg, "应指明缺哪个挂载")
        self.assertIn("兜底", msg, "应说明已回退到库表路径")

    def test_readable_journal_is_available(self):
        self.poller = self.make()
        self.install_fake(
            {("<no-units>", "-n", "1", "--show-cursor"): (0, f"some line\n{SHOW_CURSOR_LINE}\n", "")}
        )
        self.assertTrue(self.poller.is_available())
        self.assertEqual(self.messages(logging.WARNING), "")

    def test_nonzero_exit_is_unavailable(self):
        self.poller = self.make()
        self.install_fake({("<no-units>", "-n", "1", "--show-cursor"): (1, "", "permission denied")})
        self.assertFalse(self.poller.is_available())
        self.assertIn("permission denied", self.messages(logging.WARNING))

    def test_missing_journalctl_is_unavailable(self):
        self.poller = self.make()
        mod.journalctl_available = lambda: False
        self.assertFalse(self.poller.is_available())
        self.assertIn("未找到 journalctl", self.messages(logging.WARNING))

    def test_probe_does_not_filter_by_unit(self):
        """可用性判据必须是不带 -u 的读取：带 -u 时「该 unit 无条目」会被误判为不可读。"""
        self.poller = self.make()
        fake = self.install_fake(
            {("<no-units>", "-n", "1", "--show-cursor"): (0, SHOW_CURSOR_LINE + "\n", "")}
        )
        self.assertTrue(self.poller.is_available())
        self.assertEqual(fake.calls, [(("-n", "1", "--show-cursor"), False)])

    def test_probe_result_is_cached(self):
        self.poller = self.make()
        fake = self.install_fake(
            {("<no-units>", "-n", "1", "--show-cursor"): (0, SHOW_CURSOR_LINE + "\n", "")}
        )
        self.assertTrue(self.poller.is_available())
        self.assertTrue(self.poller.is_available())
        self.assertEqual(len(fake.calls), 1, "不应重复探测")


class TestAlignCursor(ProbeCase):
    def test_falls_back_to_n1_when_n0_has_no_cursor(self):
        """部分 systemd 版本下 `-n 0 --show-cursor` 不输出 cursor 行，必须回退 -n 1。"""
        self.poller = self.make()
        self.install_fake(
            {
                ("-n", "0", "--show-cursor"): (0, NO_ENTRIES + "\n", ""),
                ("-n", "1", "--show-cursor"): (0, f"entry\n{SHOW_CURSOR_LINE}\n", ""),
            }
        )
        state = {"version": 1, "cursor": "", "aligned": False}
        self.poller._align_cursor(state)
        self.assertEqual(state["cursor"], CURSOR, "应回退 -n 1 取到游标")
        self.assertTrue(state["aligned"])
        self.assertIn(CURSOR, self.poller._state_file.read_text(encoding="utf-8"), "游标应落盘")
        self.assertEqual(self.messages(logging.WARNING), "", "成功时不应告警")

    def test_n0_success_does_not_try_n1(self):
        self.poller = self.make()
        fake = self.install_fake({("-n", "0", "--show-cursor"): (0, SHOW_CURSOR_LINE + "\n", "")})
        state = {"version": 1, "cursor": "", "aligned": False}
        self.poller._align_cursor(state)
        self.assertEqual(state["cursor"], CURSOR)
        self.assertEqual(fake.args_seen(), [("-n", "0", "--show-cursor")], "不应有多余调用")

    def test_both_attempts_fail_warns_once(self):
        self.poller = self.make()
        self.install_fake({})  # 两次都返回 NO_ENTRIES
        state = {"version": 1, "cursor": "", "aligned": False}
        self.poller._align_cursor(state)
        self.poller._align_cursor(state)
        self.poller._align_cursor(state)
        self.assertEqual(state["cursor"], "", "拿不到游标时不应写入假值")
        self.assertTrue(state["aligned"])
        self.assertTrue(state.get("since"), "空 journal 应记录时间起点，等待新日志")
        warn = self.messages(logging.WARNING)
        self.assertEqual(warn.count("SSH journal 暂无游标"), 1, f"告警应只打一次，实际：{warn}")

    def test_warning_includes_journalctl_output(self):
        """失败时必须留下 journalctl 的实际输出，否则无从排查。"""
        self.poller = self.make()
        self.install_fake(
            {
                ("-n", "0", "--show-cursor"): (0, NO_ENTRIES + "\n", NO_JOURNAL_ERR),
                ("-n", "1", "--show-cursor"): (0, NO_ENTRIES + "\n", NO_JOURNAL_ERR),
            }
        )
        state = {"version": 1, "cursor": "", "aligned": False}
        self.poller._align_cursor(state)
        warn = self.messages(logging.WARNING)
        self.assertIn("No journal files were found", warn)
        self.assertIn("-n 0", warn)
        self.assertIn("-n 1", warn)

    def test_nonzero_exit_does_not_mark_aligned(self):
        self.poller = self.make()
        self.install_fake({("-n", "0", "--show-cursor"): (1, "", "boom")})
        state = {"version": 1, "cursor": "", "aligned": False}
        self.poller._align_cursor(state)
        self.assertFalse(state["aligned"], "读取失败不应标记为已对齐")
        self.assertEqual(state["cursor"], "")
        self.assertIn("对齐失败", self.messages(logging.WARNING))

    def test_state_persisted_between_instances(self):
        self.poller = self.make()
        self.install_fake({("-n", "0", "--show-cursor"): (0, SHOW_CURSOR_LINE + "\n", "")})
        state = {"version": 1, "cursor": "", "aligned": False}
        self.poller._align_cursor(state)
        again = self.make()
        self.assertEqual(again._load_state().get("cursor"), CURSOR, "游标应能在重启后读回")


class TestNoUnitFilterLeak(ProbeCase):
    def test_fetch_entries_still_uses_units(self):
        """增量读取必须带 -u 过滤，否则会把整机 journal 当 SSH 事件解析。"""
        self.poller = self.make()
        fake = self.install_fake(
            {("-o", "json", "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
              "--after-cursor", CURSOR, "--show-cursor"): (0, "", "")}
        )
        self.poller._fetch_entries(CURSOR)
        self.assertTrue(fake.calls)
        self.assertTrue(all(with_units for _, with_units in fake.calls), "增量读取应带 -u 过滤")

    def test_empty_cursor_does_not_spawn_journalctl(self):
        self.poller = self.make()
        fake = self.install_fake({})
        entries, cur = self.poller._fetch_entries("")
        self.assertEqual((entries, cur), ([], ""))
        self.assertEqual(fake.calls, [], "无游标时不得去扫历史")


class TestJournalResetDetection(ProbeCase):
    """飞牛的 journal 常是 volatile（/run/log/journal，tmpfs），宿主机一重启就清空。

    此时旧游标指向一个不存在的 boot，`--after-cursor` 会反复失败导致链路再次卡死，
    因此必须能识别「journal 已被重置」并重新对齐。
    """

    def test_align_records_boot_id(self):
        self.poller = self.make()
        self.poller._current_boot_id = lambda: "boot-A"  # type: ignore[assignment]
        self.install_fake({("-n", "0", "--show-cursor"): (0, SHOW_CURSOR_LINE + "\n", "")})
        state = {"version": 1, "cursor": "", "aligned": False, "boot_id": ""}
        self.poller._align_cursor(state)
        self.assertEqual(state["boot_id"], "boot-A", "对齐时应记下 boot id")
        self.assertEqual(json.loads(self.poller._state_file.read_text(encoding="utf-8"))["boot_id"], "boot-A")

    def test_boot_change_forces_realign(self):
        self.poller = self.make()
        self.install_fake({("-n", "0", "--show-cursor"): (0, SHOW_CURSOR_LINE + "\n", "")})
        self.poller._current_boot_id = lambda: "boot-B"  # type: ignore[assignment]
        state = {"version": 1, "cursor": "s=old;i=1;b=deadbeef", "aligned": True, "boot_id": "boot-A"}
        self.poller._poll_once(state)
        self.assertEqual(state["cursor"], CURSOR, "boot 变化后应重新对齐到新游标")
        self.assertEqual(state["boot_id"], "boot-B")
        self.assertIn("已重置", self.messages(logging.INFO))

    def test_same_boot_keeps_reading_incrementally(self):
        """boot 未变时必须继续走增量，不得每次重新对齐（否则会漏事件）。"""
        self.poller = self.make()
        fake = self.install_fake(
            {("-o", "json", "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
              "--after-cursor", CURSOR, "--show-cursor"): (0, "", "")}
        )
        self.poller._current_boot_id = lambda: "boot-A"  # type: ignore[assignment]
        state = {"version": 1, "cursor": CURSOR, "aligned": True, "boot_id": "boot-A"}
        self.poller._poll_once(state)
        self.assertTrue(fake.calls)
        self.assertTrue(all(with_units for _, with_units in fake.calls), "应走带 -u 的增量读取")
        self.assertFalse(
            any(a[:1] == ("-n",) for a in fake.args_seen()), "不该重新对齐"
        )

    def test_unknown_boot_id_does_not_trigger_realign(self):
        """读不到 boot id（返回空串）时保守处理：不因无法判断就重新对齐。"""
        self.poller = self.make()
        fake = self.install_fake(
            {("-o", "json", "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
              "--after-cursor", CURSOR, "--show-cursor"): (0, "", "")}
        )
        self.poller._current_boot_id = lambda: ""  # type: ignore[assignment]
        state = {"version": 1, "cursor": CURSOR, "aligned": True, "boot_id": "boot-A"}
        self.poller._poll_once(state)
        self.assertTrue(all(with_units for _, with_units in fake.calls))

    def test_legacy_state_without_boot_id(self):
        """旧版本状态文件没有 boot_id 字段，不应因此重新对齐（升级不能丢事件）。"""
        self.poller = self.make()
        fake = self.install_fake(
            {("-o", "json", "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
              "--after-cursor", CURSOR, "--show-cursor"): (0, "", "")}
        )
        self.poller._current_boot_id = lambda: "boot-A"  # type: ignore[assignment]
        state = {"version": 1, "cursor": CURSOR, "aligned": True}  # 无 boot_id
        self.poller._poll_once(state)
        self.assertTrue(fake.calls)
        self.assertTrue(all(with_units for _, with_units in fake.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
