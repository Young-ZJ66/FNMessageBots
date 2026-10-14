"""Web 访问控制回归测试：FNMB_DISABLE_AUTH 需要显式确认才生效。

运行方式（项目根目录）：
    python tests/test_web_access_guard.py

用子进程隔离环境变量，避免污染测试进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"


class EnvIsolatedCase(unittest.TestCase):
    OUT_PREFIX = "#OUT#"

    def run_snippet(self, body: str, env_extra: dict | None = None) -> str:
        """执行片段并只返回带 OUT_PREFIX 标记的结果行（避免与模块自身 print 混在一起）。"""
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join([str(_ROOT), str(_ROOT / "src"), str(_SUPPORT)])
        # 清掉可能存在的干扰变量，再按需要设置
        for key in ("FNMB_DISABLE_AUTH", "FNMB_DISABLE_AUTH_ACK"):
            env.pop(key, None)
        if env_extra:
            env.update(env_extra)
        code = textwrap.dedent(
            f"""
            import sys
            OUT = {self.OUT_PREFIX!r}
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
            # 子进程会打印中文（初始化码提示、鉴权关闭提示等）。不指定编码时
            # Windows 默认用 GBK 解码 UTF-8 输出，会 UnicodeDecodeError 或产生乱码。
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(_ROOT),
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(f"subprocess failed: {proc.stderr}")
        lines = [
            line[len(self.OUT_PREFIX):]
            for line in proc.stdout.splitlines()
            if line.startswith(self.OUT_PREFIX)
        ]
        return "\n".join(lines)

    def run_full_stdout(self, body: str, env_extra: dict | None = None) -> str:
        """返回完整 stdout（用于断言提示文案）。"""
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join([str(_ROOT), str(_ROOT / "src"), str(_SUPPORT)])
        for key in ("FNMB_DISABLE_AUTH", "FNMB_DISABLE_AUTH_ACK"):
            env.pop(key, None)
        if env_extra:
            env.update(env_extra)
        code = textwrap.dedent(
            f"""
            import sys
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
            # 子进程会打印中文（初始化码提示、鉴权关闭提示等）。不指定编码时
            # Windows 默认用 GBK 解码 UTF-8 输出，会 UnicodeDecodeError 或产生乱码。
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(_ROOT),
            timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(f"subprocess failed: {proc.stderr}")
        return proc.stdout


class TestAuthDisableRequiresAck(EnvIsolatedCase):
    PROBE = "from web.access_guard import auth_disabled_by_env; print(OUT + str(auth_disabled_by_env()))"

    def test_no_env_means_auth_enabled(self):
        self.assertEqual(self.run_snippet(self.PROBE), "False")

    def test_disable_without_ack_keeps_auth_enabled(self):
        out = self.run_snippet(self.PROBE, {"FNMB_DISABLE_AUTH": "1"})
        self.assertEqual(out, "False", "只设 FNMB_DISABLE_AUTH 不得关闭鉴权")

    def test_disable_without_ack_prints_hint(self):
        body = "from web.access_guard import auth_disabled_by_env; auth_disabled_by_env()"
        out = self.run_full_stdout(body, {"FNMB_DISABLE_AUTH": "1"})
        self.assertIn("FNMB_DISABLE_AUTH_ACK", out, "应提示需要显式确认变量")

    def test_disable_with_ack_takes_effect(self):
        out = self.run_snippet(self.PROBE, {"FNMB_DISABLE_AUTH": "1", "FNMB_DISABLE_AUTH_ACK": "1"})
        self.assertEqual(out, "True")

    def test_ack_alone_does_nothing(self):
        out = self.run_snippet(self.PROBE, {"FNMB_DISABLE_AUTH_ACK": "1"})
        self.assertEqual(out, "False")

    def test_truthy_variants(self):
        for value in ("true", "yes", "on", "TRUE", "1"):
            with self.subTest(value=value):
                out = self.run_snippet(
                    self.PROBE,
                    {"FNMB_DISABLE_AUTH": value, "FNMB_DISABLE_AUTH_ACK": value},
                )
                self.assertEqual(out, "True", f"{value!r} 应被视为开启")

    def test_non_truthy_values_ignored(self):
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                out = self.run_snippet(
                    self.PROBE,
                    {"FNMB_DISABLE_AUTH": value, "FNMB_DISABLE_AUTH_ACK": "1"},
                )
                self.assertEqual(out, "False", f"{value!r} 不应关闭鉴权")


class TestSetupCodeAndLimiter(EnvIsolatedCase):
    """初始化码与登录限流的既有行为不得被改动。"""

    def test_setup_code_roundtrip(self):
        body = """
            import shutil, uuid
            from pathlib import Path
            from web.access_guard import SetupCodeStore
            p = Path("tests/_tmp") / ("setup_" + uuid.uuid4().hex[:6]) / ".setup_code"
            store = SetupCodeStore(p)
            code = store.ensure()
            print(OUT + str(len(code)))
            print(OUT + str(store.verify(code.lower())))
            print(OUT + str(store.verify("WRONGCODE1")))
            store.clear()
            print(OUT + str(p.exists()))
            shutil.rmtree(p.parent, ignore_errors=True)
        """
        out = self.run_snippet(body).splitlines()
        self.assertEqual(out[0], "10")
        self.assertEqual(out[1], "True", "初始化码校验应忽略大小写")
        self.assertEqual(out[2], "False")
        self.assertEqual(out[3], "False", "设置密码后应清除初始化码文件")

    def test_login_limiter_locks_after_threshold(self):
        body = """
            from web.access_guard import LoginRateLimiter, LOGIN_MAX_FAILURES
            lim = LoginRateLimiter()
            for _ in range(LOGIN_MAX_FAILURES):
                lim.record_failure("1.2.3.4")
            print(OUT + str(lim.retry_after("1.2.3.4") > 0))
            print(OUT + str(lim.retry_after("5.6.7.8") > 0))
            lim.reset("1.2.3.4")
            print(OUT + str(lim.retry_after("1.2.3.4") > 0))
        """
        out = self.run_snippet(body).splitlines()
        self.assertEqual(out[0], "True", "达到阈值后应锁定该来源")
        self.assertEqual(out[1], "False", "不得连带锁定其它来源")
        self.assertEqual(out[2], "False", "reset 后应解除锁定")


if __name__ == "__main__":
    unittest.main(verbosity=2)
