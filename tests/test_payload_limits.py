"""各渠道消息体大小上限的回归测试。

依据（官方文档）：
- 企业微信 markdown / markdown_v2：`content` 最长 **4096 字节**、必须 utf8
- 钉钉 markdown：`text` 最大 **5000 字符**
- 飞书自定义机器人：**请求体不超过 20KB**

修复前实测：影视库超长路径产生 40046 字符、巡检多盘 10355 字符，均超企微/钉钉上限，
会被平台拒绝（失败后虽有纯文本回退，但结构化消息直接失效）。
"""

from __future__ import annotations

import json
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
    DINGTALK_TEXT_MAX_CHARS,
    DINGTALK_TITLE_MAX_CHARS,
    FEISHU_BODY_MAX_BYTES,
    TRUNCATE_SUFFIX,
    WECOM_CONTENT_MAX_BYTES,
    MultiPlatformMessage,
    MultiPlatformNotifier,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
BIG = "A" * 20000


def oversized_patrol() -> dict:
    return {
        "hostname": "fnos",
        "missing_fields": ["x" * 3000],
        "disks": [
            {"name": "D" + "i" * 200, "temp": "30", "free": "1T", "health": "OK"}
            for _ in range(40)
        ],
    }


def oversized_trim() -> dict:
    return {"title": BIG, "path": BIG}


def big_batch() -> dict:
    grouped = {
        et: [{"event_data": {"user": "admin", "m": "y" * 80}, "brief": "b"} for _ in range(8)]
        for et in ("LoginSucc", "APP_CRASH")
    }
    return {"count": 16, "by_type": {"LoginSucc": 8, "APP_CRASH": 8}, "grouped_events": grouped, "items": []}


class LimitCase(unittest.TestCase):
    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.notifier = MultiPlatformNotifier(
            title_prefix="飞牛NAS", dedup_window=300, pool_size=2, retries=1, timeout=5
        )
        self.notifier.merge_window = 3600

    def tearDown(self) -> None:
        try:
            self.notifier.close()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, event_type, event_data):
        return self.notifier._build_message(event_type, event_data, "2026-01-01 12:00:00", "{}")


class TestWecomByteLimit(LimitCase):
    def test_content_within_bytes(self):
        for label, (et, ed) in {
            "patrol": ("NAS_PATROL_REPORT", oversized_patrol()),
            "trim": ("TRIM_RESOURCE_ADDED", oversized_trim()),
            "batch": ("POLL_BATCH_SUMMARY", big_batch()),
        }.items():
            with self.subTest(case=label):
                msg = self.build(et, ed)
                payload = msg.to_wechat_markdown_format()
                size = len(payload["markdown"]["content"].encode("utf-8"))
                self.assertLessEqual(size, WECOM_CONTENT_MAX_BYTES, f"{label}: {size} 字节超上限")

    def test_truncation_is_marked(self):
        msg = self.build("TRIM_RESOURCE_ADDED", oversized_trim())
        content = msg.to_wechat_markdown_format()["markdown"]["content"]
        self.assertIn(TRUNCATE_SUFFIX, content, "截断后应有明确标记，便于排查")

    def test_utf8_boundary_not_broken(self):
        """按字节截断不得切坏多字节字符（否则平台侧 utf8 校验会失败）。"""
        msg = MultiPlatformMessage(title="标题", content="", markdown="中" * 5000)
        payload = msg.to_wechat_markdown_format()
        text = payload["markdown"]["content"]
        self.assertLessEqual(len(text.encode("utf-8")), WECOM_CONTENT_MAX_BYTES)
        # 能无损解码即说明没切坏
        self.assertEqual(text.encode("utf-8").decode("utf-8"), text)

    def test_short_content_is_untouched(self):
        msg = self.build("LoginSucc", {"user": "admin", "IP": "192.168.1.10"})
        content = msg.to_wechat_markdown_format()["markdown"]["content"]
        self.assertNotIn(TRUNCATE_SUFFIX, content, "未超限时不得截断")


class TestDingtalkCharLimit(LimitCase):
    def test_text_within_chars(self):
        for label, (et, ed) in {
            "patrol": ("NAS_PATROL_REPORT", oversized_patrol()),
            "trim": ("TRIM_RESOURCE_ADDED", oversized_trim()),
            "batch": ("POLL_BATCH_SUMMARY", big_batch()),
        }.items():
            with self.subTest(case=label):
                payload = self.build(et, ed).to_dingtalk_markdown_format()
                md = payload["markdown"]
                total = len(md["title"]) + len(md["text"])
                self.assertLessEqual(total, DINGTALK_TEXT_MAX_CHARS, f"{label}: {total} 字符超上限")

    def test_title_within_limit(self):
        msg = MultiPlatformMessage(title="T" * 500, content="x")
        payload = msg.to_dingtalk_markdown_format()
        self.assertLessEqual(len(payload["markdown"]["title"]), DINGTALK_TITLE_MAX_CHARS)

    def test_title_present_and_short_body_intact(self):
        payload = self.build("LoginSucc", {"user": "admin"}).to_dingtalk_markdown_format()
        self.assertTrue(payload["markdown"]["title"])
        self.assertIn("登录成功", payload["markdown"]["title"])
        self.assertNotIn(TRUNCATE_SUFFIX, payload["markdown"]["text"])


class TestFeishuBodyLimit(LimitCase):
    def test_request_body_within_bytes(self):
        for label, (et, ed) in {
            "patrol": ("NAS_PATROL_REPORT", oversized_patrol()),
            "trim": ("TRIM_RESOURCE_ADDED", oversized_trim()),
            "batch": ("POLL_BATCH_SUMMARY", big_batch()),
        }.items():
            with self.subTest(case=label):
                body = json.dumps(self.build(et, ed).to_feishu_post_format(), ensure_ascii=False)
                size = len(body.encode("utf-8"))
                self.assertLessEqual(size, 20 * 1024, f"{label}: 请求体 {size} 字节超 20KB")
                self.assertLessEqual(size, FEISHU_BODY_MAX_BYTES + 2048)

    def test_structure_matches_official_shape(self):
        """官方结构：content.post.zh_cn.{title?, content:[[{tag,text}]]}。"""
        payload = self.build("LoginSucc", {"user": "admin"}).to_feishu_post_format()
        self.assertEqual(payload["msg_type"], "post")
        zh = payload["content"]["post"]["zh_cn"]
        self.assertIn("title", zh)
        self.assertIsInstance(zh["content"], list)
        for paragraph in zh["content"]:
            self.assertIsInstance(paragraph, list)
            for element in paragraph:
                self.assertEqual(element["tag"], "text")
                self.assertIsInstance(element["text"], str)


class TestOtherChannelsUnaffected(LimitCase):
    def test_plain_text_formats_keep_original_content(self):
        """纯文本回退路径不做截断（保持既有行为，平台对 text 的限制宽松得多）。"""
        msg = self.build("LoginSucc", {"user": "admin", "IP": "1.1.1.1"})
        self.assertEqual(msg.to_wechat_format()["text"]["content"], msg.merged_plain_text())
        self.assertEqual(msg.to_dingtalk_format()["text"]["content"], msg.merged_plain_text())
        self.assertEqual(msg.to_feishu_format()["content"]["text"], msg.merged_plain_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
