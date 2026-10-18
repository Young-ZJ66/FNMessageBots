"""渠道分发注册表（CHANNEL_SPECS + _dispatch_channels）的回归测试。

背景：普通事件 / 合并磁盘事件 / 系统通知三个入口历史上各抄了一份
「if 有参数 → 构造消息 → 发送 → 记录」的链条。加一个渠道要改三处，极易漏改
（表现为「某个入口就是不发这个渠道」）。现在集中登记，这里锁住：
- 注册表覆盖全部渠道，且 param_attr 都真实存在（拼错属性名会让渠道被静默跳过）
- 分发按注册表逐个投递，未配置的跳过
- 三个入口都走同一分发函数（源码级守卫，防止有人再抄一份链条）
"""

from __future__ import annotations

import re
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
    CHANNEL_SPECS,
    ChannelSpec,
    MultiPlatformMessage,
    MultiPlatformNotifier,
    reset_rich_text_rejected_channels,
)

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
MODULE_PATH = _ROOT / "src" / "notifier" / "multi_platform_notifier.py"

# 全部渠道的配置值（都填上，确保每个渠道都会被投递）
ALL_CONFIGURED = dict(
    wechat_webhook_url="https://example.test/wechat",
    dingtalk_webhook_url="https://example.test/dingtalk",
    feishu_webhook_url="https://example.test/feishu",
    webhook_params='{"url":"https://example.test/hook"}',
)


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        stub_deps.reset_fake_handler()
        reset_rich_text_rejected_channels()
        self.addCleanup(reset_rich_text_rejected_channels)
        self.root = _TMP_ROOT / f"registry_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.notifier = None

    def tearDown(self) -> None:
        if self.notifier is not None:
            try:
                self.notifier.close()
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self, **overrides) -> MultiPlatformNotifier:
        params = dict(ALL_CONFIGURED)
        params.update(overrides)
        self.notifier = MultiPlatformNotifier(
            title_prefix="飞牛NAS", dedup_window=300, pool_size=4, retries=1, timeout=5, **params
        )
        return self.notifier

    @staticmethod
    def requested_urls() -> list[str]:
        return [r["url"] for r in stub_deps.REQUEST_LOG]


class TestRegistryIntegrity(unittest.TestCase):
    def test_specs_cover_all_channels(self):
        keys = [s.key for s in CHANNEL_SPECS]
        self.assertEqual(len(keys), len(set(keys)), "渠道 key 不得重复")
        self.assertEqual(
            sorted(keys),
            ["dingtalk", "feishu", "webhook", "wechat"],
            "支持渠道收敛为企业微信/钉钉/飞书/通用 Webhook",
        )

    def test_every_param_attr_is_a_real_constructor_param(self):
        """param_attr 拼错会让渠道被静默跳过（永远不发），必须与构造参数严格对应。"""
        import inspect

        accepted = set(inspect.signature(MultiPlatformNotifier.__init__).parameters)
        for spec in CHANNEL_SPECS:
            with self.subTest(channel=spec.key):
                self.assertIn(
                    spec.param_attr,
                    accepted,
                    f"{spec.key} 的 param_attr={spec.param_attr} 不是构造参数，写入配置也不会生效",
                )

    def test_every_sender_exists(self):
        for spec in CHANNEL_SPECS:
            with self.subTest(channel=spec.key):
                self.assertTrue(
                    callable(getattr(MultiPlatformNotifier, spec.sender, None)),
                    f"{spec.key} 的 sender={spec.sender} 不存在",
                )

    def test_spec_is_immutable(self):
        spec = CHANNEL_SPECS[0]
        with self.assertRaises(Exception):
            spec.label = "x"  # type: ignore[misc]

    def test_labels_are_non_empty_and_distinct(self):
        labels = [s.label for s in CHANNEL_SPECS]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(all(x.strip() for x in labels))

    def test_channel_spec_defaults(self):
        spec = ChannelSpec("k", "L", "attr", "_send")
        self.assertFalse(spec.uses_bark_message)
        self.assertTrue(ChannelSpec("k", "L", "attr", "_send", True).uses_bark_message)


