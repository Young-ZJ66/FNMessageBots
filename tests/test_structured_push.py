"""结构化消息（企业微信 markdown / 钉钉 markdown / 飞书 post）与文案去 emoji 的回归测试。

运行方式（项目根目录）：
    python tests/test_structured_push.py

使用 tests/_support/stub_deps.py 的 requests 桩，不联网。
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

from notifier.multi_platform_notifier import (  # noqa: E402
    MultiPlatformMessage,
    MultiPlatformNotifier,
    _markdown_body_from_plain,
    reset_rich_text_rejected_channels,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

EMOJI_SAMPLE = "🔐 飞牛NAS-登录成功通知"


def _ok(method, url, kwargs):
    return stub_deps.StubResponse(200, text='{"errcode":0}')


def _reject_markdown(method, url, kwargs):
    """模拟渠道不支持 markdown：markdown 请求返回业务错误，纯文本正常。"""
    payload = kwargs.get("json") or {}
    if payload.get("msgtype") == "markdown" or payload.get("msg_type") == "post":
        return stub_deps.StubResponse(400, text='{"errcode":40008,"errmsg":"invalid message type"}')
    return stub_deps.StubResponse(200, text='{"errcode":0}')


class NotifierCase(unittest.TestCase):
    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        # 渠道富文本能力记忆是进程级状态：每个用例都要从「未探测」开始，
        # 否则前一个用例标记过「不支持富文本」会让后面的用例直接发纯文本。
        reset_rich_text_rejected_channels()
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        reset_rich_text_rejected_channels()

    def make(self, **kwargs) -> MultiPlatformNotifier:
        params = dict(
            title_prefix="飞牛NAS",
            dedup_window=300,
            pool_size=2,
            retries=1,
            timeout=5,
        )
        params.update(kwargs)
        n = MultiPlatformNotifier(**params)
        n.merge_window = 3600
        return n


class TestWechatStructured(NotifierCase):
    def test_markdown_payload(self):
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            msg = n._build_message(
                "LoginSucc", {"user": "admin", "IP": "192.168.1.10"}, "2026-01-01 12:00:00", "{}"
            )
            ok, cr = n._send_to_wechat(msg)
        finally:
            n.close()

        self.assertTrue(ok)
        sent = stub_deps.REQUEST_LOG[-1]["kwargs"]["json"]
        self.assertEqual(sent["msgtype"], "markdown")
        content = sent["markdown"]["content"]
        self.assertIn("登录成功通知", content)
        # 结构化：时间与键值明细收进引用块，无列表标记
        self.assertIn("> 时间: 2026-01-01 12:00:00", content)
        self.assertIn("> 用户名: admin", content)
        self.assertIn("> IP地址: 192.168.1.10", content)
        self.assertNotIn("- ", content, "企微 markdown 不支持列表，不得生成列表标记")

    def test_fallback_to_text_when_markdown_rejected(self):
        stub_deps.set_fake_handler(_reject_markdown)
        n = self.make(wechat_webhook_url="http://example.test/wechat")
        try:
            msg = n._build_message(
                "LoginSucc", {"user": "admin", "IP": "192.168.1.10"}, "2026-01-01 12:00:00", "{}"
            )
            ok, cr = n._send_to_wechat(msg)
        finally:
            n.close()

        self.assertTrue(ok, "markdown 被拒时必须回退纯文本并成功")
        msgtypes = [r["kwargs"]["json"].get("msgtype") for r in stub_deps.REQUEST_LOG]
        self.assertEqual(msgtypes, ["markdown", "text"], "应先尝试 markdown 再回退 text")
        self.assertTrue(cr.get("success"))
        self.assertIn("企业微信", str(cr.get("channel")))

    def test_markdown_disabled_uses_text_only(self):
        n = self.make(wechat_webhook_url="http://example.test/wechat", markdown_enabled=False)
        try:
            msg = n._build_message("LoginSucc", {"user": "a", "IP": "1.1.1.1"}, "t", "{}")
            ok, _ = n._send_to_wechat(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        self.assertEqual(len(stub_deps.REQUEST_LOG), 1)
        self.assertEqual(stub_deps.REQUEST_LOG[0]["kwargs"]["json"]["msgtype"], "text")

    def test_multiple_webhooks_all_receive(self):
        n = self.make(wechat_webhook_url="http://a.test/1|http://b.test/2")
        try:
            msg = n._build_message("LoginSucc", {"user": "a"}, "t", "{}")
            ok, _ = n._send_to_wechat(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        urls = [r["url"] for r in stub_deps.REQUEST_LOG]
        self.assertEqual(urls, ["http://a.test/1", "http://b.test/2"])


class TestDingtalkStructured(NotifierCase):
    def test_markdown_payload_has_title(self):
        n = self.make(dingtalk_webhook_url="http://example.test/ding")
        try:
            msg = n._build_message(
                "CPU_USAGE_ALARM", {"data": {"THRESHOLD": 90}}, "2026-01-01 12:00:00", "{}"
            )
            ok, _ = n._send_to_dingtalk(msg)
        finally:
            n.close()

        self.assertTrue(ok)
        sent = stub_deps.REQUEST_LOG[-1]["kwargs"]["json"]
        self.assertEqual(sent["msgtype"], "markdown")
        self.assertTrue(sent["markdown"]["title"], "钉钉 markdown 必带 title")
        self.assertIn("CPU使用率告警", sent["markdown"]["title"])
        self.assertIn("使用率阈值: 90%", sent["markdown"]["text"])
        self.assertNotIn("- ", sent["markdown"]["text"], "企微/钉钉 markdown 不支持列表，不得生成列表标记")

    def test_fallback_to_text_when_markdown_rejected(self):
        stub_deps.set_fake_handler(_reject_markdown)
        n = self.make(dingtalk_webhook_url="http://example.test/ding")
        try:
            msg = n._build_message("CPU_USAGE_ALARM", {"data": {"THRESHOLD": 90}}, "t", "{}")
            ok, _ = n._send_to_dingtalk(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        msgtypes = [r["kwargs"]["json"].get("msgtype") for r in stub_deps.REQUEST_LOG]
        self.assertEqual(msgtypes, ["markdown", "text"])


class TestFeishuStructured(NotifierCase):
    def test_post_payload(self):
        n = self.make(feishu_webhook_url="http://example.test/feishu")
        try:
            msg = n._build_message(
                "BACKUP_TASK_FAILED", {"task_name": "每日备份"}, "2026-01-01 12:00:00", "{}"
            )
            ok, _ = n._send_to_feishu(msg)
        finally:
            n.close()

        self.assertTrue(ok)
        sent = stub_deps.REQUEST_LOG[-1]["kwargs"]["json"]
        self.assertEqual(sent["msg_type"], "post")
        post = sent["content"]["post"]["zh_cn"]
        self.assertIn("备份任务失败", post["title"])
        self.assertTrue(post["content"])
        first_paragraph = post["content"][0]
        self.assertEqual(first_paragraph[0]["tag"], "text")
        self.assertIsInstance(first_paragraph[0]["text"], str)

    def test_fallback_to_text_when_post_rejected(self):
        stub_deps.set_fake_handler(_reject_markdown)
        n = self.make(feishu_webhook_url="http://example.test/feishu")
        try:
            msg = n._build_message("BACKUP_TASK_FAILED", {"task_name": "x"}, "t", "{}")
            ok, _ = n._send_to_feishu(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        msg_types = [r["kwargs"]["json"].get("msg_type") for r in stub_deps.REQUEST_LOG]
        self.assertEqual(msg_types, ["post", "text"])


class TestPlainChannelsUnaffected(NotifierCase):
    def test_bark_still_plain_and_plaintext_capable(self):
        n = self.make(bark_url="https://api.day.app/DEVKEY")
        try:
            msg = n._build_bark_message(
                "LoginSucc", {"user": "a"}, "2026-01-01 12:00:00", "{}"
            )
            ok, _ = n._send_to_bark(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        sent = stub_deps.REQUEST_LOG[-1]["kwargs"]["json"]
        self.assertEqual(set(sent.keys()) & {"msgtype", "msg_type", "markdown"}, set())
        self.assertIn("title", sent)
        self.assertIn("body", sent)

    def test_pushplus_payload_unchanged(self):
        import json as _json

        n = self.make(pushplus_params=_json.dumps({"token": "tk1"}))
        try:
            msg = n._build_bark_message("LoginSucc", {"user": "a"}, "t", "{}")
            ok, _ = n._send_to_pushplus(msg)
        finally:
            n.close()
        self.assertTrue(ok)
        sent = stub_deps.REQUEST_LOG[-1]["kwargs"]["json"]
        self.assertEqual(sent["token"], "tk1")
        self.assertNotIn("markdown", sent)


class TestEmojiReduction(NotifierCase):
    def test_titles_have_no_emoji_by_default(self):
        n = self.make(wechat_webhook_url="http://example.test/x")
        try:
            for event_type in ("LoginSucc", "APP_CRASH", "UPS_ONBATT", "DOCKER_CONTAINER_DIE"):
                with self.subTest(event=event_type):
                    msg = n._build_message(event_type, {"user": "a"}, "t", "{}")
                    self.assertNotRegex(msg.title, r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
                    self.assertNotIn("  ", msg.title)
                    self.assertTrue(
                        msg.title.startswith("飞牛NAS-"),
                        f"标题前缀不得被去图标逻辑破坏: {msg.title!r}",
                    )
        finally:
            n.close()

    def test_title_prefix_survives_emoji_strip(self):
        """回归：去掉图标后不得把「飞牛NAS-」前缀一起剥掉。"""
        n = self.make()
        try:
            msg = n._build_message("LoginSucc", {"user": "a"}, "t", "{}")
        finally:
            n.close()
        self.assertEqual(msg.title, "飞牛NAS-登录成功通知")

    def test_custom_title_prefix_survives_emoji_strip(self):
        n = self.make(title_prefix="我的NAS")
        try:
            msg = n._build_message("LoginSucc", {"user": "a"}, "t", "{}")
        finally:
            n.close()
        self.assertEqual(msg.title, "我的NAS-登录成功通知")

    def test_emoji_never_added_by_templates(self):
        """推送文案模板不再自带图标；显式开启也只影响事件数据自带的字符。"""
        n = self.make(plain_emoji_enabled=True)
        try:
            msg = n._build_message("LoginSucc", {"user": "a"}, "t", "{}")
        finally:
            n.close()
        self.assertNotRegex(
            msg.title, r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "标题模板不得携带图标"
        )

    def test_markdown_body_has_no_emoji(self):
        n = self.make(wechat_webhook_url="http://example.test/x")
        try:
            msg = n._build_message("LoginSucc", {"user": "a", "IP": "1.1.1.1"}, "t", "{}")
        finally:
            n.close()
        self.assertNotRegex(msg.markdown, r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")

    def test_plain_content_unchanged_for_history_summary(self):
        """markdown 开启不得改变 build_history_summary 依赖的纯文本渲染。"""
        n = self.make()
        try:
            summary_plain = n.build_history_summary(
                "LoginSucc", {"user": "a", "IP": "1.1.1.1"}, "{}", "2026-01-01 12:00:00"
            )
            msg = n._build_message("LoginSucc", {"user": "a", "IP": "1.1.1.1"}, "2026-01-01 12:00:00", "{}")
        finally:
            n.close()
        self.assertIn("用户名: a", summary_plain)
        self.assertIn("用户名: a", msg.content, "纯文本正文必须保持原有「键: 值」格式")
        self.assertNotIn("- 用户名", msg.content)


class TestMarkdownConversionUnit(unittest.TestCase):
    def test_timestamp_line_not_converted(self):
        out = _markdown_body_from_plain("2026-01-01 12:00:00\n用户名: admin")
        self.assertEqual(out.splitlines()[0], "2026-01-01 12:00:00")
        self.assertEqual(out.splitlines()[1], "用户名: admin")

    def test_indented_lines_become_plain_lines(self):
        out = _markdown_body_from_plain("【登录成功通知】\n\t用户名: a\n\tIP: 1.1.1.1\n\n说明行")
        self.assertEqual(
            out.splitlines(),
            ["**登录成功通知**", "用户名: a", "IP: 1.1.1.1", "", "说明行"],
        )

    def test_no_list_markers_generated(self):
        """企微 markdown 不支持列表语法：明细行不得生成 "- " 标记。"""
        out = _markdown_body_from_plain("2026-01-01 12:00:00\n\t影片1 · a.mkv\n\t影片2 · b.mkv")
        self.assertEqual(
            out.splitlines(),
            ["2026-01-01 12:00:00", "影片1 · a.mkv", "影片2 · b.mkv"],
        )

    def test_ellipsis_line_kept_separate(self):
        out = _markdown_body_from_plain("【类型】\n\t条目 1\n… 该类型其余 2 条请去日志查看")
        self.assertIn("… 该类型其余 2 条请去日志查看", out)

    def test_blank_input(self):
        self.assertEqual(_markdown_body_from_plain(""), "")
        self.assertEqual(_markdown_body_from_plain(None), "")

    def test_message_merged_markdown_prefers_markdown_field(self):
        msg = MultiPlatformMessage(title="T", content="用户名: a", markdown="用户名: a")
        self.assertEqual(msg.merged_markdown(), "## T\n\n用户名: a")

    def test_message_merged_markdown_falls_back_to_content(self):
        msg = MultiPlatformMessage(title="T", content="用户名: a")
        self.assertEqual(msg.merged_markdown(), "## T\n\n用户名: a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
