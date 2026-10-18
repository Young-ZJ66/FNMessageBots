"""URL 脱敏回归测试：日志/接口输出不得泄露 webhook key、access_token、URL 路径中的令牌。

运行方式（项目根目录）：
    python tests/test_url_redaction.py
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

from notifier.connection_pool import SECRET_MASK, safe_url_for_log  # noqa: E402

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

FEISHU_KEY = "6f1a9c3d-4b2e-4f8a-9c1d-7e5b3a2f0d91"
WECOM_KEY = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
MAGIC_TOKEN = "abcdef0123456789abcdef0123456789"


class TestSafeUrlForLog(unittest.TestCase):
    def test_keeps_plain_url(self):
        self.assertEqual(
            safe_url_for_log("http://192.168.1.10:818/api/push"),
            "http://192.168.1.10:818/api/push",
        )

    def test_masks_sensitive_query_values(self):
        out = safe_url_for_log(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}")
        self.assertNotIn(WECOM_KEY, out)
        self.assertIn("key=", out)

    def test_masks_feishu_webhook_key_in_query(self):
        out = safe_url_for_log(f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_KEY}")
        self.assertNotIn(FEISHU_KEY, out)

    def test_masks_access_token_query(self):
        out = safe_url_for_log(f"https://oapi.dingtalk.com/robot/send?access_token={MAGIC_TOKEN}&x=1")
        self.assertNotIn(MAGIC_TOKEN, out)
        self.assertIn("x=1", out, "非敏感参数应保留")

    def test_masks_token_in_path(self):
        out = safe_url_for_log(f"http://192.168.1.10:818/api/push/{MAGIC_TOKEN}")
        self.assertNotIn(MAGIC_TOKEN, out)
        self.assertIn("/api/push/", out)

    def test_masks_userinfo_credentials(self):
        out = safe_url_for_log("http://user:pass1234@host.local:9010/hook")
        self.assertNotIn("pass1234", out)
        self.assertIn("host.local", out)

    def test_empty_and_invalid_inputs_are_safe(self):
        self.assertEqual(safe_url_for_log(""), "")
        self.assertEqual(safe_url_for_log(None), "")
        # 非法 URL 不应抛异常
        self.assertIsInstance(safe_url_for_log("http://[::1"), str)

    def test_short_path_segments_are_kept(self):
        """短路径段（如 /api/v1/send）不应被误脱敏。"""
        self.assertEqual(
            safe_url_for_log("https://example.test/api/v1/send"),
            "https://example.test/api/v1/send",
        )


class TestGetStatsRedaction(unittest.TestCase):
    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        self.root = _TMP_ROOT / f"redaction_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_get_stats_does_not_expose_webhook_keys(self):
        from notifier.multi_platform_notifier import MultiPlatformNotifier

        notifier = MultiPlatformNotifier(
            wechat_webhook_url=f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}",
            feishu_webhook_url=f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_KEY}",
            dingtalk_webhook_url=f"https://oapi.dingtalk.com/robot/send?access_token={MAGIC_TOKEN}",
            dedup_window=300,
            pool_size=2,
            retries=1,
            timeout=5,
        )
        try:
            stats = notifier.get_stats()
        finally:
            notifier.close()

        blob = repr(stats)
        self.assertNotIn(WECOM_KEY, blob)
        self.assertNotIn(FEISHU_KEY, blob)
        self.assertNotIn(MAGIC_TOKEN, blob)
        self.assertIn(SECRET_MASK, stats["wechat_webhook_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
