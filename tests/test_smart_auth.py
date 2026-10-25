"""SMART 巡检主动诊断的桩测试（不触真实磁盘/命令）。

运行方式（项目根目录）：
    python tests/test_smart_auth.py

覆盖 run_diagnostics 的五类分类（权限缺失/未装工具/超时/无传感器/正常），
以及 build_payload 的数据包字段（最小 capability、完整路径、SAT 标记）。
命令文本由前端拼装，服务端不生成——这是设计约束，不测文本。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor.patrol import disks, smart_auth  # noqa: E402

SMART_JSON = '{"temperature":{"current":33},"smart_status":{"passed":true}}'
NO_TEMP_JSON = '{"smart_status":{"passed":true}}'
SAT_HINT = "Probable ATA device behind a SAT layer. Try an additional '-d sat' argument.\n"
PERM_DENIED = "Smartctl open device: /dev/sda failed: Permission denied\n"


def _patch_disks(**overrides):
    saved = {k: getattr(disks, k) for k in overrides}
    for k, v in overrides.items():
        setattr(disks, k, v)
    return saved


def _restore_disks(saved):
    for k, v in saved.items():
        setattr(disks, k, v)


def _real_file() -> str:
    return str(Path(__file__).resolve())


class RunDiagnosticsCase(unittest.TestCase):
    def tearDown(self):
        smart_auth._STATE["issues"] = []
        smart_auth._STATE["checked_at"] = ""

    def _run(self, *, disks_list=("sda",), access=None, run=None, exists=None,
             deadline_seconds=4.0, **disk_overrides):
        defaults = dict(
            _patrol_list_visible_whole_disks=lambda: list(disks_list),
            _resolve_cmd=lambda name: _real_file() if name == "smartctl" else "",
            _hwmon_temp1_for_block=lambda nb, sysfs_root=None: "",
            _smartctl_sat_retry_needed=lambda bp, out: False,
            _parse_celsius_from_smartctl_json=lambda obj: "--",
        )
        defaults.update(disk_overrides)
        saved = _patch_disks(**defaults)
        try:
            return smart_auth.run_diagnostics(
                deadline_seconds=deadline_seconds,
                _access=access or (lambda dev, mode=True: True),
                _run=run or (lambda args, timeout: SMART_JSON),
                _exists=exists or (lambda p: True),
            )
        finally:
            _restore_disks(saved)

    def test_all_healthy_no_issues(self):
        issues = self._run(_hwmon_temp1_for_block=lambda nb, sysfs_root=None: "33")
        self.assertEqual(issues, [])
        self.assertFalse(smart_auth.auth_needed())

    def test_json_health_status_counts_as_known(self):
        # JSON 形态无 "test result" 字样，但 smart_status.passed 存在 → 不误报
        issues = self._run(
            _hwmon_temp1_for_block=lambda nb, sysfs_root=None: "33",
            _parse_celsius_from_smartctl_json=lambda obj: "33",
        )
        self.assertEqual(issues, [])

    def test_denied_device_yields_permission_issue(self):
        issues = self._run(access=lambda dev, mode=True: not dev.endswith("sda"))
        perm = [it for it in issues if it["code"] == "permission"]
        self.assertEqual(len(perm), 1)
        self.assertEqual(perm[0]["devices"], ["/dev/sda"])
        self.assertFalse(perm[0]["needs_nvme"])
        self.assertIn("只加 disk 组", perm[0]["action"])

    def test_permission_error_text_counts_as_denied(self):
        issues = self._run(run=lambda args, timeout: PERM_DENIED)
        perm = [it for it in issues if it["code"] == "permission"]
        self.assertEqual(len(perm), 1)
        self.assertEqual(perm[0]["devices"], ["/dev/sda"])

    def test_missing_smartctl(self):
        issues = self._run(_resolve_cmd=lambda name: "")
        self.assertEqual([it["code"] for it in issues], ["missing"])
        self.assertIn("smartmontools", issues[0]["action"])

    def test_timeout_classified_separately(self):
        def _timeout(args, timeout):
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

        issues = self._run(run=_timeout)
        self.assertEqual([it["code"] for it in issues], ["timeout"])

    def test_sat_retry_used_when_needed(self):
        calls = []

        def _run_stub(args, timeout):
            calls.append(list(args))
            if "-d" in args:
                return SMART_JSON
            return SAT_HINT

        issues = self._run(
            run=_run_stub,
            _smartctl_sat_retry_needed=lambda bp, out: True,
            _parse_celsius_from_smartctl_json=lambda obj: "33",
            _hwmon_temp1_for_block=lambda nb, sysfs_root=None: "33",
        )
        self.assertEqual(issues, [])
        self.assertTrue(any("-d" in c and "sat" in c for c in calls), calls)

    def test_no_temperature_data_reported(self):
        issues = self._run()  # hwmon 与 JSON 解析均无温度
        temp = [it for it in issues if it["title"] == "硬盘温度"]
        self.assertEqual(len(temp), 1)
        self.assertEqual(temp[0]["devices"], ["/dev/sda"])

    def test_hwmon_temp_suppresses_temperature_issue(self):
        issues = self._run(_hwmon_temp1_for_block=lambda nb, sysfs_root=None: "41")
        temp = [it for it in issues if it["title"] == "硬盘温度"]
        self.assertEqual(temp, [])

    def test_no_disks_source_issue(self):
        issues = self._run(disks_list=())
        self.assertEqual([it["code"] for it in issues], ["source"])

    def test_unchecked_after_deadline(self):
        issues = self._run(deadline_seconds=0.0)
        self.assertEqual([it["code"] for it in issues], ["timeout"])
        self.assertIn("/dev/sda", issues[0]["message"])


class BuildPayloadCase(unittest.TestCase):
    def tearDown(self):
        smart_auth._STATE["issues"] = []
        smart_auth._STATE["checked_at"] = ""

    def test_minimal_capability_for_sata_only(self):
        saved = _patch_disks(
            _patrol_list_visible_whole_disks=lambda: ["sda", "sdb"],
            _resolve_cmd=lambda name: _real_file() if name == "smartctl" else "",
            _hwmon_temp1_for_block=lambda nb, sysfs_root=None: "",
            _smartctl_sat_retry_needed=lambda bp, out: False,
            _parse_celsius_from_smartctl_json=lambda obj: "--",
        )
        try:
            smart_auth.run_diagnostics(
                _access=lambda dev, mode=True: not dev.endswith("sda"),
                _run=lambda args, timeout: SMART_JSON,
                _exists=lambda p: True,
            )
            payload = smart_auth.build_payload()
        finally:
            _restore_disks(saved)
        self.assertEqual(payload["mode"], "native")
        self.assertEqual(payload["caps_native"], "cap_sys_rawio+ep")  # 无 NVMe 不加 admin
        self.assertEqual(payload["devices"], ["/dev/sda"])
        self.assertTrue(payload["needed"])
        # 完整路径保证仅在能解析到 sbin 时成立（Linux 真机）；否则回退裸名
        if os.name != "nt":
            self.assertIn("/usr/sbin", payload["setcap"])
        self.assertFalse(payload["sat"])

    def test_nvme_devices_add_admin_capability(self):
        saved = _patch_disks(
            _patrol_list_visible_whole_disks=lambda: ["nvme0n1"],
            _resolve_cmd=lambda name: _real_file() if name == "smartctl" else "",
            _hwmon_temp1_for_block=lambda nb, sysfs_root=None: "",
            _smartctl_sat_retry_needed=lambda bp, out: False,
            _parse_celsius_from_smartctl_json=lambda obj: "--",
        )
        try:
            smart_auth.run_diagnostics(
                _access=lambda dev, mode=True: False,
                _run=lambda args, timeout: SMART_JSON,
                _exists=lambda p: True,
            )
            payload = smart_auth.build_payload()
        finally:
            _restore_disks(saved)
        self.assertIn("cap_sys_admin", payload["caps_native"])

    def test_sat_flag_reaches_payload(self):
        saved = _patch_disks(SAT_FALLBACK_USED=True)
        try:
            payload = smart_auth.build_payload()
            self.assertTrue(payload["sat"])
        finally:
            _restore_disks(saved)

    def test_patrol_flag_fallback_for_needed(self):
        saved = _patch_disks(SMART_AUTH_NEEDED=True)
        try:
            self.assertTrue(smart_auth.auth_needed())
            payload = smart_auth.build_payload()
            self.assertTrue(payload["needed"])
        finally:
            _restore_disks(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
