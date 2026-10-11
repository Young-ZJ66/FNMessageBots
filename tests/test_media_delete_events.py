"""影视库轮询器回归测试：media_delete 删除事件、schema 自适应、旧有事件不受影响。

运行方式（项目根目录）：
    python tests/test_media_delete_events.py
"""

from __future__ import annotations

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

from monitor.media_db_poller import (  # noqa: E402
    TRIM_RESOURCE_ADDED,
    TRIM_RESOURCE_DELETED,
    TRIM_SCRAPE_SUCCESS,
    MediaDBPoller,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

ITEM_TABLE = """
CREATE TABLE item (
    guid TEXT PRIMARY KEY, type TEXT, title TEXT, path TEXT, parent_guid TEXT,
    season_number INTEGER, episode_number INTEGER, tmdb_id TEXT, fetch_status INTEGER,
    create_time INTEGER, update_time INTEGER, overview TEXT,
    release_date TEXT, first_air_date TEXT, air_date TEXT, runtime INTEGER
)
"""

ITEM_MEDIA_TABLE = """
CREATE TABLE item_media (
    guid TEXT PRIMARY KEY, item_guid TEXT, path TEXT, size INTEGER,
    create_time INTEGER, update_time INTEGER
)
"""


def media_delete_table(columns: str) -> str:
    return f"CREATE TABLE media_delete ({columns})"


class MediaCase(unittest.TestCase):
    delete_columns = "guid TEXT, path TEXT, title TEXT, update_time INTEGER"

    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "trimmedia.db"
        self.cursor_dir = self.root / "cursor"
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(ITEM_TABLE)
        self.conn.executescript(ITEM_MEDIA_TABLE)
        if self.delete_columns is not None:
            self.conn.executescript(media_delete_table(self.delete_columns))
        self.conn.commit()
        self.delivered = []

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self, events=(TRIM_RESOURCE_ADDED, TRIM_SCRAPE_SUCCESS, TRIM_RESOURCE_DELETED)):
        p = MediaDBPoller(
            db_path=str(self.db_path),
            cursor_dir=str(self.cursor_dir),
            poll_interval=1,
            monitor_events=list(events),
        )
        for ev in p.monitor_events:
            p.add_handler(ev, lambda ed, entry, ev=ev: self.delivered.append((ev, ed)))
        self.poller = p
        return p

    def baseline(self, poller) -> None:
        state = poller._load_state()
        conn = poller._connect()
        try:
            poller._baseline(conn, state)
        finally:
            conn.close()
        poller._save_state(state)

    def insert_item(self, guid: str, *, itype="Movie", ts=1000, fetch_status=0, title=None):
        self.conn.execute(
            "INSERT INTO item (guid,type,title,path,fetch_status,create_time,update_time)"
            " VALUES (?,?,?,?,?,?,?)",
            (guid, itype, title or guid, f"/media/{guid}.mkv", fetch_status, ts, ts),
        )
        self.conn.commit()

    def insert_delete(self, *, ts=2000, path="/media/x.mkv", title="X", guid="dg1"):
        self.conn.execute(
            "INSERT INTO media_delete (guid,path,title,update_time) VALUES (?,?,?,?)",
            (guid, path, title, ts),
        )
        self.conn.commit()


class TestDeleteEvent(MediaCase):
    def test_delete_row_emits_event(self):
        poller = self.make()
        self.baseline(poller)
        self.insert_delete(ts=2000, path="/media/电影 A.mkv", title="电影 A")

        poller._poll_once(poller._load_state())

        types = [ev for ev, _ in self.delivered]
        self.assertIn(TRIM_RESOURCE_DELETED, types)
        ed = next(ed for ev, ed in self.delivered if ev == TRIM_RESOURCE_DELETED)
        self.assertEqual(ed["path"], "/media/电影 A.mkv")
        self.assertEqual(ed["title"], "电影 A")
        self.assertIn("删除", ed["message"])

    def test_delete_watermark_advances_and_no_duplicate(self):
        poller = self.make()
        self.baseline(poller)
        self.insert_delete(ts=2000)

        state = poller._load_state()
        poller._poll_once(state)
        first = len(self.delivered)
        self.assertEqual(first, 1)
        self.assertEqual(int(state["last_media_delete_ms"]), 2000)

        # 再跑一轮：水位已推进，不应重复推送
        poller._poll_once(state)
        self.assertEqual(len(self.delivered), first, "同一删除记录不得重复推送")

    def test_delete_before_watermark_is_not_pushed(self):
        """baseline 之前的存量删除记录不补推（与既有「不推送历史」语义一致）。"""
        poller = self.make()
        self.insert_delete(ts=500)
        self.baseline(poller)
        poller._poll_once(poller._load_state())
        self.assertEqual([ev for ev, _ in self.delivered], [])

    def test_delete_not_monitored_is_ignored(self):
        poller = self.make(events=(TRIM_RESOURCE_ADDED,))
        self.baseline(poller)
        self.insert_delete(ts=2000)
        poller._poll_once(poller._load_state())
        self.assertEqual([ev for ev, _ in self.delivered], [])


