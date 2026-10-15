"""通知链路回归测试：磁盘事件合并的「入队 ≠ 已发送」语义，以及合并发送的补记。

运行方式（项目根目录）：
    python tests/test_notifier_disk_merge.py

使用 tests/_support/stub_deps.py 提供的 requests 桩，不联网、不修改任何生产文件。
"""

from __future__ import annotations

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

import requests  # noqa: E402  (loaded from stub)

from notifier.multi_platform_notifier import MultiPlatformNotifier  # noqa: E402

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


class DiskMergeCase(unittest.TestCase):
    min_merge_window = 3600  # 避免定时线程在测试期间提前发出合并事件

    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.recorded = []
        self.notifier = self._make_notifier(recorder=self.recorded.append)

    def tearDown(self) -> None:
        try:
            self.notifier.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_notifier(self, recorder=None, **kwargs):
        n = MultiPlatformNotifier(
            dedup_window=300,
            pool_size=2,
            retries=1,
            timeout=5,
            merged_event_recorder=recorder,
            **kwargs,
        )
        n.merge_window = self.min_merge_window
        return n

    @staticmethod
    def disk_event(name="sda"):
        return {"name": name, "model": "TEST", "message": f"{name} 唤醒"}


class TestDeferredNotSuccess(DiskMergeCase):
    def test_disk_event_returns_deferred_marker(self):
        ok, channel_results = self.notifier.send_notification(
            "DiskWakeup", self.disk_event(), "raw", "2026-01-01 00:00:00"
        )

        self.assertTrue(ok, "入队本身不算失败")
        self.assertEqual(len(channel_results), 1)
        self.assertEqual(channel_results[0].get("skipped"), "deferred",
                         "必须带 deferred 标记，供上层区分入队与已投递")
        # 事件确实进了合并缓存
        window = int(__import__("time").time() / self.min_merge_window)
        self.assertEqual(len(self.notifier.disk_wakeup_cache.get(window, [])), 1)

    def test_deferred_event_does_not_send_http(self):
        self.notifier.send_notification("DiskSpindown", self.disk_event(), "raw", "t")
        self.assertEqual(stub_deps.REQUEST_LOG, [], "入队阶段不应发起任何 HTTP 请求")
        self.assertEqual(self.recorded, [], "入队阶段不应回调记录")

    def test_both_disk_event_types_use_same_semantics(self):
        for et in ("DiskWakeup", "DiskSpindown"):
            with self.subTest(event_type=et):
                ok, crs = self.notifier.send_notification(et, self.disk_event(), "raw", "t")
                self.assertTrue(ok)
                self.assertEqual(crs[0].get("skipped"), "deferred")


class TestMergedSendRecorder(DiskMergeCase):
    def test_recorder_called_only_after_real_send(self):
        calls = []

        # items 是后加的逐条明细参数：推送记录要靠它才能展开看到是哪几块盘
        def recorder(event_type, count, success, channel_results, items=None):
            calls.append((event_type, count, success, channel_results, items))

        notifier = self._make_notifier(recorder=recorder, wechat_webhook_url="http://example.test/hook")
        disks = [self.disk_event("sda"), self.disk_event("sdb")]
        ok, crs = notifier._send_merged_disk_event("DiskWakeup", disks, 0)
        notifier.close()

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1, "真正发出合并通知后必须回调一次")
        event_type, count, success, channel_results, items = calls[0]
        self.assertEqual(event_type, "DiskWakeup")
        self.assertEqual(count, 2)
        self.assertTrue(success)
        self.assertTrue(channel_results)
        self.assertEqual(items, disks, "逐条明细必须原样传给记录方，否则无从回看")

    def test_recorder_reports_failure(self):
        calls = []

        def recorder(event_type, count, success, channel_results, items=None):
            calls.append((event_type, count, success, channel_results, items))

        stub_deps.set_fake_handler(lambda m, u, k: stub_deps.StubResponse(500, text="boom"))
        notifier = self._make_notifier(recorder=recorder, wechat_webhook_url="http://example.test/hook")
        ok, _ = notifier._send_merged_disk_event("DiskSpindown", [self.disk_event()], 0)
        notifier.close()

        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][2], "失败也必须回调，以便统计失败数")

    def test_recorder_exception_does_not_break_send(self):
        def boom(*_args):
            raise RuntimeError("recorder 挂了")

        notifier = self._make_notifier(recorder=boom, wechat_webhook_url="http://example.test/hook")
        ok, crs = notifier._send_merged_disk_event("DiskWakeup", [self.disk_event()], 0)
        notifier.close()
        self.assertTrue(ok, "记录回调异常不得影响推送结果")
        self.assertTrue(crs)

    def test_missing_recorder_is_tolerated(self):
        notifier = self._make_notifier(recorder=None, wechat_webhook_url="http://example.test/hook")
        ok, _ = notifier._send_merged_disk_event("DiskWakeup", [self.disk_event()], 0)
        notifier.close()
        self.assertTrue(ok, "未接回调时行为应与以前一致")


