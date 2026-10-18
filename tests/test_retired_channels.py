"""已移除渠道（魔法推送、企业微信应用）的残留配置必须优雅降级。

用户升级后 config.json 里往往还留着 `magic_push_params` / `wecom_app_params`。
要求：**不报错、不丢配置**（键原样留在文件里）、启动时提示一次「已忽略」，
以免用户以为还在推送。
"""

from __future__ import annotations

import json
import logging
import os
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

import config as config_mod  # noqa: E402
from config import Config  # noqa: E402
from notifier.multi_platform_notifier import MultiPlatformNotifier  # noqa: E402

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

RETIRED = {
    "magic_push_params": '{"base_url":"https://magic.example","token":"secret-token"}',
    "wecom_app_params": '{"corp_id":"c1","corp_secret":"s1","agent_id":"1"}',
}


def minimal_config(extra: dict | None = None) -> dict:
    data = {
        "monitor_events": ["LoginSucc"],
        "logger_db_path": "",
        "cursor_dir": "",
    }
    data.update(extra or {})
    return data


class RetiredChannelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"retired_{uuid.uuid4().hex[:8]}"
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.cfg_file = self.root / "config" / "config.json"
        # _get_config_file_path() 优先读 APP_HOME
        self._old_app_home = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(self.root)
        self.captured: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = self.captured.append  # type: ignore[method-assign]
        self.logger = logging.getLogger("config")
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
        self._cleanup = lambda: self.logger.removeHandler(handler)

    def tearDown(self) -> None:
        self._cleanup()
        if self._old_app_home is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = self._old_app_home
        shutil.rmtree(self.root, ignore_errors=True)

    def write_cfg(self, data: dict) -> None:
        self.cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def build(self) -> Config:
        c = Config.__new__(Config)
        c._env_set_keys = set()
        c._load_from_file_skip_if_set()
        return c

    def messages(self) -> str:
        return "\n".join(str(r.getMessage()) for r in self.captured)


class TestRetiredKeysIgnored(RetiredChannelCase):
    def test_loading_config_with_retired_keys_does_not_raise(self):
        self.write_cfg(minimal_config(RETIRED))
        c = self.build()  # 不应抛异常
        self.assertFalse(hasattr(c, "magic_push_params"), "字段应已从 dataclass 移除")
        self.assertFalse(hasattr(c, "wecom_app_params"))

    def test_retired_keys_are_reported(self):
        self.write_cfg(minimal_config(RETIRED))
        self.build()
        msg = self.messages()
        self.assertIn("magic_push_params", msg)
        self.assertIn("wecom_app_params", msg)
        self.assertIn("已移除", msg)
        self.assertIn("可安全删除", msg)

    def test_absent_retired_keys_do_not_log(self):
        self.write_cfg(minimal_config())
        self.build()
        self.assertNotIn("已移除", self.messages(), "没配过就不该提示")

    def test_empty_retired_values_do_not_log(self):
        self.write_cfg(minimal_config({k: "" for k in RETIRED}))
        self.build()
        self.assertNotIn("已移除", self.messages())

    def test_other_channels_still_load(self):
        data = minimal_config({
            **RETIRED,
            "wechat_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
            "webhook_params": '{"url":"https://example.test/hook"}',
            "meow_params": '{"nickname":"me"}',
        })
        self.write_cfg(data)
        c = self.build()
        self.assertTrue(c.wechat_webhook_url)
        self.assertIn("example.test", c.webhook_params)
        self.assertIn("me", c.meow_params)

    def test_config_file_is_not_rewritten(self):
        """只提示、不改用户的文件（避免升级即改动用户数据）。"""
        self.write_cfg(minimal_config(RETIRED))
        before = self.cfg_file.read_text(encoding="utf-8")
        self.build()
        self.assertEqual(self.cfg_file.read_text(encoding="utf-8"), before)


class TestRetiredEnvVars(RetiredChannelCase):
    def test_env_vars_reported_and_ignored(self):
        for key in ("magic_push_params", "wecom_app_params"):
            self.assertFalse(hasattr(Config(), key))
        os.environ["WECOM_APP_PARAMS"] = '{"corp_id":"x"}'
        os.environ["MAGIC_PUSH_PARAMS"] = '{"token":"y"}'
        try:
            c = Config.__new__(Config)
            c._env_set_keys = set()
            c._load_from_env()
            msg = self.messages()
            self.assertIn("WECOM_APP_PARAMS", msg)
            self.assertIn("MAGIC_PUSH_PARAMS", msg)
            self.assertFalse(hasattr(c, "wecom_app_params"))
            self.assertFalse(hasattr(c, "magic_push_params"))
        finally:
            os.environ.pop("WECOM_APP_PARAMS", None)
            os.environ.pop("MAGIC_PUSH_PARAMS", None)


