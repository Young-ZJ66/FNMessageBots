"""批次明细写入推送记录的回归测试。

运行方式（项目根目录）：
    python tests/test_batch_items_in_history.py

使用 tests/_support/stub_deps.py 的依赖桩，不联网。
覆盖行为：
- process_batch_events 为汇总事件附带全量 batch_items（每条含 event_type/timestamp/brief）
- 批次内重复项先去重再统计
- 超过 BATCH_ITEMS_HISTORY_LIMIT 时截断（完整数据仍在 data/logs 落盘）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
# event_processor 以 `src.` 前缀导入通知模块，需同时可寻址项目根与 src
for _p in (str(_SUPPORT), str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

from monitor.event_processor import EventProcessor  # noqa: E402


class _CapturingNotifier:
    """捕获 send_notification 调用的桩。"""

    def __init__(self):
        self.calls = []

    def send_notification(self, event_type, event_data, raw_log, timestamp):
        self.calls.append({"event_type": event_type, "event_data": event_data})
        return SimpleNamespace(success=True, method="test", details={})


class BatchItemsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.gettempdir()) / f"fnmb_batch_items_{uuid.uuid4().hex[:8]}"
        (self.root / "logs").mkdir(parents=True)
        (self.root / "cursor").mkdir(parents=True)
        self.config = SimpleNamespace(
            log_dir=str(self.root / "logs"),
            cursor_dir=str(self.root / "cursor"),
            log_retention_days=7,
        )
        self.notifier = _CapturingNotifier()
        self.processor = EventProcessor(self.notifier, self.config)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _item(self, idx: int, event_type: str = "TRIM_RESOURCE_ADDED") -> dict:
        return {
            "row_id": str(idx),
            "event_type": event_type,
            "event_data": {"title": f"影片{idx}", "_source": "trim_media_db"},
            "entry": SimpleNamespace(
                timestamp=f"2026-10-11 21:00:{idx % 60:02d}",
                raw_data="{}",
                cursor=f"cur-{idx}",
                original_line="",
            ),
            "source": "trim_media_db",
        }

    def _summary(self):
        self.assertEqual(len(self.notifier.calls), 1)
        call = self.notifier.calls[0]
        self.assertEqual(call["event_type"], "POLL_BATCH_SUMMARY")
        return call["event_data"]

    def test_batch_items_attached_with_briefs(self):
        self.processor.process_batch_events([self._item(1), self._item(2), self._item(3)])
        ed = self._summary()
        items = ed["batch_items"]
        self.assertEqual(len(items), 3)
        for i, it in enumerate(items, 1):
            self.assertEqual(it["event_type"], "TRIM_RESOURCE_ADDED")
            self.assertIn("timestamp", it)
            self.assertIn("影片", it["brief"])
            self.assertLessEqual(len(it["brief"]), 121)

    def test_duplicates_removed_before_counting(self):
        self.processor.process_batch_events([self._item(1), self._item(1)])
        ed = self._summary()
        self.assertEqual(ed["count"], 1)
        self.assertEqual(len(ed["batch_items"]), 1)

    def test_mixed_types_keep_type_field(self):
        self.processor.process_batch_events(
            [self._item(1), self._item(2, event_type="DOCKER_CONTAINER_START")]
        )
        ed = self._summary()
        types = {it["event_type"] for it in ed["batch_items"]}
        self.assertEqual(types, {"TRIM_RESOURCE_ADDED", "DOCKER_CONTAINER_START"})

    def test_history_cap_limits_items(self):
        limit = EventProcessor.BATCH_ITEMS_HISTORY_LIMIT
        self.processor.process_batch_events(
            [self._item(i) for i in range(limit + 7)]
        )
        ed = self._summary()
        self.assertEqual(ed["count"], limit + 7)
        self.assertEqual(len(ed["batch_items"]), limit, "写入推送记录的明细应按上限截断")


if __name__ == "__main__":
    unittest.main()
