"""Config 层回归测试：新增配置项的默认值、环境变量优先级、热加载与校验回滚。

运行方式（项目根目录）：
    python tests/test_config_fields.py

每个用例用子进程隔离（Config 在 __post_init__ 读取环境变量与进程 cwd）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import unittest
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"

NEW_BOOL_FIELDS = (
    "push_markdown_enabled",
    "push_emoji_enabled",
    "resume_from_cursor_on_start",
)


class ConfigCase(unittest.TestCase):
    OUT = "#OUT#"

    def make_app_home(self, config_body: str | None = None) -> Path:
        home = _TMP_ROOT / f"cfg_{uuid.uuid4().hex[:8]}"
        (home / "config").mkdir(parents=True, exist_ok=True)
        (home / "data" / "logs").mkdir(parents=True, exist_ok=True)
        (home / "data" / "cursor").mkdir(parents=True, exist_ok=True)
        if config_body is not None:
            (home / "config" / "config.json").write_text(config_body, encoding="utf-8")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def run_snippet(self, body: str, env_extra: dict | None = None) -> str:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 统一子进程 stdout 编码，避免中文/非 ASCII 在 Windows 控制台被按 GBK 解码后断言失败
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = os.pathsep.join([str(_ROOT), str(_ROOT / "src"), str(_SUPPORT)])
        for key in (
            "PUSH_MARKDOWN_ENABLED",
            "PUSH_EMOJI_ENABLED",
            "RESUME_FROM_CURSOR_ON_START",
            "MINIMAL_PUSH_ENABLED",
        ):
            env.pop(key, None)
        if env_extra:
            env.update(env_extra)
        code = textwrap.dedent(
            f"""
            import sys
            OUT = {self.OUT!r}
            sys.path.insert(0, {str(_SUPPORT)!r})
            sys.path.insert(0, {str(_ROOT / "src")!r})
            import stub_deps; stub_deps.install()
            {body}
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(_ROOT),
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(f"subprocess failed: {proc.stderr}")
        # 只取带标记的结果行；无标记行时回退整段 stdout（便于排查）
        marked = [
            line[len(self.OUT):]
            for line in proc.stdout.splitlines()
            if line.startswith(self.OUT)
        ]
        return "\n".join(marked) if marked else proc.stdout.strip()


class TestDefaults(ConfigCase):
    def test_new_fields_defaults_with_empty_config(self):
        home = self.make_app_home("{}")
        out = self.run_snippet(
            """
            from config import Config
            c = Config()
            print(OUT + str(c.push_markdown_enabled))
            print(OUT + str(c.push_emoji_enabled))
            print(OUT + str(c.resume_from_cursor_on_start))
            """,
            {"APP_HOME": str(home)},
        ).splitlines()
        self.assertEqual(out, ["True", "False", "False"],
                         "结构化默认开启；emoji 默认关闭；游标续推默认关闭")

    def test_reader_defaults_when_key_absent(self):
        """config.json 无这些键时，仍应取到与 dataclass 一致的默认值。"""
        home = self.make_app_home('{"title_prefix": "我的NAS"}')
        out = self.run_snippet(
            """
            from config import Config
            c = Config()
            # 避免在控制台打印非 ASCII（Windows 终端编码会干扰断言）
            print(OUT + str(c.title_prefix == "我的NAS"))
            print(OUT + str(c.push_markdown_enabled))
            print(OUT + str(c.push_emoji_enabled))
            """,
            {"APP_HOME": str(home)},
        ).splitlines()
        self.assertEqual(out, ["True", "True", "False"])


class TestFileValues(ConfigCase):
    def test_explicit_false_values_are_respected(self):
        home = self.make_app_home(
            '{"monitor_events": ["LoginSucc"], "push_markdown_enabled": false,'
            ' "push_emoji_enabled": true, "resume_from_cursor_on_start": true}'
        )
        out = self.run_snippet(
            """
            from config import Config
            c = Config()
            print(OUT + str(c.push_markdown_enabled))
            print(OUT + str(c.push_emoji_enabled))
            print(OUT + str(c.resume_from_cursor_on_start))
            """,
            {"APP_HOME": str(home)},
        ).splitlines()
        self.assertEqual(out, ["False", "True", "True"])

    def test_string_truthy_values(self):
        home = self.make_app_home(
            '{"monitor_events": ["LoginSucc"], "push_markdown_enabled": "1",'
            ' "push_emoji_enabled": "yes", "resume_from_cursor_on_start": "on"}'
        )
        out = self.run_snippet(
            """
            import config as _cfg, os, json
            from pathlib import Path
            from config import Config
            c = Config()
            cfg_path = Path(os.environ["APP_HOME"]) / "config" / "config.json"
            print(OUT + "file=" + str(cfg_path.read_text(encoding="utf-8").count("push_markdown_enabled")))
            print(OUT + "mod=" + _cfg.__file__)
            print(OUT + "raw=" + repr(json.loads(cfg_path.read_text(encoding="utf-8"))["push_markdown_enabled"]))
            print(OUT + f"{c.push_markdown_enabled},{c.push_emoji_enabled},{c.resume_from_cursor_on_start}")
            """,
            {"APP_HOME": str(home)},
        )
        self.assertEqual(out.splitlines()[-1], "True,True,True", out)


