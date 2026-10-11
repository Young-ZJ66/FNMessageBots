"""共享去重缓存（utils.dedup_store）行为测试。

抽出前 6 个轮询器各有一份逐行相同的实现，抽出后必须保持完全一致的语义：
TTL 过滤、损坏文件容错、键值类型归一、落盘格式。
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
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

from utils.dedup_store import DedupStore  # noqa: E402

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


class DedupStoreCase(unittest.TestCase):
    ttl = 3600

    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"dedup_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "poller_dedup.json"
        self.logger = logging.getLogger("test.dedup")
        self.store = DedupStore(self.path, self.ttl, "测试用", self.logger)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def write_raw(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")


class TestLoad(DedupStoreCase):
    def test_missing_file_yields_empty(self):
        self.assertEqual(self.store.load(), {})

    def test_expired_entries_are_dropped(self):
        now = int(time.time())
        self.write_raw(json.dumps({
            "fresh": now - 10,
            "stale": now - self.ttl - 10,
        }))
        self.assertEqual(self.store.load(), {"fresh": now - 10})

    def test_keys_are_stringified_and_values_int(self):
        now = int(time.time())
        self.write_raw(json.dumps({str(now - 5): float(now - 5)}))
        seen = self.store.load()
        self.assertEqual(list(seen.keys()), [str(now - 5)])
        self.assertIsInstance(seen[str(now - 5)], int)

    def test_non_numeric_values_are_ignored(self):
        now = int(time.time())
        self.write_raw(json.dumps({"good": now, "bad": "abc", "none": None, "obj": {}}))
        self.assertEqual(self.store.load(), {"good": now})

    def test_corrupt_file_clears_cache_without_raising(self):
        self.store.seen = {"keep": int(time.time())}
        self.write_raw("{not json")
        self.assertEqual(self.store.load(), {}, "损坏文件应按无缓存处理")
        self.assertEqual(self.store.seen, {})

    def test_non_dict_json_clears_cache(self):
        self.write_raw("[1, 2, 3]")
        self.assertEqual(self.store.load(), {})

    def test_empty_file_is_tolerated(self):
        self.write_raw("")
        self.assertEqual(self.store.load(), {})


class TestSave(DedupStoreCase):
    def test_roundtrip(self):
        now = int(time.time())
        self.store.seen = {"a": now}
        self.store.save()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"a": now})

        other = DedupStore(self.path, self.ttl, "测试用", self.logger)
        self.assertEqual(other.load(), {"a": now})

    def test_unicode_keys_saved_readable(self):
        self.store.seen = {"事件|影视": int(time.time())}
        self.store.save()
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("事件|影视", raw, "ensure_ascii=False，避免中文键被转义")

    def test_save_failure_does_not_raise(self):
        bad = DedupStore(self.root / "no_such_dir" / "x.json", self.ttl, "测试用", self.logger)
        bad.seen = {"a": 1}
        bad.save()  # 不应抛异常


class TestPrune(DedupStoreCase):
    def test_prune_drops_expired_only(self):
        now = int(time.time())
        self.store.seen = {"fresh": now, "stale": now - self.ttl - 1}
        self.store.prune()
        self.assertEqual(self.store.seen, {"fresh": now})

    def test_prune_normalizes_values(self):
        now = int(time.time())
        self.store.seen = {"a": str(now)}
        self.store.prune()
        self.assertIsInstance(self.store.seen["a"], int)


class TestPollerIntegration(unittest.TestCase):
    """轮询器侧的兼容访问：_dedup_seen 读写必须落到 store。"""

    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"dedupp_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cursor = self.root / "cursor"
        self.cursor.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_poller_property_is_backed_by_store(self):
        from monitor.media_db_poller import MediaDBPoller

        p = MediaDBPoller(
            db_path=str(self.root / "trimmedia.db"),
            cursor_dir=str(self.cursor),
            poll_interval=1,
            monitor_events=["TRIM_RESOURCE_ADDED"],
        )
        now = int(time.time())
        # 直接赋值（历史上 prune 就是这么做的）
        p._dedup_seen = {"x": now}
        self.assertEqual(p._dedup_store.seen, {"x": now}, "赋值必须写进 store")
        # 就地写入
        p._dedup_seen["y"] = now
        self.assertEqual(p._dedup_store.seen["y"], now, "就地写入必须可见")
        # 落盘再读回
        p._save_dedup()
        p._dedup_store.seen = {}
        p._load_dedup()
        self.assertEqual(p._dedup_seen, {"x": now, "y": now})
        # prune 走 store
        p._dedup_store.seen = {"old": now - 100 * 24 * 3600}
        p._prune_dedup()
        self.assertEqual(p._dedup_seen, {})

    def test_pollers_delegate_dedup_to_shared_store(self):
        """6 个轮询器的去重读写都委托给共享 DedupStore，不再各自实现一份。"""
        import inspect

        from monitor import (
            backup_db_poller,
            docker_events_poller,
            media_db_poller,
            photo_db_poller,
            scheduler_db_poller,
            trim_activity_poller,
        )

        modules = (
            backup_db_poller,
            docker_events_poller,
            media_db_poller,
            photo_db_poller,
            scheduler_db_poller,
            trim_activity_poller,
        )
        for mod in modules:
            classes = [
                obj
                for _, obj in inspect.getmembers(mod, inspect.isclass)
                if obj.__module__ == mod.__name__ and hasattr(obj, "_load_dedup")
            ]
            self.assertTrue(classes, f"{mod.__name__}: 未找到轮询器类")
            cls = classes[0]
            with self.subTest(poller=cls.__name__):
                for name in ("_load_dedup", "_save_dedup", "_prune_dedup"):
                    src = inspect.getsource(getattr(cls, name))
                    self.assertIn("self._dedup_store", src,
                                  f"{cls.__name__}.{name} 应委托给 DedupStore")
                    self.assertNotIn("json.loads", src, f"{cls.__name__}.{name} 不应自己解析 JSON")
                    self.assertNotIn("json.dumps", src, f"{cls.__name__}.{name} 不应自己序列化")


if __name__ == "__main__":
    unittest.main(verbosity=2)