class TestDispatch(RegistryCase):
    def test_one_request_per_configured_channel(self):
        n = self.make()
        results, channel_results = n._dispatch_channels(
            MultiPlatformMessage(title="t", content="c"), log_prefix="测试"
        )
        self.assertEqual(len(channel_results), len(CHANNEL_SPECS), "每个渠道都应有一条结果")
        urls = self.requested_urls()
        self.assertEqual(
            len(urls),
            len(CHANNEL_SPECS),
            "每个已配置渠道都应发出一次 HTTP 请求",
        )
        # 各渠道最终请求地址与配置值不一定相同（Webhook 从 JSON 提取 url），
        # 因此按「可辨识的地址片段」核对，而不是比对原始配置值。
        expected_fragments = {
            "wechat": "example.test/wechat",
            "dingtalk": "example.test/dingtalk",
            "feishu": "example.test/feishu",
            "webhook": "example.test/hook",
        }
        for spec in CHANNEL_SPECS:
            with self.subTest(channel=spec.key):
                frag = expected_fragments[spec.key]
                self.assertTrue(
                    any(frag in u for u in urls), f"{spec.key} 未投递到含 {frag} 的地址；实际 {urls}"
                )

    def test_unconfigured_channels_are_skipped(self):
        n = self.make(
            dingtalk_webhook_url="",
            feishu_webhook_url="",
            webhook_params="",
        )
        results, channel_results = n._dispatch_channels(
            MultiPlatformMessage(title="t", content="c"), log_prefix="测试"
        )
        self.assertEqual(len(results), 1, "只配置了企业微信，应只发一个渠道")
        self.assertEqual(channel_results[0]["channel"], "企业微信")

    def test_results_align_with_channel_results(self):
        n = self.make()
        results, channel_results = n._dispatch_channels(
            MultiPlatformMessage(title="t", content="c"), log_prefix="测试"
        )
        self.assertEqual(len(results), len(channel_results))
        self.assertEqual(
            [c["channel"] for c in channel_results],
            [s.label for s in CHANNEL_SPECS],
            "结果顺序应与注册表顺序一致",
        )

    def test_dispatch_passes_same_message_to_all(self):
        """所有渠道收到同一个通用消息对象（标题都出现在请求 payload 中）。"""
        n = self.make()
        plain = MultiPlatformMessage(title="通用标题", content="通用正文")
        n._dispatch_channels(plain, log_prefix="测试")

        self.assertEqual(len(stub_deps.REQUEST_LOG), len(CHANNEL_SPECS))
        for rec in stub_deps.REQUEST_LOG:
            kw = rec["kwargs"]
            payload = kw.get("json_body") or kw.get("json") or kw.get("data") or b""
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            self.assertIn(
                "通用标题",
                str(payload),
                "所有渠道的请求 payload 应携带通用消息标题",
            )

    def test_all_three_entry_points_use_dispatch(self):
        """三个入口（普通/合并/系统通知）都必须经过注册表分发。"""
        src = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"self\._dispatch_channels\(", src)),
            3,
            "普通事件、合并磁盘事件、系统通知三个入口都应调用 _dispatch_channels",
        )

    def test_no_manual_dispatch_chain_left(self):
        """源码级守卫：不得再出现「if self.xxx_url: self._send_to_xxx(...)」式手抄链条。"""
        src = MODULE_PATH.read_text(encoding="utf-8")
        manual = re.findall(r"if self\.\w*(?:webhook_url|_params|_url):\n\s+ok, cr = self\._send_to_", src)
        self.assertEqual(manual, [], f"仍有手写分发链：{manual}")

    def test_extra_channels_hook_is_gone(self):
        src = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("_send_to_extra_channels", src, "旧的额外渠道钩子应由注册表取代")


class TestEntryPointsDispatchToAllChannels(RegistryCase):
    """端到端：三个入口都要把消息送到所有已配置渠道（原先最容易漏改的地方）。"""

    def entry_points(self, n):
        yield "系统通知", lambda: n.send_system_notification(
            "APP_START", "监控已启动", {"hostname": "fnos", "version": "2.6.0"}
        )
        yield "普通事件", lambda: n.send_notification(
            "LoginSucc", {"user": "admin", "IP": "192.168.1.10"}, "2026-01-01 12:00:00", "{}"
        )

    def test_system_and_plain_events_reach_every_channel(self):
        n = self.make()
        n.merge_window = 3600
        for label, call in self.entry_points(n):
            with self.subTest(entry=label):
                stub_deps.REQUEST_LOG.clear()
                n.sent_events.clear()
                call()
                urls = self.requested_urls()
                self.assertEqual(
                    len(urls),
                    len(CHANNEL_SPECS),
                    f"{label} 应投递到全部已配置渠道，实际 {len(urls)} 个",
                )

    def test_merged_disk_event_reaches_all_channels(self):
        n = self.make()
        n.merge_window = 1
        ok, channel_results = n._send_merged_disk_event(
            "DiskWakeup", [{"disk": "sda", "model": "WD", "serial": "S1"}], 0
        )
        self.assertTrue(ok)
        self.assertEqual(len(channel_results), len(CHANNEL_SPECS), "每个已配置渠道都应有发送结果")


if __name__ == "__main__":
    unittest.main(verbosity=2)
