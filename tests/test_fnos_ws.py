"""patrol/fnos_ws.py 纯函数桩测试与 disks.py 接入行为测试（不连真实 fnOS）。

运行方式（项目根目录）：
    python tests/test_fnos_ws.py

覆盖：
- build_ws_url：host 补 scheme/端口、ws/wss 显式、非法形态拒绝
- sign_payload：结构与 HMAC 自洽（篡改检测）
- make_reqid：前缀 + backId 归一 + 序号
- parse_resmon_disk：data.disk[] 提取、异常项跳过
- parse_disk_smart：裸 smartctl JSON / smart|data 包装 / 休眠盘三种形态
- disks._collect_disk_items：飞牛总览优先、休眠盘不做任何探测（不唤醒）、
  未启用时走原链
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from monitor.patrol import fnos_ws  # noqa: E402
from monitor.patrol.fnos_ws import (  # noqa: E402
    FnosWsError,
    build_ws_url,
    make_reqid,
    parse_disk_smart,
    parse_resmon_disk,
    sign_payload,
)


class BuildWsUrlCase(unittest.TestCase):
    def test_host_only_gets_default_scheme_and_port(self):
        self.assertEqual(build_ws_url("192.168.5.66:5666"), "ws://192.168.5.66:5666/websocket?type=main")

    def test_bare_host_gets_ws_5666(self):
        self.assertEqual(build_ws_url("nas.lan"), "ws://nas.lan:5666/websocket?type=main")

    def test_wss_default_port_5667(self):
        self.assertEqual(build_ws_url("wss://nas.lan"), "wss://nas.lan:5667/websocket?type=main")

    def test_explicit_port_kept(self):
        self.assertEqual(build_ws_url("ws://127.0.0.1:9000"), "ws://127.0.0.1:9000/websocket?type=main")

    def test_empty_rejected(self):
        with self.assertRaises(FnosWsError):
            build_ws_url("")

    def test_http_scheme_rejected(self):
        with self.assertRaises(FnosWsError):
            build_ws_url("http://192.168.5.66:5666")

    def test_garbage_rejected(self):
        with self.assertRaises(FnosWsError):
            build_ws_url("://bad url//")


class SignPayloadCase(unittest.TestCase):
    SECRET = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()

    def test_signature_prefix_and_body_roundtrip(self):
        body = {"reqid": "abc", "req": "appcgi.resmon.disk"}
        payload = sign_payload(self.SECRET, body)
        sig_b64, raw = payload[:44], payload[44:]
        key = base64.b64decode(self.SECRET)
        expect = base64.b64encode(
            hmac.new(key, raw.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(sig_b64, expect)
        self.assertEqual(json.loads(raw), body)

    def test_tampered_body_breaks_signature(self):
        body = {"reqid": "abc", "req": "a"}
        payload = sign_payload(self.SECRET, body)
        sig_b64, raw = payload[:44], payload[44:]
        tampered = raw.replace("a", "b") if '"a"' in raw else raw + " "
        key = base64.b64decode(self.SECRET)
        bad = base64.b64encode(
            hmac.new(key, tampered.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        self.assertNotEqual(bad, sig_b64)

    def test_compact_json_no_spaces(self):
        payload = sign_payload(self.SECRET, {"a": 1, "b": "中文"})
        self.assertNotIn(", ", payload[44:])
        self.assertIn("中文", payload[44:])


class MakeReqidCase(unittest.TestCase):
    def test_shape(self):
        rid = make_reqid("abcd1234", "f00dcafe1234", 7)
        self.assertEqual(rid, "abcd1234f00dcafe00000007")

    def test_non_hex_backid_normalized(self):
        rid = make_reqid("p", "zzzz", 1)
        self.assertEqual(rid, "p0000000000000001")


class ParseResmonDiskCase(unittest.TestCase):
    def test_extracts_names_and_temps(self):
        resp = {
            "result": "succ",
            "data": {"disk": [
                {"name": "sda", "temp": 33},
                {"name": "sdb", "temp": "34.5"},
                {"name": "sdc", "temp": None},
                {"name": "", "temp": 40},
                "junk",
                {"name": "sdd", "temp": 999},   # 超界跳过
                {"name": "sde", "temp": -30},   # 超界跳过
            ]},
        }
        self.assertEqual(parse_resmon_disk(resp), {"sda": "33", "sdb": "34.5"})

    def test_missing_data_returns_empty(self):
        self.assertEqual(parse_resmon_disk({}), {})
        self.assertEqual(parse_resmon_disk({"data": {}}), {})


class ParseDiskSmartCase(unittest.TestCase):
    def test_bare_smartctl_json(self):
        resp = {
            "result": "succ",
            "model_name": "WDC WD40EFRX",
            "temperature": {"current": 33},
            "power_on_time": {"hours": 12345},
            "smart_status": {"passed": True},
        }
        self.assertEqual(
            parse_disk_smart(resp),
            {"standby": False, "temp": "33", "power_on_hours": "12345", "passed": True},
        )

    def test_wrapped_in_smart(self):
        resp = {"result": "succ", "smart": {
            "temperature": {"current": 41},
            "smart_status": {"passed": False},
        }}
        out = parse_disk_smart(resp)
        self.assertEqual(out["temp"], "41")
        self.assertFalse(out["passed"])

    def test_standby_disk(self):
        resp = {"result": "succ", "diskStandby": 1}
        self.assertEqual(
            parse_disk_smart(resp),
            {"standby": True, "temp": "", "power_on_hours": "", "passed": None},
        )

    def test_missing_fields_tolerated(self):
        out = parse_disk_smart({"result": "succ"})
        self.assertEqual(out, {"standby": False, "temp": "", "power_on_hours": "", "passed": None})


class CollectDiskItemsFnosCase(unittest.TestCase):
    """接入行为：总览优先、休眠不唤醒、未启用走原链。"""

    def _run(self, cfg, overview):
        from monitor.patrol import disks

        orig_scan = disks._patrol_scan_vol_space_by_physical
        orig_list = disks._patrol_list_visible_whole_disks
        orig_temp = disks._smart_temp_for_disk
        orig_health = disks._smart_health_for_disk
        orig_psutil = disks._psutil_disk_temp_fallback
        disks._patrol_scan_vol_space_by_physical = lambda: {}
        disks._patrol_list_visible_whole_disks = lambda: ["sda", "sdb", "sdc"]
        called = {"temp": [], "health": []}

        def fake_temp(dev):
            called["temp"].append(dev)
            return "--"

        def fake_health(dev):
            called["health"].append(dev)
            return "--"

        disks._smart_temp_for_disk = fake_temp
        disks._smart_health_for_disk = fake_health
        disks._psutil_disk_temp_fallback = lambda dev: "--"
        disks._fnos_overview_for_patrol = lambda c: overview
        try:
            items = disks._collect_disk_items(cfg)
        finally:
            disks._patrol_scan_vol_space_by_physical = orig_scan
            disks._patrol_list_visible_whole_disks = orig_list
            disks._smart_temp_for_disk = orig_temp
            disks._smart_health_for_disk = orig_health
            disks._psutil_disk_temp_fallback = orig_psutil
            disks._fnos_overview_for_patrol = lambda c: {}
        return items, called

    def test_fnos_values_win_and_standby_not_probed(self):
        cfg = type("C", (), {"fnos_monitor_enabled": True})()
        overview = {
            "sda": {"temp": "33", "passed": True, "standby": False, "power_on_hours": "1"},
            "sdb": {"temp": "", "passed": None, "standby": True, "power_on_hours": ""},
        }
        items, called = self._run(cfg, overview)
        by_dev = {it["device"]: it for it in items}
        self.assertEqual(by_dev["sda"]["temp_c"], "33")
        self.assertEqual(by_dev["sda"]["status"], "正常")  # 健康 → 展示为正常
        self.assertEqual(by_dev["sdb"]["status"], "休眠")
        self.assertEqual(by_dev["sdb"]["temp_c"], "--")
        self.assertIn("休眠", by_dev["sdb"]["temp_reason"])
        # 休眠盘绝不触发 smartctl/psutil 探测（避免唤醒）
        self.assertNotIn("sdb", called["temp"])
        self.assertNotIn("sdb", called["health"])

    def test_missing_disk_falls_back_to_command_chain(self):
        cfg = type("C", (), {"fnos_monitor_enabled": True})()
        overview = {"sda": {"temp": "30", "passed": True, "standby": False, "power_on_hours": ""}}
        items, called = self._run(cfg, overview)
        by_dev = {it["device"]: it for it in items}
        self.assertEqual(by_dev["sda"]["temp_c"], "30")
        self.assertIn("sdb", called["temp"])   # 总览没有的盘走原链
        self.assertIn("sdc", called["health"])

    def test_disabled_uses_command_chain_only(self):
        cfg = type("C", (), {"fnos_monitor_enabled": False})()
        items, called = self._run(cfg, {})
        self.assertEqual(len(items), 3)
        self.assertEqual(called["temp"], ["sda", "sdb", "sdc"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