class TestNotifierRejectsRemovedChannels(RetiredChannelCase):
    def test_constructor_no_longer_accepts_removed_params(self):
        with self.assertRaises(TypeError):
            MultiPlatformNotifier(magic_push_params="{}")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            MultiPlatformNotifier(wecom_app_params="{}")  # type: ignore[call-arg]
        # 渠道收敛：Bark/PushPlus/SMTP/MeoW 也从签名中移除
        for gone in ("bark_url", "pushplus_params", "smtp_params", "meow_params"):
            with self.subTest(channel=gone):
                with self.assertRaises(TypeError):
                    MultiPlatformNotifier(**{gone: "x"})  # type: ignore[arg-type]

    def test_remaining_channels_construct(self):
        n = MultiPlatformNotifier(
            wechat_webhook_url="https://example.test/w",
            dingtalk_webhook_url="https://example.test/d",
            feishu_webhook_url="https://example.test/f",
            webhook_params='{"url":"https://example.test/h"}',
        )
        try:
            active = n.get_delivery_health()["active_platforms"]
            self.assertEqual(
                sorted(active),
                ["dingtalk", "feishu", "webhook", "wechat"],
            )
            self.assertNotIn("magic_push", active)
            self.assertNotIn("wecom_app", active)
        finally:
            n.close()


class TestChannelTypeRegistry(unittest.TestCase):
    """渠道清单在多处各有一份副本，必须一致（否则「界面能选但存不下」或「存了不生效」）。

    说明：这里不 `import web.ui_app`——测试用的 flask 桩不支持 `from flask import Flask`，
    导入会失败（即 web UI 模块目前无测试覆盖）。故对 ui_app 做源码级解析。
    """

    @staticmethod
    def _ui_channel_ids() -> list[str]:
        import re

        src = (_ROOT / "src" / "web" / "ui_app.py").read_text(encoding="utf-8")
        block = re.search(r"CHANNEL_OPTIONS\s*=\s*\[(.*?)\]", src, re.S)
        assert block is not None, "未找到 CHANNEL_OPTIONS"
        return re.findall(r'"id"\s*:\s*"([^"]+)"', block.group(1))

    def test_config_store_keys_have_no_removed_channels(self):
        from web.config_store import CHANNEL_TYPE_KEYS

        types = [t for t, _ in CHANNEL_TYPE_KEYS]
        for gone in ("magic_push", "wecom_app"):
            self.assertNotIn(gone, types, f"{gone} 应已从 CHANNEL_TYPE_KEYS 移除")

    def test_ui_channel_options_have_no_removed_channels(self):
        ids = self._ui_channel_ids()
        self.assertTrue(ids, "解析 CHANNEL_OPTIONS 失败")
        for gone in ("magic_push", "wecom_app"):
            self.assertNotIn(gone, ids, f"{gone} 应已从 CHANNEL_OPTIONS 移除")

    def test_ui_and_config_store_agree(self):
        from web.config_store import CHANNEL_TYPE_KEYS

        ui_ids = set(self._ui_channel_ids())
        store_ids = {t for t, _ in CHANNEL_TYPE_KEYS}
        self.assertEqual(
            ui_ids,
            store_ids,
            "Web 配置页的渠道列表与写入 config.json 的映射必须一一对应",
        )

    def test_index_html_has_no_removed_channel_widgets(self):
        html = (_ROOT / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
        for gone in ("magic_push", "wecom_app", "magic-push", "wecom-"):
            self.assertNotIn(gone, html, f"index.html 仍残留 {gone} 相关代码")

    def test_validator_rejects_removed_types(self):
        from notifier.channel_params import validate_channel_json

        for gone in ("magic_push", "wecom_app"):
            with self.subTest(channel=gone):
                with self.assertRaises(KeyError):
                    validate_channel_json(gone, "{}")


class TestConfigFileEncoding(unittest.TestCase):
    """config.json 必须按 UTF-8 读写。

    项目自带的 config.json 含中文（`title_prefix: "飞牛NAS"`）；旧实现用 open(path, "r")
    且把异常吞成一行 print，在 Windows(GBK) 下会导致**整个文件配置被静默忽略**。
    """

    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"cfgenc_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "config.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_reads_utf8_config_with_chinese(self):
        data = {"title_prefix": "飞牛NAS", "monitor_events": ["登录成功"]}
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out, err = Config._read_json_config(self.path)
        self.assertEqual(err, "")
        self.assertEqual(out["title_prefix"], "飞牛NAS")

    def test_falls_back_to_legacy_gbk_file(self):
        """历史上可能是 GBK 写出的文件，回退平台默认编码仍应读出来。"""
        data = {"title_prefix": "飞牛NAS"}
        self.path.write_bytes(json.dumps(data, ensure_ascii=False).encode("gbk"))
        out, err = Config._read_json_config(self.path)
        self.assertEqual(err, "", f"GBK 文件应能回退读取: {err}")
        self.assertEqual(out["title_prefix"], "飞牛NAS")

    def test_bad_json_reports_error_instead_of_silence(self):
        self.path.write_text("{not json", encoding="utf-8")
        out, err = Config._read_json_config(self.path)
        self.assertIsNone(out)
        self.assertIn("JSON", err)

    def test_non_object_top_level_rejected(self):
        self.path.write_text("[1,2,3]", encoding="utf-8")
        out, err = Config._read_json_config(self.path)
        self.assertIsNone(out)
        self.assertIn("对象", err)

    def test_missing_file_reports_error(self):
        out, err = Config._read_json_config(self.root / "nope.json")
        self.assertIsNone(out)
        self.assertIn("读取失败", err)

    def test_repo_config_json_is_utf8_readable(self):
        """仓库自带 config.json 必须能被 UTF-8 读出来（含中文）。"""
        repo_cfg = _ROOT / "config" / "config.json"
        out, err = Config._read_json_config(repo_cfg)
        self.assertEqual(err, "", f"自带 config.json 读取失败: {err}")
        self.assertEqual(out.get("title_prefix"), "飞牛NAS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
