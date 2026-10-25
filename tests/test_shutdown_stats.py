"""UnifiedNotifier.get_stats 拍平键的回归测试（关停统计永远打 0 的修复）。

运行方式（项目根目录）：
    python tests/test_shutdown_stats.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

from notifier.unified_notifier import UnifiedNotifier  # noqa: E402


class _FakeInner:
    def __init__(self, stats):
        self._stats = stats

    def get_stats(self):
        return self._stats


class _FakeConfig:
    wechat_webhook_url = "https://example.test/w"
    dingtalk_webhook_url = ""
    feishu_webhook_url = ""
    pushplus_params = ""
    smtp_params = ""
    webhook_params = ""
    meow_params = ""
    bark_url = ""
    bark_icon = ""
    title_prefix = "飞牛NAS"
    minimal_push_enabled = False
    push_markdown_enabled = True
    push_emoji_enabled = False
    poll_batch_summary_enabled = False
    dedup_window = 300
    http_pool_size = 2
    http_retry_count = 1
    http_timeout = 5
    trim_media_db_path = ""
    trim_activity_db_path = ""
    logger_db_path = ""
    cursor_dir = "./data/cursor"
    log_dir = "./data/logs"
    log_retention_days = 7


class ShutdownStatsCase(unittest.TestCase):
    def _notifier_with_stats(self, stats):
        n = UnifiedNotifier.__new__(UnifiedNotifier)
        n.config = _FakeConfig()
        n.multi_platform_notifier = _FakeInner(stats)
        return n

    def test_top_level_keys_match_pool_counters(self):
        n = self._notifier_with_stats({
            "total_requests": 12,
            "successful_requests": 11,
            "failed_requests": 1,
            "success_rate": "91.7%",
        })
        stats = n.get_stats()
        self.assertEqual(stats["request_count"], 12)
        self.assertEqual(stats["success_count"], 11)
        self.assertEqual(stats["error_count"], 1)
        self.assertEqual(stats["success_rate"], "91.7%")

    def test_zero_when_missing(self):
        n = self._notifier_with_stats({})
        stats = n.get_stats()
        self.assertEqual(stats["request_count"], 0)
        self.assertEqual(stats["success_count"], 0)
        self.assertEqual(stats["error_count"], 0)
        self.assertEqual(stats["success_rate"], "0.0%")

    def test_nested_dict_kept_for_compat(self):
        inner = {"total_requests": 3, "successful_requests": 3,
                 "failed_requests": 0, "success_rate": "100.0%"}
        stats = self._notifier_with_stats(inner).get_stats()
        self.assertEqual(stats["multi_platform_notifier"], inner)


if __name__ == "__main__":
    unittest.main()
