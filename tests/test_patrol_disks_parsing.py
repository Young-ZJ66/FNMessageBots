"""patrol/disks.py 纯解析函数的桩测试（不触磁盘/命令，可在任何平台跑）。

运行方式（项目根目录）：
    python tests/test_patrol_disks_parsing.py

覆盖最高回归价值的解析逻辑：
- _normalize_block_name：分区→整盘名的裁剪规则（nvme/mmcblk/dm/md/luks 各分支）
- _patrol_is_whole_disk_name：整盘名识别
- _dedupe_block_names：去重并过滤非法/非整盘名
- _nvme_kelvin_or_celsius / _celsius_ok / _fmt_celsius：温度单位与格式
- _parse_celsius_from_smartctl_text：smartctl 文本的多格式解析（主温度/Sensor/ATA RAW）
- _patrol_mount_is_data_volish / _patrol_is_fn_vol_mount：飞牛挂载点识别
- _patrol_mount_is_file_bind_noise：容器文件 bind mount 噪声过滤（纯路径分支）
- _patrol_disk_row_merge_key：多挂载点合并键（nvme 归一 / dm 单列 / sda 直通）
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

import stub_deps  # noqa: E402

stub_deps.install()

from monitor.patrol.disks import (  # noqa: E402
    _celsius_ok,
    _dedupe_block_names,
    _fmt_celsius,
    _normalize_block_name,
    _nvme_kelvin_or_celsius,
    _parse_celsius_from_smartctl_text,
    _patrol_disk_row_merge_key,
    _patrol_is_fn_vol_mount,
    _patrol_is_whole_disk_name,
    _patrol_mount_is_data_volish,
    _patrol_mount_is_file_bind_noise,
)


class NormalizeBlockNameCase(unittest.TestCase):
    def test_partitions_trim_to_whole_disk(self):
        cases = {
            "sda1": "sda",
            "sdb12": "sdb",
            "nvme0n1p2": "nvme0n1",
            "nvme1n1p1": "nvme1n1",
            "mmcblk0p1": "mmcblk0",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_block_name(raw), want)

    def test_whole_disks_keep_name(self):
        for raw in ("sda", "nvme0n1", "md0", "dm-2"):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_block_name(raw), raw)

    def test_mmcblk_trim_is_legacy_behavior(self):
        """既有行为：mmcblk0 也走「去尾数字」分支（mmcblk 前缀只要含 p 即裁）。
        记录契约防止无意识变更；mmcblk 带分区应裁为 mmcblk0。"""
        self.assertEqual(_normalize_block_name("mmcblk0p1"), "mmcblk0")
        self.assertEqual(_normalize_block_name("mmcblk0"), "mmcblk")

    def test_dm_keeps_number(self):
        self.assertEqual(_normalize_block_name("dm-0"), "dm-0")
        self.assertEqual(_normalize_block_name("dm-12"), "dm-12")

    def test_mapper_names_untouched(self):
        """trim_/luks- 前缀是逻辑名，不能削尾数字（否则削坏 UUID 段）。"""
        self.assertEqual(_normalize_block_name("trim_9a6b"), "trim_9a6b")
        self.assertEqual(_normalize_block_name("luks-1234abcd"), "luks-1234abcd")

    def test_dirty_input(self):
        self.assertEqual(_normalize_block_name(""), "")
        self.assertEqual(_normalize_block_name(None), "")
        self.assertEqual(_normalize_block_name("sda:1"), "")
        self.assertEqual(_normalize_block_name("/dev/sda"), "")
        self.assertEqual(_normalize_block_name("  sda1  "), "sda")


class WholeDiskNameCase(unittest.TestCase):
    def test_whole_disk_names(self):
        for raw in ("sda", "sdb", "nvme0n1", "vdX", "xvda", "hda", "mmcblk0"):
            with self.subTest(raw=raw):
                self.assertTrue(_patrol_is_whole_disk_name(raw))

    def test_non_whole_names(self):
        for raw in ("", "sda1", "nvme0n1p2", "dm-0", "trim_x", "../etc", "sda:1"):
            with self.subTest(raw=raw):
                self.assertFalse(_patrol_is_whole_disk_name(raw))


class DedupeBlockNamesCase(unittest.TestCase):
    def test_dedupe_and_filter(self):
        self.assertEqual(
            _dedupe_block_names(["sda1", "SDA", "nvme0n1p1", "nvme0n1", "dm-0", "", "sda"]),
            ["sda", "nvme0n1"],
        )

    def test_case_insensitive_dedupe(self):
        self.assertEqual(_dedupe_block_names(["SDA", "sda"]), ["SDA"])


class TemperatureCase(unittest.TestCase):
    def test_celsius_range(self):
        self.assertTrue(_celsius_ok(40.0))
        self.assertTrue(_celsius_ok(-40.0))
        self.assertTrue(_celsius_ok(120.0))
        self.assertFalse(_celsius_ok(121.0))
        self.assertFalse(_celsius_ok(-41.0))

    def test_fmt(self):
        self.assertEqual(_fmt_celsius(41), "41.0")
        self.assertEqual(_fmt_celsius(41.25), "41.2")

    def test_nvme_kelvin(self):
        self.assertEqual(_nvme_kelvin_or_celsius(314), 41.0, "开尔文 314 = 摄氏 41")
        self.assertEqual(_nvme_kelvin_or_celsius(41), 41.0, "摄氏 41 直接接受")
        self.assertIsNone(_nvme_kelvin_or_celsius(500), "超范围拒绝")
        self.assertIsNone(_nvme_kelvin_or_celsius("abc"))

    def test_smartctl_main_temperature(self):
        out = "Temperature: 41 Celsius\n"
        self.assertEqual(_parse_celsius_from_smartctl_text(out), "41.0")

    def test_smartctl_sensor1_only(self):
        out = "Temperature Sensor 1: 35\nTemperature Sensor 2: 99\n"
        self.assertEqual(_parse_celsius_from_smartctl_text(out), "35.0", "Sensor 2+ 必须忽略")

    def test_smartctl_ata194_raw(self):
        """ATA 194 取「-」后的 RAW 值，不能把 VALUE/THRESH 当温度。"""
        out = "194 Temperature_Celsius     0x0022   100   040   000    Old_age   Always       -       41\n"
        self.assertEqual(_parse_celsius_from_smartctl_text(out), "41.0")

    def test_smartctl_stats_line_ignored(self):
        out = "Comp. Temperature Time: 0\nTemperature: 42 Celsius\n"
        self.assertEqual(_parse_celsius_from_smartctl_text(out), "42.0")

    def test_smartctl_empty(self):
        self.assertEqual(_parse_celsius_from_smartctl_text(""), "--", "空输出回退占位符")
        self.assertEqual(_parse_celsius_from_smartctl_text("no temp here"), "--")


class MountFilterCase(unittest.TestCase):
    def test_data_volish(self):
        for m in ("/vol1", "/vol2", "/VOL3", "/volume1", "/volume2/data"):
            with self.subTest(m=m):
                self.assertTrue(_patrol_mount_is_data_volish(m))
        for m in ("/volumes", "/voluntary", "/vol", "/opt", "/"):
            with self.subTest(m=m):
                self.assertFalse(_patrol_mount_is_data_volish(m))

    def test_fn_vol_exact(self):
        self.assertTrue(_patrol_is_fn_vol_mount("/vol1"))
        self.assertTrue(_patrol_is_fn_vol_mount("/VOL2"))
        self.assertFalse(_patrol_is_fn_vol_mount("/vol1/data"), "存储空间根挂载不含子路径")
        self.assertFalse(_patrol_is_fn_vol_mount("/vol"))

    def test_file_bind_noise_pure_paths(self):
        """纯路径分支（不依赖 os.stat 的部分）；stat 分支在本平台不可控故不覆盖。"""
        self.assertTrue(_patrol_mount_is_file_bind_noise("/etc/hostname"))
        self.assertTrue(_patrol_mount_is_file_bind_noise("/app/data"))
        self.assertTrue(_patrol_mount_is_file_bind_noise("/run/secrets/x"))
        self.assertFalse(_patrol_mount_is_file_bind_noise("/vol1"))


class MergeKeyCase(unittest.TestCase):
    def test_whole_disk_uses_physical(self):
        self.assertEqual(_patrol_disk_row_merge_key("", "sda", "x", "/vol1"), "sda")
        self.assertEqual(_patrol_disk_row_merge_key("", "nvme0n1", "x", "/vol1"), "nvme0n1")

    def test_nvme_partition_normalized(self):
        self.assertEqual(_patrol_disk_row_merge_key("", "nvme0n1", "x", "/vol1"), "nvme0n1")

    def test_dm_keyed_alone(self):
        self.assertEqual(_patrol_disk_row_merge_key("", "dm-0", "x", "/vol1"), "dm-0")

    def test_trailing_dash_stripped(self):
        self.assertEqual(_patrol_disk_row_merge_key("", "sda-", "x", "/vol1"), "sda")


if __name__ == "__main__":
    unittest.main(verbosity=2)