class TestSchemaTolerance(MediaCase):
    def test_different_column_names(self):
        """不同飞牛版本列名不同：按实际列自适应该照常推送。"""
        self.assertEqual(self.delete_columns, "guid TEXT, path TEXT, title TEXT, update_time INTEGER")

    def test_delete_time_column_variant(self):
        self.conn.execute("DROP TABLE media_delete")
        self.conn.executescript(
            "CREATE TABLE media_delete (id INTEGER PRIMARY KEY, file_path TEXT, delete_time INTEGER, name TEXT)"
        )
        self.conn.commit()

        poller = self.make()
        self.baseline(poller)
        self.conn.execute(
            "INSERT INTO media_delete (file_path, delete_time, name) VALUES (?,?,?)",
            ("/media/剧集 B.mkv", 2100, "剧集 B"),
        )
        self.conn.commit()

        poller._poll_once(poller._load_state())

        deleted = [ed for ev, ed in self.delivered if ev == TRIM_RESOURCE_DELETED]
        self.assertEqual(len(deleted), 1, "delete_time / file_path 列变体应被识别")
        self.assertEqual(deleted[0]["path"], "/media/剧集 B.mkv")
        self.assertEqual(deleted[0]["title"], "剧集 B")

    def test_missing_table_degrades_silently(self):
        self.conn.execute("DROP TABLE media_delete")
        self.conn.commit()

        poller = self.make()
        self.baseline(poller)
        self.assertFalse(poller._media_delete_columns(poller._connect()), "无表时应返回空集合")

        self.insert_item("movie-1", ts=2000)
        poller._poll_once(poller._load_state())
        self.assertIn(TRIM_RESOURCE_ADDED, [ev for ev, _ in self.delivered], "其它影视事件不受影响")

    def test_table_without_time_column_degrades(self):
        self.conn.execute("DROP TABLE media_delete")
        self.conn.executescript("CREATE TABLE media_delete (guid TEXT, path TEXT)")
        self.conn.commit()

        poller = self.make()
        self.baseline(poller)
        self.conn.execute("INSERT INTO media_delete (guid,path) VALUES ('a','/media/a.mkv')")
        self.conn.commit()

        poller._poll_once(poller._load_state())
        self.assertEqual([ev for ev, _ in self.delivered], [], "无时间列时静默跳过，不报错不推送")


class TestExistingMediaEventsUnchanged(MediaCase):
    def test_new_item_still_emits_added(self):
        poller = self.make()
        self.baseline(poller)
        self.insert_item("movie-2", ts=2000)
        poller._poll_once(poller._load_state())
        self.assertEqual([ev for ev, _ in self.delivered], [TRIM_RESOURCE_ADDED])

    def test_scrape_success_still_emits(self):
        poller = self.make()
        self.insert_item("movie-3", ts=1000, fetch_status=0)
        self.baseline(poller)
        self.conn.execute(
            "UPDATE item SET fetch_status=1, update_time=3000 WHERE guid='movie-3'"
        )
        self.conn.commit()
        poller._poll_once(poller._load_state())
        self.assertIn(TRIM_SCRAPE_SUCCESS, [ev for ev, _ in self.delivered])

    def test_state_file_keeps_delete_watermark_field(self):
        poller = self.make()
        self.baseline(poller)
        state = poller._load_state()
        self.assertIn("last_media_delete_ms", state, "状态字段保持兼容，便于版本回退")


if __name__ == "__main__":
    unittest.main(verbosity=2)
