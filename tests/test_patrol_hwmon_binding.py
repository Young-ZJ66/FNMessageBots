"""patrol/disks.py hwmon 设备绑定的桩测试（假 sysfs 树，不触真实 /sys）。

运行方式（项目根目录）：
    python tests/test_patrol_hwmon_binding.py

背景（对齐上游修复）：此前 /sys/class/hwmon 按 name∈{drivetemp,nvme,sata}
泛配——多盘机器启用 drivetemp 时所有盘都会收集到全部 hwmon 条目，排序后
统一显示 hwmon0 的温度。现改为 sysfs 设备链接绑定（realpath 归属），
仅收集真实属于该盘/该 NVMe 控制器的传感器。

说明：假树用真实目录模拟 /sys/class 布局（resolve 对普通目录返回自身，
归属判定语义不变），不依赖符号链接——Windows 开发环境无 symlink 权限。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor.patrol import disks  # noqa: E402


class _FakeSysfs:
    """最小 sysfs 树：class/block、class/nvme、class/hwmon 均为真实目录。"""

    def __init__(self, root: Path):
        self.root = root
        (root / "class" / "block").mkdir(parents=True)
        (root / "class" / "nvme").mkdir(parents=True)
        (root / "class" / "hwmon").mkdir(parents=True)

    def add_block(self, name: str) -> Path:
        d = self.root / "class" / "block" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_nvme_ctrl(self, ctrl: str) -> Path:
        d = self.root / "class" / "nvme" / ctrl
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_hwmon(self, index: int, host_dir: Path, millideg: int) -> Path:
        hw = host_dir / "hwmon" / f"hwmon{index}"
        hw.mkdir(parents=True, exist_ok=True)
        (hw / "temp1_input").write_text(str(millideg), encoding="utf-8")
        return hw

    def add_class_hwmon_with_device(self, index: int, millideg: int) -> Path:
        """模拟 class/hwmon/hwmonN（带 device 子目录的真身形态）。"""
        hw = self.root / "class" / "hwmon" / f"hwmon{index}"
        (hw / "device").mkdir(parents=True, exist_ok=True)
        (hw / "temp1_input").write_text(str(millideg), encoding="utf-8")
        return hw


class DiskHwmonDirsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fs = _FakeSysfs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_sata_disk_binds_its_own_hwmon(self):
        dev = self.fs.add_block("sda")
        self.fs.add_hwmon(0, dev, 33000)  # sda 自己的传感器 33℃
        self.assertEqual(
            disks._disk_hwmon_dirs("sda", sysfs_root=self.fs.root),
            [dev / "hwmon" / "hwmon0"],
        )
        self.assertEqual(disks._hwmon_temp1_for_block("sda", sysfs_root=self.fs.root), "33")

    def test_unrelated_hwmon_not_matched(self):
        """归属 bug 回归：无关传感器的温度绝不能套到这块盘上。"""
        dev = self.fs.add_block("sda")
        self.fs.add_hwmon(0, dev, 33000)
        unrelated = self.fs.add_class_hwmon_with_device(9, 45000)  # 别的盘 45℃
        dirs = disks._disk_hwmon_dirs("sda", sysfs_root=self.fs.root)
        self.assertEqual(dirs, [dev / "hwmon" / "hwmon0"])
        self.assertNotIn(unrelated, dirs)
        self.assertEqual(disks._hwmon_temp1_for_block("sda", sysfs_root=self.fs.root), "33")

    def test_disk_without_hwmon_returns_empty(self):
        self.fs.add_block("sdb")
        self.assertEqual(disks._disk_hwmon_dirs("sdb", sysfs_root=self.fs.root), [])
        self.assertEqual(disks._hwmon_temp1_for_block("sdb", sysfs_root=self.fs.root), "")

    def test_nvme_namespace_binds_controller_hwmon(self):
        self.fs.add_block("nvme0n1")
        ctrl = self.fs.add_nvme_ctrl("nvme0")
        self.fs.add_hwmon(1, ctrl, 41000)
        dirs = disks._disk_hwmon_dirs("nvme0n1", sysfs_root=self.fs.root)
        self.assertIn(ctrl / "hwmon" / "hwmon1", dirs)
        self.assertEqual(disks._hwmon_temp1_for_block("nvme0n1", sysfs_root=self.fs.root), "41")

    def test_invalid_name_rejected(self):
        self.assertEqual(disks._disk_hwmon_dirs("../etc", sysfs_root=self.fs.root), [])
        self.assertEqual(disks._disk_hwmon_dirs("", sysfs_root=self.fs.root), [])

    def test_out_of_range_reading_ignored(self):
        dev = self.fs.add_block("sdc")
        self.fs.add_hwmon(2, dev, -99000)  # 明显非法的读数
        self.assertEqual(disks._hwmon_temp1_for_block("sdc", sysfs_root=self.fs.root), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
