"""状态/游标文件编码回归测试。

背景：这些文件写入时用 `ensure_ascii=False`（内容含中文），若未显式指定 encoding，
在 Windows / 非 UTF-8 locale 下会写成 GBK，下次读取抛 UnicodeDecodeError，
导致游标与水位状态整体失效（漏推或重推）。本测试锁死「按 UTF-8 写、按 UTF-8 读」。
"""

from __future__ import annotations

import json
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

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
CHINESE = "中文键-影视库·设备"


class StateEncodingCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"enc_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def assert_written_as_utf8(self, path: Path) -> None:
        """文件必须能按 UTF-8 解码，且中文未被转义。"""
        self.assertTrue(path.exists(), f"{path} 未生成")
        raw = path.read_bytes()
        text = raw.decode("utf-8")  # 非 UTF-8 会在此抛错
        self.assertIn(CHINESE, text, "中文不应被转义（ensure_ascii=False + UTF-8）")


class TestPollersStateEncoding(StateEncodingCase):
    def test_media_state(self):
        from monitor.media_db_poller import MediaDBPoller

        p = MediaDBPoller(
            db_path=str(self.root / "trimmedia.db"),
            cursor_dir=str(self.root / "c"),
            poll_interval=1,
            monitor_events=["TRIM_RESOURCE_ADDED"],
        )
        p._save_state({"version": 1, "initialized": True, "note": CHINESE})
        self.assert_written_as_utf8(p._state_file)
        self.assertEqual(p._load_state()["note"], CHINESE)

    def test_photo_state(self):
        from monitor.photo_db_poller import PhotoDBPoller

        p = PhotoDBPoller(
            db_path=str(self.root / "photo.db"),
            cursor_dir=str(self.root / "c"),
            poll_interval=1,
            monitor_events=["PHOTO_SHARE_CREATED"],
        )
        p._save_state({"note": CHINESE})
        self.assert_written_as_utf8(p._state_file)
        self.assertEqual(p._load_state()["note"], CHINESE)

    def test_trim_activity_state(self):
        from monitor.trim_activity_poller import TrimActivityPoller

        p = TrimActivityPoller(
            db_path=str(self.root / "trimactivity.db"),
            cursor_dir=str(self.root / "c"),
            poll_interval=1,
            monitor_events=["MEDIA_LOGIN_SUCC"],
        )
        p._save_state({"note": CHINESE})
        self.assert_written_as_utf8(p._state_file)
        self.assertEqual(p._load_state()["note"], CHINESE)

    def test_backup_cursor(self):
        from monitor.backup_db_poller import BackupDBPoller

        p = BackupDBPoller(
            db_path=str(self.root / "basic_backup.db3"),
            cursor_dir=str(self.root / "c"),
            poll_interval=1,
            monitor_events=["BACKUP_TASK_SUCCESS"],
        )
        payload = {"last_finished_time": 123, "note": CHINESE}
        p._cursor_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assert_written_as_utf8(p._cursor_file)
        # 轮询器自身的读取路径必须能读回
        self.assertEqual(json.loads(p._cursor_file.read_text(encoding="utf-8"))["note"], CHINESE)

    def test_scheduler_cursor(self):
        from monitor.scheduler_db_poller import SchedulerDBPoller

        p = SchedulerDBPoller(
            db_path=str(self.root / "scheduler.db"),
            cursor_dir=str(self.root / "c"),
            poll_interval=1,
            monitor_events=["SCHEDULER_TASK_SUCCESS"],
        )
        p._cursor_file.write_text(json.dumps({"note": CHINESE}, ensure_ascii=False), encoding="utf-8")
        self.assert_written_as_utf8(p._cursor_file)

    def test_docker_since_file(self):
        from monitor.docker_events_poller import DockerEventsPoller

        cursor_dir = self.root / "c"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        p = DockerEventsPoller(
            socket_path=str(self.root / "docker.sock"),
            cursor_dir=str(cursor_dir),
            monitor_events=["DOCKER_CONTAINER_START"],
        )
        # 该文件由轮询器自己读写：写入包含中文的 payload 校验编码
        p._since_file.write_text(
            json.dumps({"last_event_time": 1750000000, "note": CHINESE}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assert_written_as_utf8(p._since_file)
        self.assertEqual(p._load_since_cursor(), 1750000000)
        self.assert_written_as_utf8(p._since_file)

    def test_wan_ip_state(self):
        from monitor.wan_ip_monitor import _load_state, _save_state

        path = self.root / "wan_ip_monitor_state.json"
        _save_state(path, {"last_wan_ip": "1.2.3.4", "note": CHINESE})
        self.assert_written_as_utf8(path)
        self.assertEqual(_load_state(path)["note"], CHINESE)

    def test_ssh_state(self):
        from monitor.ssh_journal_poller import SshJournalPoller

        p = SshJournalPoller(
            cursor_dir=str(self.root / "c"),
            poll_interval=1,
            monitor_events=["SSH_LOGIN_SUCCESS"],
        )
        p._state_file.write_text(
            json.dumps({"cursor": "c1", "note": CHINESE}, ensure_ascii=False), encoding="utf-8"
        )
        self.assert_written_as_utf8(p._state_file)


class TestNoImplicitEncodingLeft(StateEncodingCase):
    """源码级守卫：状态/游标文件读写不得省略 encoding。"""

    def test_all_state_io_specifies_encoding(self):
        import inspect

        from monitor import (
            backup_db_poller,
            db_log_poller,
            docker_events_poller,
            media_db_poller,
            photo_db_poller,
            scheduler_db_poller,
            ssh_journal_poller,
            trim_activity_poller,
            wan_ip_monitor,
        )

        offenders = []
        for mod in (
            backup_db_poller,
            db_log_poller,
            docker_events_poller,
            media_db_poller,
            photo_db_poller,
            scheduler_db_poller,
            ssh_journal_poller,
            trim_activity_poller,
            wan_ip_monitor,
        ):
            src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
            for lineno, line in enumerate(src.splitlines(), 1):
                if (".read_text(" in line or ".write_text(" in line) and "encoding=" not in line:
                    offenders.append(f"{Path(inspect.getfile(mod)).name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "以下文件 IO 未指定 encoding：\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
