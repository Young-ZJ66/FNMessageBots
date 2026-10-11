"""DBLogPoller 游标推进 / 脏行容忍 的回归测试。

运行方式（项目根目录）：
    python tests/test_db_log_poller.py
    # 或一次跑完全部：
    python -m unittest discover -s tests -p "test_*.py"

只依赖 Python 标准库，直接加载 src 下的真实模块，不修改任何生产文件。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest
import uuid
from unittest import mock
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from monitor.db_log_poller import DBLogPoller, _as_int, _row_to_entry  # noqa: E402
import monitor.db_log_poller as db_poller_module  # noqa: E402

# 测试工作目录放在仓库内（不依赖系统临时目录权限），每个用例一个子目录
_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

SCHEMA = """
CREATE TABLE log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serviceId TEXT,
    uid TEXT,
    uname TEXT,
    logtime INTEGER,
    loglevel TEXT,
    eventId TEXT,
    parameter TEXT,
    category TEXT
)
"""


def make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert(conn: sqlite3.Connection, event_id: str, *, uid="1000", uname="alice",
           logtime=1700000000, loglevel="6", parameter="{}") -> int:
    cur = conn.execute(
        "INSERT INTO log (serviceId, uid, uname, logtime, loglevel, eventId, parameter, category)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("eventlogger", uid, uname, logtime, loglevel, event_id, parameter, "file"),
    )
    conn.commit()
    return int(cur.lastrowid)


class TempDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "logger_data.db3"
        self.cursor_dir = self.root / "cursor"
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self.conn = make_db(self.db_path)
        self.delivered = []

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)

    def build_poller(self, events=("LoginSucc", "Logout"), **kwargs) -> DBLogPoller:
        p = DBLogPoller(
            db_path=str(self.db_path),
            cursor_dir=str(self.cursor_dir),
            poll_interval=1,
            monitor_events=list(events),
            **kwargs,
        )
        for ev in p.monitor_events:
            p.add_handler(ev, lambda ed, entry, ev=ev: self.delivered.append((ev, ed, entry)))
        return p

    def read_cursor(self) -> str:
        f = self.cursor_dir / "db_poller_cursor.txt"
        return f.read_text().strip() if f.exists() else ""


class TestSafeInt(unittest.TestCase):
    def test_numeric_and_passthrough(self):
        self.assertEqual(_as_int(12), 12)
        self.assertEqual(_as_int("12"), 12)
        self.assertEqual(_as_int("12.0"), 12)
        self.assertEqual(_as_int("  7 "), 7)

    def test_never_raises(self):
        for bad in (None, "", "abc", "1e3x", "uid-3", {}, [], object(), True, False):
            with self.subTest(value=repr(bad)):
                self.assertEqual(_as_int(bad), 0)
                self.assertEqual(_as_int(bad, 9), 9)

    def test_row_to_entry_tolerates_dirty_values(self):
        entry = _row_to_entry({
            "id": 5, "serviceId": "eventlogger", "uid": "not-a-number",
            "uname": "bob", "logtime": 1700000000, "loglevel": "warn",
            "eventId": "LoginSucc", "parameter": '{"user":"bob"}',
        })
        self.assertEqual(entry.priority, 0)
        self.assertEqual(entry.pid, 0)
        self.assertEqual(entry.cursor, "5")


class TestDirtyRowsDoNotStall(TempDbCase):
    """核心回归：脏行不得阻塞游标推进、不得影响其后的日志推送。"""

    def test_dirty_row_is_skipped_and_cursor_advances(self):
        poller = self.build_poller()
        # 启动对齐：既有记录不补推
        insert(self.conn, "LoginSucc")
        self.assertEqual(poller._align_cursor_on_start(), 1)

        # 一条脏行（uid/loglevel 非数字）+ 一条正常行
        dirty_id = insert(self.conn, "LoginSucc", uid="abc", logtime="not-a-ts", loglevel="warn")
        good_id = insert(self.conn, "LoginSucc")

        new_cursor = poller._poll_once(1)

        self.assertEqual(new_cursor, good_id, "游标必须推进到本轮最大 id")
        self.assertEqual(self.read_cursor(), str(good_id))
        # 两条都能投递：脏行只是 loglevel/uid 字段容错为 0，不因此丢弃事件
        self.assertEqual([ev for ev, _, _ in self.delivered], ["LoginSucc", "LoginSucc"])
        delivered_entry = self.delivered[0][2]
        self.assertEqual(delivered_entry.pid, 0, "非数字 uid 应回落为 0 而不是抛异常")
        self.assertEqual(delivered_entry.priority, 0, "非数字 loglevel 应回落为 0")
        self.assertGreater(dirty_id, 1)

    def test_row_conversion_error_skips_row_and_advances_cursor(self):
        """单行构造异常（例如某事件数据畸形）不得卡住游标，后续日志仍要推送。"""
        poller = self.build_poller(events=("LoginSucc",))
        insert(self.conn, "LoginSucc")
        poller._align_cursor_on_start()
        insert(self.conn, "LoginSucc")   # id=2，构造时抛错
        insert(self.conn, "LoginSucc")   # id=3，构造时抛错
        last = insert(self.conn, "LoginSucc")  # id=4，正常

        real_row_to_entry = db_poller_module._row_to_entry

        def flaky_row_to_entry(row):
            if int(row.get("id") or 0) in (2, 3):
                raise ValueError("模拟畸形行")
            return real_row_to_entry(row)

        with mock.patch.object(db_poller_module, "_row_to_entry", flaky_row_to_entry):
            new_cursor = poller._poll_once(1)

        self.assertEqual(new_cursor, last, "脏行之后仍要继续推进并投递")
        self.assertEqual([ev for ev, _, _ in self.delivered], ["LoginSucc"])
        self.assertEqual(poller._poll_once(new_cursor), last, "已跳过的行不应重复处理")

    def test_unknown_event_and_unmonitored_event_are_ignored_without_stall(self):
        poller = self.build_poller(events=("LoginSucc",))
        insert(self.conn, "LoginSucc")
        poller._align_cursor_on_start()
        insert(self.conn, "SomeUnknownEventId")
        insert(self.conn, "Logout")  # 未监控
        last = insert(self.conn, "LoginSucc")

        self.assertEqual(poller._poll_once(1), last)
        self.assertEqual([ev for ev, _, _ in self.delivered], ["LoginSucc"])

    def test_empty_event_id_row_does_not_stall(self):
        poller = self.build_poller(events=("LoginSucc",))
        insert(self.conn, "LoginSucc")
        poller._align_cursor_on_start()
        blank = self.conn.execute(
            "INSERT INTO log (serviceId, uid, uname, logtime, loglevel, eventId, parameter, category)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("eventlogger", "1000", "alice", 1700000000, "6", "", "{}", "file"),
        ).lastrowid
        self.conn.commit()
        last = insert(self.conn, "LoginSucc")

        self.assertEqual(poller._poll_once(1), last)
        self.assertGreaterEqual(blank, 2)


class TestFetchLimitChunking(TempDbCase):
    def test_large_backlog_is_read_in_chunks(self):
        poller = self.build_poller(events=("LoginSucc",), max_id_attempts=1)
        poller.FETCH_LIMIT = 2
        insert(self.conn, "LoginSucc")
        poller._align_cursor_on_start()

        ids = [insert(self.conn, "LoginSucc") for _ in range(5)]
        cursor = 1
        rounds = 0
        while rounds < 10 and len(self.delivered) < 5:
            cursor = poller._poll_once(cursor)
            rounds += 1

        self.assertEqual(len(self.delivered), 5)
        self.assertEqual(cursor, ids[-1])
        self.assertGreaterEqual(rounds, 3, "5 行 / 每轮 2 行 至少要 3 轮")

    def test_fetch_new_rows_respects_limit(self):
        poller = self.build_poller(events=("LoginSucc",))
        poller.FETCH_LIMIT = 3
        for _ in range(7):
            insert(self.conn, "LoginSucc")
        rows = poller._fetch_new_rows(0)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])


class TestStartupAlignmentFailure(TempDbCase):
    """MAX(id) 读取失败时不得退回 0（否则重推整库），而是拒绝启动并返回 None。"""

    def test_unreadable_db_returns_none_and_keeps_no_cursor(self):
        poller = self.build_poller(events=("LoginSucc",), max_id_attempts=1)
        poller.db_path = str(self.root / "missing.db3")

        self.assertIsNone(poller._align_cursor_on_start())
        self.assertEqual(self.read_cursor(), "", "失败时不应写入游标文件")

    def test_readable_db_returns_max_id(self):
        poller = self.build_poller(events=("LoginSucc",))
        for _ in range(4):
            insert(self.conn, "LoginSucc")
        self.assertEqual(poller._align_cursor_on_start(), 4)
        self.assertEqual(self.read_cursor(), "4")

    def test_retry_after_temporary_failure(self):
        """第一次读取失败、第二次成功时，应拿到真实 max id（不退回 0）。"""
        poller = self.build_poller(events=("LoginSucc",), max_id_attempts=2)
        insert(self.conn, "LoginSucc")
        insert(self.conn, "LoginSucc")

        import monitor.sqlite_uri as sqlite_uri
        real_connect = sqlite_uri.connect_readonly_with_fallback
        calls = {"n": 0}

        def flaky_connect(db_path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(db_path, *args, **kwargs)

        with mock.patch.object(db_poller_module, "connect_readonly_with_fallback", flaky_connect):
            result = poller._align_cursor_on_start()

        self.assertEqual(calls["n"], 2, "第一次失败后必须重试")
        self.assertEqual(result, 2, "重试成功后应返回真实 max id")
        self.assertEqual(self.read_cursor(), "2")

    def test_all_attempts_failing_returns_none(self):
        poller = self.build_poller(events=("LoginSucc",), max_id_attempts=2)
        insert(self.conn, "LoginSucc")

        import monitor.sqlite_uri as sqlite_uri

        def always_fail(db_path, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(db_poller_module, "connect_readonly_with_fallback", always_fail):
            result = poller._align_cursor_on_start()

        self.assertIsNone(result)
        self.assertEqual(self.read_cursor(), "", "对齐失败时不得写入游标（避免从 0 重推整库）")


class TestResumeFromCursor(TempDbCase):
    """可选行为：启动时沿用已保存游标，补推停机期间的记录（默认关闭）。"""

    def test_default_aligns_to_latest_and_skips_backlog(self):
        poller = self.build_poller(events=("LoginSucc",))
        for _ in range(3):
            insert(self.conn, "LoginSucc")
        # 预置一个旧游标，默认模式下不应被采用
        (self.cursor_dir / "db_poller_cursor.txt").write_text("1")

        self.assertEqual(poller._align_cursor_on_start(), 3)
        self.assertEqual(self.read_cursor(), "3")
        self.assertEqual(poller._poll_once(3), 3)
        self.assertEqual(self.delivered, [], "默认模式不补推历史")

    def test_resume_uses_saved_cursor_and_backfills(self):
        poller = self.build_poller(events=("LoginSucc",), resume_from_cursor_on_start=True)
        for _ in range(3):
            insert(self.conn, "LoginSucc")
        (self.cursor_dir / "db_poller_cursor.txt").write_text("1")

        start = poller._align_cursor_on_start()

        self.assertEqual(start, 1, "应沿用已保存游标")
        self.assertEqual(poller._poll_once(start), 3)
        self.assertEqual(len(self.delivered), 2, "应补推停机期间新增的两条")

    def test_resume_without_saved_cursor_falls_back_to_latest(self):
        poller = self.build_poller(events=("LoginSucc",), resume_from_cursor_on_start=True)
        for _ in range(2):
            insert(self.conn, "LoginSucc")

        self.assertEqual(poller._align_cursor_on_start(), 2)
        self.assertEqual(self.read_cursor(), "2")

    def test_resume_with_stale_cursor_beyond_db_resets(self):
        """库被替换/回滚导致旧游标大于当前最大 id 时，应对齐当前水位而不是卡死。"""
        poller = self.build_poller(events=("LoginSucc",), resume_from_cursor_on_start=True)
        insert(self.conn, "LoginSucc")
        (self.cursor_dir / "db_poller_cursor.txt").write_text("9999")

        self.assertEqual(poller._align_cursor_on_start(), 1)
        self.assertEqual(self.read_cursor(), "1")

    def test_resume_still_refuses_when_max_id_unreadable(self):
        poller = self.build_poller(
            events=("LoginSucc",), max_id_attempts=1, resume_from_cursor_on_start=True
        )
        insert(self.conn, "LoginSucc")
        (self.cursor_dir / "db_poller_cursor.txt").write_text("1")
        poller.db_path = str(self.root / "missing.db3")

        self.assertIsNone(poller._align_cursor_on_start())
        self.assertEqual(self.read_cursor(), "1", "失败时不应改动已有游标")


class TestCursorSemanticsPreserved(TempDbCase):
    """保持原有语义：仅处理新记录、同一批失败重试、成功后不重复投递。"""

    def test_no_backfill_of_existing_rows(self):
        poller = self.build_poller(events=("LoginSucc",))
        for _ in range(3):
            insert(self.conn, "LoginSucc")
        cursor = poller._align_cursor_on_start()
        self.assertEqual(cursor, 3)
        self.assertEqual(poller._poll_once(cursor), 3)
        self.assertEqual(self.delivered, [])

    def test_batch_failure_retries_then_drops(self):
        poller = self.build_poller(events=("LoginSucc",))
        insert(self.conn, "LoginSucc")
        poller._align_cursor_on_start()
        insert(self.conn, "LoginSucc")

        def boom(_items):
            raise RuntimeError("push failed")

        poller.set_batch_handler(boom)
        cursor = 1
        for _ in range(poller.BATCH_MAX_ATTEMPTS):
            cursor = poller._poll_once(cursor)
        self.assertGreater(cursor, 1, "达到重试上限后必须推进游标，避免无限重发")

    def test_successful_batch_delivered_once(self):
        poller = self.build_poller(events=("LoginSucc",))
        insert(self.conn, "LoginSucc")
        poller._align_cursor_on_start()
        insert(self.conn, "LoginSucc")
        insert(self.conn, "LoginSucc")
        poller.set_batch_handler(lambda items: self.delivered.extend(
            (it["event_type"], it["event_data"], it["entry"]) for it in items
        ))

        cursor = poller._poll_once(1)
        self.assertEqual(cursor, 3)
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(poller._poll_once(cursor), 3)
        self.assertEqual(len(self.delivered), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
