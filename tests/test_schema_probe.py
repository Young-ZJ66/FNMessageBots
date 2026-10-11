"""schema 自检的回归测试。

飞牛内部库没有公开 schema，列名与真机不符时查询会**静默返回空数据**（表现为「不推送」），
所以自检必须把「实际列」和「缺失列」都打到日志里。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
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

from monitor import schema_probe  # noqa: E402
from monitor.schema_probe import (  # noqa: E402
    DB_SCHEMAS,
    probe,
    probe_database,
    probe_media_delete,
    reset_schema_probe_cache,
    table_columns,
    table_exists,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


class CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def text(self, level: int | None = None) -> str:
        rows = [r for r in self.records if level is None or r.levelno == level]
        # LogRecord.getMessage() 已完成 % 格式化，不要再套一次 args
        return "\n".join(f"{r.levelname}: {r.getMessage()}" for r in rows)


class ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_schema_probe_cache()
        self.root = _TMP_ROOT / f"probe_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.root / "t.db3"))
        self.conn.row_factory = sqlite3.Row
        self.logger = logging.getLogger(f"test.schema.{uuid.uuid4().hex[:6]}")
        self.logger.setLevel(logging.DEBUG)
        self.capture = CaptureLogs()
        self.logger.addHandler(self.capture)
        self.logger.propagate = False

    def tearDown(self) -> None:
        self.logger.removeHandler(self.capture)
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)
        reset_schema_probe_cache()


class TestTableIntrospection(ProbeCase):
    def test_table_columns_with_row_factory(self):
        self.conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        self.assertEqual(table_columns(self.conn, "t"), {"a", "b"})

    def test_table_columns_without_row_factory(self):
        conn = sqlite3.connect(str(self.root / "plain.db3"))
        conn.execute("CREATE TABLE t (x INTEGER, y TEXT, z TEXT)")
        try:
            self.assertEqual(table_columns(conn, "t"), {"x", "y", "z"})
        finally:
            conn.close()

    def test_missing_table_yields_empty(self):
        self.assertEqual(table_columns(self.conn, "nope"), set())
        self.assertFalse(table_exists(self.conn, "nope"))

    def test_table_exists_true(self):
        self.conn.execute("CREATE TABLE yes (a)")
        self.assertTrue(table_exists(self.conn, "yes"))


class TestProbeDatabase(ProbeCase):
    def test_logs_actual_columns(self):
        self.conn.execute("CREATE TABLE log (id INTEGER, eventId TEXT, parameter TEXT, extra TEXT)")
        probe_database(
            self.conn,
            "测试库",
            {"log": ("id", "eventId", "parameter")},
            logger=self.logger,
            db_path="/tmp/x.db3",
        )
        info = self.capture.text(logging.INFO)
        self.assertIn("schema 自检", info)
        self.assertIn("eventId", info)
        self.assertIn("extra", info, "实际列应全部打印，便于与代码依赖列对照")
        self.assertEqual(self.capture.text(logging.WARNING), "", "列都在时不应有告警")

    def test_missing_column_warns(self):
        self.conn.execute("CREATE TABLE log (id INTEGER, eventId TEXT)")
        probe_database(
            self.conn, "测试库", {"log": ("id", "eventId", "logtime")}, logger=self.logger
        )
        warn = self.capture.text(logging.WARNING)
        self.assertIn("logtime", warn)
        self.assertIn("缺少列", warn)
        self.assertIn("静默不推送", warn)

    def test_missing_table_warns(self):
        probe_database(self.conn, "测试库", {"operations": ("id",)}, logger=self.logger)
        warn = self.capture.text(logging.WARNING)
        self.assertIn("operations", warn)
        self.assertIn("缺少表", warn)

    def test_probe_runs_once_per_db(self):
        self.conn.execute("CREATE TABLE log (id INTEGER)")
        probe_database(self.conn, "测试库", {"log": ("id",)}, logger=self.logger, db_path="/tmp/y.db3")
        first = len(self.capture.records)
        self.assertGreater(first, 0)
        again = probe_database(
            self.conn, "测试库", {"log": ("id",)}, logger=self.logger, db_path="/tmp/y.db3"
        )
        self.assertEqual(again, {}, "重复调用应直接返回")
        self.assertEqual(len(self.capture.records), first, "不应重复打日志")

    def test_different_db_path_probes_again(self):
        self.conn.execute("CREATE TABLE log (id INTEGER)")
        probe_database(self.conn, "测试库", {"log": ("id",)}, logger=self.logger, db_path="/a.db3")
        n = len(self.capture.records)
        probe_database(self.conn, "测试库", {"log": ("id",)}, logger=self.logger, db_path="/b.db3")
        self.assertGreater(len(self.capture.records), n, "换库后应重新自检")


class TestMediaDeleteProbe(ProbeCase):
    def test_no_table_is_info_only(self):
        probe_media_delete(self.conn, self.logger)
        self.assertIn("不存在", self.capture.text(logging.INFO))
        self.assertEqual(self.capture.text(logging.WARNING), "")

    def test_time_column_variants_accepted(self):
        # 列名变体逐条静态建表（避免动态拼接 SQL）
        variants = (
            ("update_time", "update_time.db3"),
            ("delete_time", "delete_time.db3"),
            ("create_time", "create_time.db3"),
            ("time", "time.db3"),
        )
        for col, db_name in variants:
            with self.subTest(column=col):
                reset_schema_probe_cache()
                self.capture.records.clear()
                conn = sqlite3.connect(str(self.root / db_name))
                try:
                    if col == "update_time":
                        conn.execute("CREATE TABLE media_delete (guid TEXT, update_time INTEGER)")
                    elif col == "delete_time":
                        conn.execute("CREATE TABLE media_delete (guid TEXT, delete_time INTEGER)")
                    elif col == "create_time":
                        conn.execute("CREATE TABLE media_delete (guid TEXT, create_time INTEGER)")
                    else:
                        conn.execute("CREATE TABLE media_delete (guid TEXT, time INTEGER)")
                    cols = probe_media_delete(conn, self.logger)
                    self.assertIn(col, cols)
                    self.assertEqual(self.capture.text(logging.WARNING), "", f"{col} 应被接受")
                finally:
                    conn.close()

    def test_no_time_column_warns(self):
        self.conn.execute("CREATE TABLE media_delete (guid TEXT, path TEXT)")
        probe_media_delete(self.conn, self.logger)
        warn = self.capture.text(logging.WARNING)
        self.assertIn("时间列", warn)
        self.assertIn("跳过", warn)


class TestSchemaRegistry(unittest.TestCase):
    """registry 完整性守卫：每个库都必须登记表与非空依赖列。"""

    def test_registry_well_formed(self):
        self.assertTrue(DB_SCHEMAS, "registry 不应为空")
        for key, (label, tables) in DB_SCHEMAS.items():
            with self.subTest(schema=key):
                self.assertTrue(label)
                self.assertTrue(tables)
                for table, cols in tables.items():
                    self.assertTrue(cols, f"{key}.{table} 依赖列不应为空")

    def test_expected_keys_present(self):
        for key in ("logger", "trim_media", "photo", "backup", "scheduler", "trim_activity"):
            self.assertIn(key, DB_SCHEMAS)

    def test_probe_unknown_key_is_noop(self):
        reset_schema_probe_cache()
        self.assertEqual(probe(None, "no_such_key", logging.getLogger("x")), {})

    def test_probe_uses_registry(self):
        reset_schema_probe_cache()
        conn = sqlite3.connect(":memory:")
        logger = logging.getLogger("test.registry")
        logger.setLevel(logging.INFO)
        cap = CaptureLogs()
        logger.addHandler(cap)
        logger.propagate = False
        try:
            conn.execute("CREATE TABLE user_token (token TEXT, user_guid TEXT)")
            probe(conn, "trim_activity", logger, "/x.db3")
            self.assertIn("user_token", cap.text(logging.INFO))
            # device 等未建的列应告警
            self.assertIn("缺少列", cap.text(logging.WARNING))
        finally:
            logger.removeHandler(cap)
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
