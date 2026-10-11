"""渠道富文本能力记忆的回归测试。

行为约定：
- 渠道明确以「格式类错误」拒绝富文本 → 记住，后续直接发纯文本（不再先失败一次）
- 网络/鉴权/限流类失败 → **不**记忆，仍按「先富文本再回退」处理
- 进程内记忆，重启后重新探测
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

from notifier import multi_platform_notifier as mpn  # noqa: E402
from notifier.multi_platform_notifier import (  # noqa: E402
    MultiPlatformNotifier,
    _looks_like_format_rejection,
    _looks_like_security_rejection,
    reset_rich_text_rejected_channels,
    rich_text_rejected_channels,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def format_rejected(method, url, kwargs):
    payload = kwargs.get("json") or {}
    if payload.get("msgtype") == "markdown" or payload.get("msg_type") == "post":
        return stub_deps.StubResponse(
            400, text='{"errcode":40008,"errmsg":"invalid message type"}'
        )
    return stub_deps.StubResponse(200, text='{"errcode":0}')


def server_error(method, url, kwargs):
    return stub_deps.StubResponse(500, text="internal error")


def timeout_error(method, url, kwargs):
    raise stub_deps._Timeout("connection timed out")


def security_rejected(method, url, kwargs):
    """模拟钉钉机器人安全设置未通过（关键词不在消息内容中）。"""
    return stub_deps.StubResponse(
        400, text='{"errcode":310000,"errmsg":"keywords not in content"}'
    )


class NotifierCase(unittest.TestCase):
    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        reset_rich_text_rejected_channels()
        # 兜底：即便用例中途失败，也不把进程级状态留给后面的用例
        self.addCleanup(reset_rich_text_rejected_channels)
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        reset_rich_text_rejected_channels()

    def make(self, **kwargs) -> MultiPlatformNotifier:
        params = dict(title_prefix="飞牛NAS", dedup_window=300, pool_size=2, retries=1, timeout=5)
        params.update(kwargs)
        n = MultiPlatformNotifier(**params)
        n.merge_window = 3600
        return n

    def send_once(self, notifier, event_type="LoginSucc", channel="wechat"):
        """按渠道发一次（默认企业微信）。"""
        msg = notifier._build_message(event_type, {"user": "a", "IP": "1.1.1.1"}, "t", "{}")
        return getattr(notifier, f"_send_to_{channel}")(msg)

    def sent_msgtypes(self):
        """已发出请求的消息类型（企微/钉钉用 msgtype，飞书用 msg_type）。"""
        return [
            r["kwargs"]["json"].get("msgtype") or r["kwargs"]["json"].get("msg_type")
            for r in stub_deps.REQUEST_LOG
        ]


class TestFormatRejectionDetection(unittest.TestCase):
    def test_format_errors_detected(self):
        for err in (
            "HTTP 400: invalid message type",
            "errcode=40008 invalid message type",
            "invalid msgtype",
            "unsupported message type",
            "message type not support",
            "渠道不支持该格式",
            "格式不正确",
        ):
            with self.subTest(error=err):
                self.assertTrue(_looks_like_format_rejection(err), err)

    def test_transient_errors_not_treated_as_format(self):
        for err in (
            "请求超时",
            "connection timed out",
            "连接错误: ConnectionError",
            "HTTP 401 unauthorized",
            "HTTP 403 forbidden",
            "rate limit exceeded",
            "HTTP 429",
        ):
            with self.subTest(error=err):
                self.assertFalse(_looks_like_format_rejection(err), err)

    def test_empty_error_is_not_format(self):
        self.assertFalse(_looks_like_format_rejection(""))
        self.assertFalse(_looks_like_format_rejection(None))

    def test_dingtalk_security_errors_are_not_format(self):
        """钉钉自定义机器人群消息校验失败统一返回 errcode=310000 + errmsg 文本。

        这些是**安全设置**问题：回退纯文本同样会失败，不能记忆为「不支持富文本」，
        否则会把真实原因（关键词/加签/白名单）掩盖成渠道能力问题。
        """
        for err in (
            "errcode=310000 keywords not in content",
            "310000 sign not match",
            "errcode=310000 invalid timestamp",
            "310000 ip 1.2.3.4 not in whitelist",
            "消息内容中不包含任何关键词",
        ):
            with self.subTest(error=err):
                self.assertFalse(_looks_like_format_rejection(err), f"{err} 不应被判为格式问题")
                self.assertTrue(_looks_like_security_rejection(err), f"{err} 应被判为安全设置问题")

    def test_fabricated_codes_no_longer_match(self):
        """回归：早前按（错误的）错误码判断，19021/19024 等并非钉钉真实编码，已剔除。"""
        for err in ("errcode=19021", "errcode=19024", "errcode=93000"):
            with self.subTest(error=err):
                self.assertFalse(
                    _looks_like_format_rejection(err),
                    f"{err} 是凭印象写的错误码，不该作为格式判据",
                )

    def test_security_detection_ignores_unrelated(self):
        for err in ("", None, "connection timed out", "invalid message type"):
            with self.subTest(error=err):
                self.assertFalse(_looks_like_security_rejection(err))


class TestCapabilityMemory(NotifierCase):
    def test_first_send_tries_structured_then_falls_back(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(self.sent_msgtypes(), ["markdown", "text"], "首条应尝试富文本再回退")

    def test_second_send_skips_structured(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            self.send_once(n)
            stub_deps.REQUEST_LOG.clear()
            n.sent_events.clear()  # 避免去重拦截，强制走一次真实发送
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(self.sent_msgtypes(), ["text"], "第二次应直接发纯文本")
        self.assertEqual(len(stub_deps.REQUEST_LOG), 1, "不应再有失败的首轮请求")

    def test_memory_is_per_channel(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(
            wechat_webhook_url="http://example.test/wechat",
            dingtalk_webhook_url="http://example.test/ding",
        )
        try:
            self.send_once(n)  # 只发企业微信
            stub_deps.REQUEST_LOG.clear()
            n.sent_events.clear()
            msg = n._build_message("LoginSucc", {"user": "a"}, "t", "{}")
            ok, _ = n._send_to_dingtalk(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(
            self.sent_msgtypes(), ["markdown", "text"],
            "钉钉未被拒过，仍应先试富文本",
        )

    def test_remembered_channel_recorded(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            self.send_once(n)
        finally:
            n.close()
        self.assertIn("企业微信", rich_text_rejected_channels())

    def test_transient_failure_is_not_remembered(self):
        stub_deps.set_fake_handler(server_error)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertFalse(ok, "5xx 时纯文本回退也失败，应报失败")
        self.assertEqual(
            rich_text_rejected_channels(), {},
            "5xx 不是格式问题，不得记忆为「不支持富文本」",
        )

    def test_timeout_is_not_remembered(self):
        stub_deps.set_fake_handler(timeout_error)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertFalse(ok)
        self.assertEqual(rich_text_rejected_channels(), {}, "超时不得记忆为格式问题")

    def test_reset_allows_probe_again(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            self.send_once(n)
            reset_rich_text_rejected_channels()
            stub_deps.REQUEST_LOG.clear()
            n.sent_events.clear()
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(self.sent_msgtypes(), ["markdown", "text"], "重置后应重新探测")

    def test_markdown_disabled_never_probes(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat", markdown_enabled=False)
        try:
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(self.sent_msgtypes(), ["text"])
        self.assertEqual(rich_text_rejected_channels(), {}, "关闭结构化时无需探测")

    def test_successful_structured_send_is_not_remembered(self):
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            ok, _ = self.send_once(n)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(self.sent_msgtypes(), ["markdown"])
        self.assertEqual(rich_text_rejected_channels(), {})

    def test_memory_shared_across_notifier_instances(self):
        """进程级记忆：热加载重建通知器后仍然生效，避免反复探测。"""
        stub_deps.set_fake_handler(format_rejected)
        n1 = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            self.send_once(n1)
        finally:
            n1.close()

        stub_deps.REQUEST_LOG.clear()
        n2 = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            ok, _ = self.send_once(n2)
        finally:
            n2.close()
        self.assertTrue(ok)
        self.assertEqual(self.sent_msgtypes(), ["text"])

    def test_reset_clears_memory(self):
        """reset 是对外契约：测试隔离与「渠道升级后重试」都依赖它。"""
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            self.send_once(n)
            self.assertNotEqual(rich_text_rejected_channels(), {})
            reset_rich_text_rejected_channels()
            self.assertEqual(rich_text_rejected_channels(), {})
        finally:
            n.close()

    def test_dingtalk_security_error_is_not_remembered(self):
        """安全设置未通过时不得记忆为「不支持富文本」，否则真实原因被掩盖。"""
        stub_deps.set_fake_handler(security_rejected)
        n = self.make(dingtalk_webhook_url="http://example.test/ding")
        try:
            ok, _ = self.send_once(n, channel="dingtalk")
        finally:
            n.close()
        self.assertFalse(ok)
        self.assertEqual(rich_text_rejected_channels(), {}, "安全设置问题不该被记忆")

    def test_dingtalk_security_error_still_probes_next_time(self):
        stub_deps.set_fake_handler(security_rejected)
        n = self.make(dingtalk_webhook_url="http://example.test/ding")
        try:
            self.send_once(n, channel="dingtalk")
            stub_deps.REQUEST_LOG.clear()
            n.sent_events.clear()
            self.send_once(n, channel="dingtalk")
        finally:
            n.close()
        self.assertEqual(
            self.sent_msgtypes(), ["markdown", "text"],
            "安全设置问题未解决前，每条仍应尝试结构化（记忆不会生效）",
        )

    def test_dingtalk_format_error_is_remembered(self):
        """对照组：真正不支持 markdown 时仍应记忆。"""
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(dingtalk_webhook_url="http://example.test/ding")
        try:
            ok, _ = self.send_once(n, channel="dingtalk")
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertIn("钉钉", rich_text_rejected_channels())


class TestNoCrossTestPollution(NotifierCase):
    """共用进程级状态时，用例之间必须互相看不到对方的探测结果。"""

    def test_case_a_marks_channel(self):
        stub_deps.set_fake_handler(format_rejected)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            self.send_once(n)
        finally:
            n.close()
        self.assertIn("企业微信", rich_text_rejected_channels())

    def test_case_b_starts_clean(self):
        self.assertEqual(
            rich_text_rejected_channels(), {},
            "上一个用例的探测结果不得泄漏到本用例（setUp 应已重置）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
