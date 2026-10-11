"""内置测试消息回归测试：/api/test 不再要求用户输入内容。

运行方式（项目根目录）：
    python tests/test_built_in_test_message.py

直接测试 ui_app.resolve_test_push_content 的解析规则（Flask 桩不支撑完整
test_client 交互，路由层仅做一层透传）。使用 tests/_support/stub_deps.py，不联网。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

from web.ui_app import BUILT_IN_TEST_MESSAGE, resolve_test_push_content  # noqa: E402


class ResolveTestPushContentCase(unittest.TestCase):
    def test_empty_dict_uses_builtin(self):
        self.assertEqual(resolve_test_push_content({}), BUILT_IN_TEST_MESSAGE)

    def test_none_uses_builtin(self):
        self.assertEqual(resolve_test_push_content(None), BUILT_IN_TEST_MESSAGE)

    def test_blank_content_uses_builtin(self):
        self.assertEqual(resolve_test_push_content({"content": "   "}), BUILT_IN_TEST_MESSAGE)

    def test_explicit_content_strips_and_wins(self):
        self.assertEqual(resolve_test_push_content({"content": " 自定义测试内容 "}), "自定义测试内容")

    def test_builtin_message_is_meaningful(self):
        self.assertIn("FnMessageBot 测试推送", BUILT_IN_TEST_MESSAGE)
        self.assertNotRegex(
            BUILT_IN_TEST_MESSAGE,
            r"[\U0001F300-\U0001FAFF\u2600-\u27BF]",
            "内置消息不得携带 emoji",
        )


if __name__ == "__main__":
    unittest.main()
