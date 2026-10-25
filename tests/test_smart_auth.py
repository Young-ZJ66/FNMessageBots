"""smart_auth 授权命令生成的桩测试（不触真实设备/命令）。

运行方式（项目根目录）：
    python tests/test_smart_auth.py

覆盖：
- native：setcap/setfacl/验证命令按实际设备与账号生成；缺 smartctl 时补
  安装行；SAT 回退生效时验证命令带 -d sat；无设备时给提示而非空命令
- 容器：按 sysfs 盘名生成 compose devices 片段
- auth_needed：跟随巡检模块的 SMART_AUTH_NEEDED 标记
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor.patrol import disks, smart_auth  # noqa: E402


class _Ctx:
    """上下文替换助手：退出时自动还原。"""

    def __init__(self, **overrides):
        self._overrides = overrides
        self._orig = {}

    def __enter__(self):
        for name, value in self._overrides.items():
            self._orig[name] = getattr(smart_auth, name)
            setattr(smart_auth, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._orig.items():
            setattr(smart_auth, name, value)
        return False


def _patch_disks(**overrides):
    saved = {k: getattr(disks, k) for k in overrides}
    for k, v in overrides.items():
        setattr(disks, k, v)
    return saved


def _restore_disks(saved):
    for k, v in saved.items():
        setattr(disks, k, v)


class GenerateNativeCase(unittest.TestCase):
    def test_full_commands_with_devices(self):
        saved = _patch_disks(SAT_FALLBACK_USED=False)
        try:
            with _Ctx(
                list_smart_devices=lambda: ["/dev/sda", "/dev/sdb"],
                app_account=lambda: "FnMessageBot",
                is_container_mode=lambda: False,
                which=lambda name: "/usr/sbin/smartctl" if name == "smartctl" else None,
            ):
                text = smart_auth.generate_auth_commands()
        finally:
            _restore_disks(saved)
        self.assertIn('setcap cap_sys_rawio,cap_sys_admin+ep "$(command -v smartctl)"', text)
        self.assertIn("setfacl -m u:FnMessageBot:r -- /dev/sda /dev/sdb", text)
        self.assertIn("sudo -u FnMessageBot smartctl -A /dev/sda | grep -i temper", text)
        self.assertNotIn("-d sat", text)
        self.assertNotIn("apt-get install", text)

    def test_missing_smartctl_adds_install_line(self):
        saved = _patch_disks(SAT_FALLBACK_USED=False)
        try:
            with _Ctx(
                list_smart_devices=lambda: ["/dev/sda"],
                app_account=lambda: "FnMessageBot",
                is_container_mode=lambda: False,
                which=lambda name: None,
            ):
                text = smart_auth.generate_auth_commands()
        finally:
            _restore_disks(saved)
        self.assertIn("sudo apt-get install -y smartmontools libcap2-bin acl", text)

    def test_sat_fallback_adds_d_sat_to_verify(self):
        saved = _patch_disks(SAT_FALLBACK_USED=True)
        try:
            with _Ctx(
                list_smart_devices=lambda: ["/dev/sda"],
                app_account=lambda: "FnMessageBot",
                is_container_mode=lambda: False,
                which=lambda name: "/usr/sbin/smartctl",
            ):
                text = smart_auth.generate_auth_commands()
        finally:
            _restore_disks(saved)
        self.assertIn("smartctl -A -d sat /dev/sda", text)

    def test_no_devices_gives_hint_not_empty(self):
        saved = _patch_disks(SAT_FALLBACK_USED=False)
        try:
            with _Ctx(
                list_smart_devices=lambda: [],
                app_account=lambda: "FnMessageBot",
                is_container_mode=lambda: False,
                which=lambda name: "/usr/sbin/smartctl",
            ):
                text = smart_auth.generate_auth_commands()
        finally:
            _restore_disks(saved)
        self.assertIn("setcap", text)
        self.assertIn("/dev/sda", text)
        self.assertIn("按实际盘名", text)


class GenerateContainerCase(unittest.TestCase):
    def test_compose_devices_snippet(self):
        with _Ctx(
            is_container_mode=lambda: True,
            list_block_names_from_sysfs=lambda: ["nvme0n1", "sda", "sdb"],
        ):
            text = smart_auth.generate_auth_commands()
        self.assertIn("devices:", text)
        self.assertIn("      - /dev/sda:/dev/sda", text)
        self.assertIn("      - /dev/nvme0n1:/dev/nvme0n1", text)
        self.assertNotIn("setcap", text)

    def test_no_sysfs_names_gives_hint(self):
        with _Ctx(is_container_mode=lambda: True, list_block_names_from_sysfs=lambda: []):
            text = smart_auth.generate_auth_commands()
        self.assertIn("/sys/block", text)
        self.assertIn("devices", text)


class AuthNeededCase(unittest.TestCase):
    def test_follows_patrol_flag(self):
        saved = _patch_disks(SMART_AUTH_NEEDED=True)
        try:
            self.assertTrue(smart_auth.auth_needed())
        finally:
            _restore_disks(saved)
        saved = _patch_disks(SMART_AUTH_NEEDED=False)
        try:
            self.assertFalse(smart_auth.auth_needed())
        finally:
            _restore_disks(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
