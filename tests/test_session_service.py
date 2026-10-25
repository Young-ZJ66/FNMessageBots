"""web/session_service 会话语义测试（模拟时钟，不触真实 Web）。

运行方式（项目根目录）：
    python tests/test_session_service.py

锁定：真实交互（touch）续期、后台读取（remaining）不续期、作用域隔离
（根路径与网关前缀 cookie 互不串扰）、过期不可复活、delete 删除。
时钟一律相对：create 固定在 t=1000，后续时间以偏移给出。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "src"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from web import session_service as ss  # noqa: E402

_BASE = 1000.0


class SessionServiceCase(unittest.TestCase):
    def setUp(self):
        ss._sessions.clear()
        ss._last_cleanup = 0.0

    def _create(self, idle_seconds=900, scope="/"):
        with patch.object(ss.time, "time", return_value=_BASE):
            return ss.create_session(idle_seconds=idle_seconds, scope=scope)

    def _at(self, offset: float):
        return patch.object(ss.time, "time", return_value=_BASE + offset)

    def test_touch_renews_and_expiry_kills_session(self):
        sid = self._create()
        with self._at(899):
            self.assertTrue(ss.touch_session(sid, 900, scope="/"))
        # 续期把活跃时间推到 1899；再过 899 秒仍有效
        with self._at(1798):
            self.assertTrue(ss.touch_session(sid, 900, scope="/"))
        # 再过 900 秒（now - last >= idle）：过期且不可复活
        with self._at(2698 + 900):
            self.assertFalse(ss.touch_session(sid, 900, scope="/"))
        with self._at(2698 + 900):
            self.assertEqual(ss.session_remaining_seconds(sid, 900, scope="/"), 0.0)

    def test_remaining_does_not_renew(self):
        sid = self._create()
        with self._at(100):
            self.assertGreater(ss.session_remaining_seconds(sid, 900, scope="/"), 700)
        # 读取不续期：直接跳过过期线（创建后 1000 秒 > 900）
        with self._at(1000):
            self.assertEqual(ss.session_remaining_seconds(sid, 900, scope="/"), 0.0)

    def test_scope_isolation(self):
        root_sid = self._create(scope="/")
        gw_sid = self._create(scope="/app/FnMessageBot/")
        # 根路径会话不能在网关作用域下使用，反之亦然
        with self._at(10):
            self.assertTrue(ss.touch_session(root_sid, 900, scope="/"))
            self.assertFalse(ss.touch_session(root_sid, 900, scope="/app/FnMessageBot/"))
            self.assertTrue(ss.touch_session(gw_sid, 900, scope="/app/FnMessageBot/"))
            self.assertFalse(ss.touch_session(gw_sid, 900, scope="/"))
            self.assertEqual(ss.session_remaining_seconds(gw_sid, 900, scope="/"), 0.0)

    def test_delete_and_scope_delete(self):
        sid = self._create(scope="/app/FnMessageBot/")
        # 作用域不匹配的删除不生效
        ss.delete_session(sid, scope="/")
        with self._at(10):
            self.assertTrue(ss.touch_session(sid, 900, scope="/app/FnMessageBot/"))
        ss.delete_session(sid, scope="/app/FnMessageBot/")
        with self._at(10):
            self.assertFalse(ss.touch_session(sid, 900, scope="/app/FnMessageBot/"))

    def test_expired_session_removed_from_store(self):
        sid = self._create(idle_seconds=10, scope="/")
        with self._at(11):
            self.assertFalse(ss.touch_session(sid, 10, scope="/"))
        self.assertNotIn(sid, ss._sessions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