class TestUnifiedNotifierRecording(unittest.TestCase):
    """UnifiedNotifier 应把「入队」与「真实发送」分开记账。"""

    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        self.root = _TMP_ROOT / f"unified_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        import utils.push_stats as push_stats

        push_stats.init(str(self.root))
        self.push_stats = push_stats
        from utils import push_history

        push_history.init(str(self.root))
        self.push_history = push_history

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _config():
        from types import SimpleNamespace

        return SimpleNamespace(
            wechat_webhook_url="",
            dingtalk_webhook_url="",
            feishu_webhook_url="",
            bark_url="",
            bark_icon="",
            pushplus_params="",
            smtp_params="",
            webhook_params="",
            meow_params="",
            title_prefix="飞牛NAS",
            minimal_push_enabled=False,
            poll_batch_summary_enabled=False,
            trim_media_db_path="",
            trim_activity_db_path="",
            logger_db_path="",
            dedup_window=300,
            http_pool_size=2,
            http_retry_count=1,
            http_timeout=5,
            dnd_enabled=False,
            dnd_start_time="22:00",
            dnd_end_time="07:00",
        )

    def test_deferred_event_not_counted_as_success(self):
        from notifier.unified_notifier import UnifiedNotifier

        notifier = UnifiedNotifier(self._config())
        before = self.push_stats.get_total()

        result = notifier.send_notification(
            "DiskWakeup", {"name": "sda", "message": "唤醒"}, "raw", "2026-01-01 00:00:00"
        )

        after = self.push_stats.get_total()
        self.assertEqual(result.method, "disk_merge_deferred")
        self.assertEqual(before, after, "入队不应改变推送统计")
        self.assertEqual(self.push_history.get_total_counts()["total"], 0, "入队不应写推送记录")
        notifier.close()

    def test_merged_send_is_recorded(self):
        from notifier.unified_notifier import UnifiedNotifier

        notifier = UnifiedNotifier(self._config())
        before = self.push_stats.get_total()
        self.push_history.clear_all()

        notifier._record_merged_event_send("DiskWakeup", 3, True, [{"channel": "企业微信", "success": True}])

        after = self.push_stats.get_total()
        self.assertEqual(after["success"], before["success"] + 1, "真实合并发送应计入成功统计")
        counts = self.push_history.get_total_counts()
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["success"], 1)
        record = self.push_history.get_records(limit=1)[0]
        self.assertEqual(record["event_type"], "DiskWakeup")
        self.assertIn("3", record["summary"])
        notifier.close()

    def test_merged_items_are_stored_for_detail_view(self):
        """合并推送的逐条明细要入库，否则推送记录里只剩一个条数、无法回看是哪几块盘。

        入库前会裁掉大字段（raw_log / full_event_data 是整条原始事件，逐条存会让库膨胀）
        并限量，条数另有 merged_count 记录。"""
        from notifier.unified_notifier import UnifiedNotifier

        notifier = UnifiedNotifier(self._config())
        self.push_history.clear_all()
        notifier._record_merged_event_send(
            "DiskWakeup", 2, True, [{"channel": "企业微信", "success": True}],
            [
                {"disk": "/dev/sda", "model": "WD40", "serial": "SN1",
                 "timestamp": "2026-10-16 00:00:00", "raw_log": "x" * 500,
                 "full_event_data": {"big": "y" * 500}},
                {"disk": "/dev/sdb", "model": "WD40", "serial": "SN2"},
            ],
        )
        import json as _json

        rec = self.push_history.get_records(limit=1)[0]
        # 库里存的是 JSON 文本
        detail = _json.loads(rec["detail"])
        self.assertEqual(detail["merged_count"], 2)
        items = detail["merged_items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["disk"], "/dev/sda")
        self.assertEqual(items[0]["serial"], "SN1")
        self.assertNotIn("raw_log", items[0], "整条原始事件不入库")
        self.assertNotIn("full_event_data", items[0], "整条原始事件不入库")
        notifier.close()

    def test_merged_send_failure_is_recorded_as_failure(self):
        from notifier.unified_notifier import UnifiedNotifier

        notifier = UnifiedNotifier(self._config())
        self.push_stats.init(str(self.root))
        self.push_history.clear_all()

        notifier._record_merged_event_send("DiskSpindown", 1, False, [])

        counts = self.push_history.get_total_counts()
        self.assertEqual(counts["fail"], 1)
        self.assertEqual(counts["success"], 0)
        notifier.close()


class TestPushStatsAtomicWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"stats_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_record_persists_and_leaves_no_temp_files(self):
        import utils.push_stats as push_stats

        push_stats.init(str(self.root))
        for _ in range(5):
            push_stats.record(True)
        push_stats.record(False)

        got = push_stats.get_total()
        self.assertEqual(got, {"total": 6, "success": 5, "fail": 1})
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".push_stats.")]
        self.assertEqual(leftovers, [], "原子写入不应残留临时文件")

    def test_stats_file_is_valid_json(self):
        import json

        import utils.push_stats as push_stats

        push_stats.init(str(self.root))
        push_stats.record(True)
        path = Path(push_stats.get_stats_path())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("total", data)
        self.assertIn("daily", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
