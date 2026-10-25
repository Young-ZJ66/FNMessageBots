"""smartctl SAT 转换层回退的桩测试（不触真实磁盘/命令）。

运行方式（项目根目录）：
    python tests/test_smart_sat_fallback.py

背景：部分 SATA 控制器/桥接的 ATA 命令要过 SAT 转换层，smartctl 自动
探测会提示「Probable ATA device behind a SAT layer」并拒绝给出数据，
需显式 -d sat。温度（JSON/文本）与健康（-H）三处调用均为 plain 失败后
自动附加 -d sat 重试（与上游一致，不使用 -n standby——实测部分 SAT 层
对节能态查询恒报 SLEEP/STANDBY）。
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

from monitor.patrol import disks  # noqa: E402

SAT_HINT_TEXT = "Probable ATA device behind a SAT layer. Try an additional '-d sat' argument.\n"
NORMAL_TEMP_JSON = '{"temperature":{"current":33},"smart_status":{"passed":true}}'
NORMAL_TEMP_TEXT = "Current Drive Temperature:     33 C\n"
NORMAL_HEALTH_TEXT = "SMART overall-health self-assessment test result: PASSED\n"


class _RunCmdStub:
    """按 smartctl 参数形态返回桩输出，并记录调用参数。"""

    def __init__(self, sat_layer=False):
        self.calls = []
        self.sat_layer = sat_layer  # True: plain 调用返回 SAT 提示，-d sat 才给真数据

    def __call__(self, args, timeout=2.0):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "smartctl" not in joined:
            return ""
        if self.sat_layer and "-d" not in args:
            return SAT_HINT_TEXT
        if "-j" in args:
            return NORMAL_TEMP_JSON
        return NORMAL_HEALTH_TEXT if "-H" in args else NORMAL_TEMP_TEXT


class SatLayerRetryCase(unittest.TestCase):
    def _patch(self, stub):
        orig = disks._run_cmd
        disks._run_cmd = stub
        return orig

    def test_temp_retries_with_d_sat(self):
        stub = _RunCmdStub(sat_layer=True)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_temp_for_block_path("/dev/sdz"), "33.0")
        finally:
            disks._run_cmd = orig
        self.assertTrue(any("-d" in c and "sat" in c for c in stub.calls), stub.calls)

    def test_health_retries_with_d_sat(self):
        stub = _RunCmdStub(sat_layer=True)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_health_for_block_path("/dev/sdz"), "健康")
        finally:
            disks._run_cmd = orig

    def test_plain_success_never_retries(self):
        stub = _RunCmdStub(sat_layer=False)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_temp_for_block_path("/dev/sdz"), "33.0")
        finally:
            disks._run_cmd = orig
        for c in stub.calls:
            self.assertNotIn("-d", c)

    def test_health_plain_success_never_retries(self):
        stub = _RunCmdStub(sat_layer=False)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_health_for_block_path("/dev/sdz"), "健康")
        finally:
            disks._run_cmd = orig
        for c in stub.calls:
            self.assertNotIn("-d", c)


class CollectRowCase(unittest.TestCase):
    """行组装回归：SAT 层机器上正常拿到温度与健康（不出现误报的「休眠」）。"""

    def test_sat_layer_machine_gets_temp_and_health(self):
        stub = _RunCmdStub(sat_layer=True)
        orig_run = disks._run_cmd
        orig_scan = disks._patrol_scan_vol_space_by_physical
        orig_list = disks._patrol_list_visible_whole_disks
        disks._run_cmd = stub
        disks._patrol_scan_vol_space_by_physical = lambda: {}
        disks._patrol_list_visible_whole_disks = lambda: ["sdz"]
        try:
            items = disks._collect_disk_items()
        finally:
            disks._run_cmd = orig_run
            disks._patrol_scan_vol_space_by_physical = orig_scan
            disks._patrol_list_visible_whole_disks = orig_list

        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["temp_c"], "33.0")
        self.assertEqual(row["status"], "正常")
        self.assertEqual(row["temp_reason"], "")

    def test_no_smart_data_gets_actionable_hint(self):
        stub = _RunCmdStub(sat_layer=True)
        stub.sat_layer = "none"  # 任何调用都不给数据

        def _empty(args, timeout=2.0):
            stub.calls.append(list(args))
            return ""

        orig_run = disks._run_cmd
        orig_scan = disks._patrol_scan_vol_space_by_physical
        orig_list = disks._patrol_list_visible_whole_disks
        disks._run_cmd = _empty
        disks._patrol_scan_vol_space_by_physical = lambda: {}
        disks._patrol_list_visible_whole_disks = lambda: ["sdz"]
        try:
            items = disks._collect_disk_items()
        finally:
            disks._run_cmd = orig_run
            disks._patrol_scan_vol_space_by_physical = orig_scan
            disks._patrol_list_visible_whole_disks = orig_list

        row = items[0]
        self.assertEqual(row["temp_c"], "--")
        self.assertIn("setcap", row["temp_reason"])
        self.assertIn("setfacl", row["temp_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
