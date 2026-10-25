"""smartctl -n standby 休眠识别与巡检行表现的桩测试（不触真实磁盘/命令）。

运行方式（项目根目录）：
    python tests/test_smart_standby.py

背景：巡检此前对每块盘跑 smartctl 会唤醒休眠中的硬盘；现在统一带
-n standby 探测，休眠盘返回低功耗提示、标记「休眠」并跳过一切补充探测。

覆盖：
- _SMART_STANDBY_RE：文本/JSON 两种低功耗提示命中，正常输出不误伤
- _smart_temp_for_block_path / _smart_health_for_block_path：休眠→「休眠」，
  正常输出照常解析，且 smartctl 调用必须带 -n standby
- _collect_disk_items：休眠盘行展示（status=休眠、temp=--、不触 psutil 回退）
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

STANDBY_TEXT = "Device is in STANDBY mode, exit(2)\n"
SLEEP_TEXT = "Device is in SLEEP mode, exit(2)\n"
STANDBY_JSON = '{"smartctl":{"exit_status":2,"messages":[{"string":"Device is in STANDBY mode, exit(2)"}]}}'
LOWPOWER_TEXT = "SKIP S.M.A.R.T. response. Device is in a low-power mode (STANDBY)\n"
NORMAL_TEMP_JSON = '{"temperature":{"current":33},"smart_status":{"passed":true}}'
NORMAL_TEMP_TEXT = "Current Drive Temperature:     33 C\n"
NORMAL_HEALTH_TEXT = "SMART overall-health self-assessment test result: PASSED\n"


class StandbyRegexCase(unittest.TestCase):
    def test_matches_standby_variants(self):
        for sample in (STANDBY_TEXT, SLEEP_TEXT, STANDBY_JSON, LOWPOWER_TEXT):
            self.assertIsNotNone(disks._SMART_STANDBY_RE.search(sample.lower()), sample)

    def test_normal_output_not_matched(self):
        for sample in (NORMAL_TEMP_JSON, NORMAL_TEMP_TEXT, NORMAL_HEALTH_TEXT):
            self.assertIsNone(disks._SMART_STANDBY_RE.search(sample.lower()), sample)


class _RunCmdStub:
    """按 smartctl 参数形态返回桩输出，并记录调用参数。"""

    def __init__(self, standby=True, sat_layer=False):
        self.calls = []
        self.standby = standby
        self.sat_layer = sat_layer  # True: plain 调用返回 SAT 提示，-d sat 才给真数据

    def __call__(self, args, timeout=2.0):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "smartctl" not in joined:
            return ""
        if self.standby:
            return STANDBY_TEXT
        if self.sat_layer and "-d" not in args:
            return "Probable ATA device behind a SAT layer. Try an additional '-d sat' argument.\n"
        if "-j" in args:
            return NORMAL_TEMP_JSON
        return NORMAL_HEALTH_TEXT if "-H" in args else NORMAL_TEMP_TEXT


class TempProbeCase(unittest.TestCase):
    def _patch(self, stub):
        orig = disks._run_cmd
        disks._run_cmd = stub
        return orig

    def test_standby_text_returns_dormant(self):
        stub = _RunCmdStub(standby=True)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_temp_for_block_path("/dev/sdz"), "休眠")
        finally:
            disks._run_cmd = orig
        self.assertIn("-n", stub.calls[0])
        self.assertIn("standby", stub.calls[0])

    def test_normal_json_returns_temperature(self):
        stub = _RunCmdStub(standby=False)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_temp_for_block_path("/dev/sdz"), "33.0")
        finally:
            disks._run_cmd = orig

    def test_health_standby_returns_dormant(self):
        stub = _RunCmdStub(standby=True)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_health_for_block_path("/dev/sdz"), "休眠")
        finally:
            disks._run_cmd = orig

    def test_health_normal_returns_healthy(self):
        stub = _RunCmdStub(standby=False)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_health_for_block_path("/dev/sdz"), "健康")
        finally:
            disks._run_cmd = orig


class CollectRowStandbyCase(unittest.TestCase):
    def test_standby_row_shows_dormant_without_wakeup(self):
        stub = _RunCmdStub(standby=True)
        orig_run = disks._run_cmd
        orig_scan = disks._patrol_scan_vol_space_by_physical
        orig_list = disks._patrol_list_visible_whole_disks
        orig_psutil = disks._psutil_disk_temp_fallback

        def _boom(dev):
            raise AssertionError("休眠盘不应触发 psutil 温度回退")

        disks._run_cmd = stub
        disks._patrol_scan_vol_space_by_physical = lambda: {}
        disks._patrol_list_visible_whole_disks = lambda: ["sdz"]
        disks._psutil_disk_temp_fallback = _boom
        try:
            items = disks._collect_disk_items()
        finally:
            disks._run_cmd = orig_run
            disks._patrol_scan_vol_space_by_physical = orig_scan
            disks._patrol_list_visible_whole_disks = orig_list
            disks._psutil_disk_temp_fallback = orig_psutil

        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["status"], "休眠")
        self.assertEqual(row["temp_c"], "--")
        self.assertIn("休眠", row["temp_reason"])
        self.assertEqual(row["status_reason"], "")


class SatLayerRetryCase(unittest.TestCase):
    """SAT 转换层回退：plain 提示「Probable ATA device behind a SAT layer」时，
    自动用 -d sat 重试并拿到温度/健康。"""

    def _patch(self, stub):
        orig = disks._run_cmd
        disks._run_cmd = stub
        return orig

    def test_temp_retries_with_d_sat(self):
        stub = _RunCmdStub(standby=False, sat_layer=True)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_temp_for_block_path("/dev/sdz"), "33.0")
        finally:
            disks._run_cmd = orig
        self.assertTrue(any("-d" in c and "sat" in c for c in stub.calls), stub.calls)

    def test_health_retries_with_d_sat(self):
        stub = _RunCmdStub(standby=False, sat_layer=True)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_health_for_block_path("/dev/sdz"), "健康")
        finally:
            disks._run_cmd = orig

    def test_plain_success_never_retries(self):
        stub = _RunCmdStub(standby=False, sat_layer=False)
        orig = self._patch(stub)
        try:
            self.assertEqual(disks._smart_temp_for_block_path("/dev/sdz"), "33.0")
        finally:
            disks._run_cmd = orig
        for c in stub.calls:
            self.assertNotIn("-d", c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