class TestEnvOverride(ConfigCase):
    def test_env_wins_over_file(self):
        home = self.make_app_home(
            '{"push_markdown_enabled": true, "push_emoji_enabled": false,'
            ' "resume_from_cursor_on_start": true}'
        )
        out = self.run_snippet(
            """
            from config import Config
            c = Config()
            print(OUT + f"{c.push_markdown_enabled},{c.push_emoji_enabled},{c.resume_from_cursor_on_start}")
            """,
            {
                "APP_HOME": str(home),
                "PUSH_MARKDOWN_ENABLED": "0",
                "PUSH_EMOJI_ENABLED": "1",
                "RESUME_FROM_CURSOR_ON_START": "false",
            },
        )
        self.assertEqual(out, "False,True,False", "环境变量必须优先于 config.json")


class TestReloadFromFile(ConfigCase):
    def test_reload_applies_new_fields(self):
        home = self.make_app_home("{}")
        out = self.run_snippet(
            """
            import json
            from pathlib import Path
            from config import Config
            c = Config()
            before = (c.push_markdown_enabled, c.push_emoji_enabled, c.resume_from_cursor_on_start)
            path = Path(__file__).resolve().parent if False else None
            import os
            cfg = Path(os.environ["APP_HOME"]) / "config" / "config.json"
            data = json.loads(cfg.read_text(encoding="utf-8"))
            data.update({"push_markdown_enabled": False, "push_emoji_enabled": True,
                         "resume_from_cursor_on_start": True, "monitor_events": ["LoginSucc"]})
            cfg.write_text(json.dumps(data), encoding="utf-8")
            ok = c.reload_from_file(cfg)
            print(OUT + str(ok))
            print(OUT + f"{c.push_markdown_enabled},{c.push_emoji_enabled},{c.resume_from_cursor_on_start}")
            print(OUT + str(before))
            """,
            {"APP_HOME": str(home)},
        ).splitlines()
        self.assertEqual(out[0], "True", "合法配置应热加载成功")
        self.assertEqual(out[1], "False,True,True")
        self.assertEqual(out[2], "(True, False, False)", "热加载前应为默认值")

    def test_reload_keeps_old_values_when_validation_fails(self):
        """monitor_events 被清空属于非法配置：应回滚，不得留下半更新状态。"""
        home = self.make_app_home('{"monitor_events": ["LoginSucc"]}')
        out = self.run_snippet(
            """
            import json, os
            from pathlib import Path
            from config import Config
            c = Config()
            cfg = Path(os.environ["APP_HOME"]) / "config" / "config.json"
            data = json.loads(cfg.read_text(encoding="utf-8"))
            data.update({"push_emoji_enabled": True, "monitor_events": []})
            cfg.write_text(json.dumps(data), encoding="utf-8")
            ok = c.reload_from_file(cfg)
            print(OUT + str(ok))
            print(OUT + f"{c.push_emoji_enabled},{c.monitor_events}")
            """,
            {"APP_HOME": str(home)},
        ).splitlines()
        self.assertEqual(out[0], "False", "非法配置不得热加载成功")
        self.assertEqual(out[1], "False,['LoginSucc']", "必须整体回滚到热加载前的值")


class TestEnvSkipsReload(ConfigCase):
    def test_env_set_field_is_not_overwritten_by_reload(self):
        home = self.make_app_home("{}")
        out = self.run_snippet(
            """
            import json, os
            from pathlib import Path
            from config import Config
            c = Config()
            cfg = Path(os.environ["APP_HOME"]) / "config" / "config.json"
            data = json.loads(cfg.read_text(encoding="utf-8"))
            data.update({"push_emoji_enabled": True, "monitor_events": ["LoginSucc"]})
            cfg.write_text(json.dumps(data), encoding="utf-8")
            c.reload_from_file(cfg)
            print(OUT + str(c.push_emoji_enabled))
            """,
            {"APP_HOME": str(home), "PUSH_EMOJI_ENABLED": "false"},
        )
        self.assertEqual(out, "False", "环境变量设置过的项不得被文件热加载覆盖")


if __name__ == "__main__":
    unittest.main(verbosity=2)
