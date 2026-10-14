"""时间显示口径回归测试。

运行方式（项目根目录）：
    python tests/test_timezone_unification.py

验证各轮询器的展示时间统一由 DISPLAY_TIMEZONE 决定（默认 Asia/Shanghai），
不再出现「同一批通知里不同数据源时间互差数小时」。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
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

from utils.logtime_display import (  # noqa: E402
    DEFAULT_DISPLAY_TIMEZONE,
    format_ts_for_display,
    get_display_timezone,
    now_for_display,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
_TS = 1750000000  # 2025-06-15 15:06:40 UTC / 23:06:40 +08:00
_TS_UTC_STR = "2025-06-15 15:06:40"
_TS_CST_STR = "2025-06-15 23:06:40"


class TestDisplayTimezoneHelper(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("DISPLAY_TIMEZONE", None)

    def test_default_is_asia_shanghai(self):
        os.environ.pop("DISPLAY_TIMEZONE", None)
        self.assertEqual(DEFAULT_DISPLAY_TIMEZONE, "Asia/Shanghai")
        self.assertEqual(str(get_display_timezone()), "Asia/Shanghai")

    def test_env_override(self):
        os.environ["DISPLAY_TIMEZONE"] = "UTC"
        self.assertEqual(str(get_display_timezone()), "UTC")

    def test_invalid_timezone_falls_back_to_local(self):
        os.environ["DISPLAY_TIMEZONE"] = "Not/AZone"
        self.assertIsNone(get_display_timezone())

    def test_format_is_second_precision(self):
        os.environ["DISPLAY_TIMEZONE"] = "Asia/Shanghai"
        out = format_ts_for_display(_TS)
        self.assertEqual(out, _TS_CST_STR)

    def test_format_never_raises_on_extreme_values(self):
        for bad in (0, -1, 10**18, float("inf"), float("nan")):
            with self.subTest(value=bad):
                self.assertIsInstance(format_ts_for_display(bad), str)

    def test_now_for_display_shape(self):
        out = now_for_display()
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestPollerConsistency(unittest.TestCase):
    """各轮询器的展示时间应与 logtime_display 保持一致（子进程验证，避免模块级缓存）。"""

    def _run_in_subprocess(self, tz: str, body: str) -> str:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["DISPLAY_TIMEZONE"] = tz
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_ROOT), str(_ROOT / "src"), str(_SUPPORT)]
        )
        code = textwrap.dedent(
            f"""
            import stub_deps; stub_deps.install()
            {body}
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            # 子进程会打印中文（初始化码提示、鉴权关闭提示等）。不指定编码时
            # Windows 默认用 GBK 解码 UTF-8 输出，会 UnicodeDecodeError 或产生乱码。
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(_ROOT),
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(f"subprocess failed: {proc.stderr}")
        return proc.stdout.strip()

    def test_all_pollers_agree_under_utc(self):
        body = """
            from monitor.backup_db_poller import _ts_to_str as backup_ts
            from monitor.db_log_poller import _logtime_to_datetime as db_ts
            from monitor.media_db_poller import _ms_to_str as media_ts
            from monitor.photo_db_poller import _sec_to_local_str as photo_ts
            from monitor.docker_events_poller import _parse_event_timestamp as docker_ts
            from monitor.ssh_journal_poller import SshJournalPoller as S

            ts = 1750000000
            outs = {
                "backup": backup_ts(ts),
                "db_log": db_ts(ts - 28800),      # 库内为 +8h 存值，偏移默认 28800
                "media": media_ts(ts * 1000),
                "photo": photo_ts(ts),
                "docker": docker_ts({"time": ts}),
                "ssh": S._ts_from_entry({"__REALTIME_TIMESTAMP": str(ts * 1_000_000)}),
            }
            print("|".join(sorted(set(outs.values()))))
            print("; ".join(f"{k}={v}" for k, v in sorted(outs.items())))
        """
        out = self._run_in_subprocess("UTC", body).splitlines()
        distinct = out[0].split("|")
        detail = out[1] if len(out) > 1 else ""
        self.assertEqual(len(distinct), 1, f"各轮询器展示时间不一致: {detail}")
        self.assertEqual(distinct[0], _TS_UTC_STR, f"UTC 下应为该时间: {detail}")

    def test_all_pollers_agree_under_asia_shanghai(self):
        body = """
            from monitor.backup_db_poller import _ts_to_str as backup_ts
            from monitor.db_log_poller import _logtime_to_datetime as db_ts
            from monitor.media_db_poller import _ms_to_str as media_ts
            from monitor.photo_db_poller import _sec_to_local_str as photo_ts
            from monitor.docker_events_poller import _parse_event_timestamp as docker_ts
            from monitor.ssh_journal_poller import SshJournalPoller as S

            ts = 1750000000
            outs = {
                "backup": backup_ts(ts),
                "db_log": db_ts(ts - 28800),
                "media": media_ts(ts * 1000),
                "photo": photo_ts(ts),
                "docker": docker_ts({"time": ts}),
                "ssh": S._ts_from_entry({"__REALTIME_TIMESTAMP": str(ts * 1_000_000)}),
            }
            print("|".join(sorted(set(outs.values()))))
            print("; ".join(f"{k}={v}" for k, v in sorted(outs.items())))
        """
        out = self._run_in_subprocess("Asia/Shanghai", body).splitlines()
        distinct = out[0].split("|")
        detail = out[1] if len(out) > 1 else ""
        self.assertEqual(len(distinct), 1, f"各轮询器展示时间不一致: {detail}")
        self.assertEqual(distinct[0], _TS_CST_STR, f"东八区下应为该时间: {detail}")

    def test_photo_share_expiry_labels_unchanged(self):
        body = """
            from monitor.photo_db_poller import _sec_to_local_str as photo_ts
            print("label=" + photo_ts(0))
            print("none=" + repr(photo_ts(None)))
        """
        out = self._run_in_subprocess("Asia/Shanghai", body).splitlines()
        self.assertEqual(out[0], "label=永久有效", "永久有效分享的既有文案不应改变")
        self.assertEqual(out[1], "none=''")


class TestPushHistoryDayKey(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"tz_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop("DISPLAY_TIMEZONE", None)

    def test_today_key_follows_display_timezone(self):
        import utils.push_stats as push_stats

        os.environ["DISPLAY_TIMEZONE"] = "UTC"
        push_stats.init(str(self.root))
        push_stats.record(True)
        stats = push_stats.get_stats_path()
        import json

        data = json.loads(Path(stats).read_text(encoding="utf-8"))
        from datetime import datetime, timezone

        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn(today_utc, data["daily"], "当日统计键应跟随 DISPLAY_TIMEZONE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
