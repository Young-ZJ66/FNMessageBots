"""媒体库事件聚合窗口（main.Application 队列到期规则）的回归测试。

运行方式（项目根目录）：
    python tests/test_media_batch_window.py

使用 tests/_support/stub_deps.py 的依赖桩，不联网、不需要第三方依赖。

覆盖行为：
- 影视/相册类事件入队后进入静默窗口，未到期时不刷出
- 静默窗口经过（无新事件）后到期刷出
- 单条滞留超过窗口 4 倍硬上限后强制到期（持续刮削不饿死）
- 非媒体事件不入窗口，随时可刷出
- 窗口关闭（media_batch_window=0 / 开关关闭）时立即到期
- force=True（关机兜底）取全部
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

import main as app_main  # noqa: E402


def _media_item(event_type: str = "TRIM_RESOURCE_ADDED", **extra) -> dict:
    item = {"event_type": event_type, "event_data": {"title": "影片"}, "source": "trim_media_db"}
    item.update(extra)
    return item


class MediaBatchWindowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = app_main.Application()
        self.app.config = SimpleNamespace(
            media_batch_summary_enabled=True,
            media_batch_window=90,
        )

    def test_media_item_deferred_within_window(self):
        self.app._enqueue_unified_batch_items([_media_item()])
        self.assertEqual(self.app._take_unified_batch_buf(), [], "静默窗口内不得刷出")

    def test_media_item_due_after_quiet_window(self):
        self.app._enqueue_unified_batch_items([_media_item()])
        # 静默：把最后入队时间拨回窗口之前
        self.app._unified_last_enqueue_ts = time.time() - 120
        due = self.app._take_unified_batch_buf()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["event_type"], "TRIM_RESOURCE_ADDED")
        # 取出后队列清空
        self.assertEqual(self.app._take_unified_batch_buf(), [])

    def test_media_item_hard_cap_after_window_times_four(self):
        self.app._enqueue_unified_batch_items([_media_item()])
        # 队列一直有新事件（last_enqueue 保持新鲜），但首条已滞留超过 4 倍窗口
        self.app._unified_last_enqueue_ts = time.time()
        item = self.app._unified_batch_buf[0]
        item["_enqueued_at"] = time.time() - 90 * 4 - 1
        due = self.app._take_unified_batch_buf()
        self.assertEqual(len(due), 1, "滞留超过硬上限的事件必须强制到期")

    def test_non_media_item_due_immediately(self):
        # Docker 异常信号（OOM/DIE/KILL）不入聚合窗口，随时可刷出
        self.app._enqueue_unified_batch_items([_media_item("DOCKER_CONTAINER_OOM", source="docker")])
        due = self.app._take_unified_batch_buf()
        self.assertEqual(len(due), 1, "非聚合事件不入聚合窗口")

    def test_disabled_flag_or_zero_window_disables_defer(self):
        self.app.config.media_batch_summary_enabled = False
        self.app._enqueue_unified_batch_items([_media_item()])
        self.assertEqual(len(self.app._take_unified_batch_buf()), 1)

        self.app.config.media_batch_summary_enabled = True
        self.app.config.media_batch_window = 0
        self.app._enqueue_unified_batch_items([_media_item()])
        self.assertEqual(len(self.app._take_unified_batch_buf()), 1, "窗口为 0 应立即到期")

    def test_force_takes_all(self):
        self.app._enqueue_unified_batch_items([_media_item()])
        self.assertEqual(len(self.app._unified_batch_buf), 1)
        due = self.app._take_unified_batch_buf(force=True)
        self.assertEqual(len(due), 1, "force 必须取出全部")
        self.assertEqual(self.app._unified_batch_buf, [], "force 后队列应为空")

    def test_docker_lifecycle_deferred_but_oom_immediate(self):
        """Docker 生命周期流转进聚合窗口（compose 批量部署易突发），异常信号即时。"""
        self.app._enqueue_unified_batch_items([
            _media_item("DOCKER_CONTAINER_START", source="docker"),
            _media_item("DOCKER_CONTAINER_OOM", source="docker"),
        ])
        due = self.app._take_unified_batch_buf()
        self.assertEqual([it["event_type"] for it in due], ["DOCKER_CONTAINER_OOM"])
        self.assertEqual(len(self.app._unified_batch_buf), 1)

    def test_mixed_items_split_by_due(self):
        self.app._enqueue_unified_batch_items(
            [_media_item(), _media_item("DOCKER_CONTAINER_OOM", source="docker")]
        )
        # OOM 即时到期；媒体项仍在窗口内
        due = self.app._take_unified_batch_buf()
        self.assertEqual([it["event_type"] for it in due], ["DOCKER_CONTAINER_OOM"])
        self.assertEqual(len(self.app._unified_batch_buf), 1)
        # 静默后媒体项到期
        self.app._unified_last_enqueue_ts = time.time() - 120
        due = self.app._take_unified_batch_buf()
        self.assertEqual([it["event_type"] for it in due], ["TRIM_RESOURCE_ADDED"])

    def test_flush_uses_event_processor(self):
        """定时刷盘只推送到期项；关机 force 刷出全部。"""
        pushed: list = []

        class FakeProcessor:
            def process_batch_events(self, items):
                pushed.extend(items)
                return True

        self.app.event_processor = FakeProcessor()
        self.app._enqueue_unified_batch_items([_media_item()])
        self.app._flush_unified_batch_pending_only()
        self.assertEqual(pushed, [], "窗口内不得推送")
        self.app._flush_unified_batch_pending_only(force=True)
        self.assertEqual(len(pushed), 1, "关机兜底必须全部刷出")


if __name__ == "__main__":
    unittest.main()
