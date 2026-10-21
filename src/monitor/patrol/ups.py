"""UPS 采集：NUT upsc 查询、设备识别、供电状态解析。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._consts import (
    _NUT_UPSD_DEFAULT_PORT,
    _NUT_UPS_CONF_MOUNT_PATH,
)
from .probes import (
    _default_ipv4_gateway,
    _resolve_cmd,
    _run_upsc_cmd,
    _upsc_is_plausible_device_name,
    _upsc_kv_has_ups_vars,
    _upsc_text_is_error_or_help,
)



def _nut_ups_conf_is_mounted() -> bool:
    """是否已挂载 ups.conf（容器内 /etc/nut/ups.conf 存在且可读）。"""
    p = Path(_NUT_UPS_CONF_MOUNT_PATH)
    try:
        return p.is_file() and os.access(str(p), os.R_OK)
    except OSError:
        return False


def _read_nut_ups_conf_text() -> str:
    """读取已挂载的 ups.conf（未挂载时返回空，不通过 sudo/cat 探测宿主机）。"""
    if not _nut_ups_conf_is_mounted():
        return ""
    try:
        return Path(_NUT_UPS_CONF_MOUNT_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _upsc_query_var(upsc_bin: str, nutpfx: List[str], device_id: str, var: str) -> str:
    """查询单个 upsc 变量（如 device.product）。"""
    out = _run_upsc_cmd([upsc_bin] + list(nutpfx) + [device_id, var], timeout=2.0)
    if not out or _upsc_text_is_error_or_help(out):
        return ""
    return out.strip().splitlines()[-1].strip()


def _parse_nut_ups_conf_sections(text: str) -> Dict[str, Dict[str, str]]:
    """解析 ups.conf：{节名(与 upsc -l 一致): {键: 值}}。"""
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m_sec = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m_sec:
            current = m_sec.group(1).strip()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, val = line.split("=", 1)
        k = key.strip().lower()
        v = val.strip().strip('"').strip("'")
        if k:
            sections[current][k] = v
    return sections


def _ups_device_display_name(
    device_id: str,
    conf_sections: Dict[str, Dict[str, str]],
    upsc_kv: Dict[str, str],
    upsc_bin: str = "",
    nutpfx: Optional[List[str]] = None,
) -> str:
    """设备名优先 ups.conf 的 product，其次 upsc device.product，最后节名。"""
    sec = conf_sections.get(device_id) or {}
    product = (sec.get("product") or "").strip()
    if product:
        return product
    from_upsc = (upsc_kv.get("device.product") or "").strip()
    if from_upsc:
        return from_upsc
    if upsc_bin and device_id:
        nutpfx_list = list(nutpfx or [])
        fetched = _upsc_query_var(upsc_bin, nutpfx_list, device_id, "device.product")
        if fetched:
            return fetched
    return device_id


def _parse_ups_status_text(v: str) -> str:
    raw = (v or "").strip().upper()
    if not raw:
        return "--"
    parts: List[str] = []
    if "OB" in raw:
        parts.append("电池供电")
    if "OL" in raw:
        parts.append("市电供电")
    if "LB" in raw:
        parts.append("低电量")
    return "/".join(parts) if parts else raw


def _upsc_list_device_names(upsc_bin: str, nut_host: Optional[str], port: int) -> List[str]:
    if not nut_host:
        raw = _run_upsc_cmd([upsc_bin, "-l"], timeout=1.8) or _run_upsc_cmd([upsc_bin, "-L"], timeout=1.8)
    else:
        raw = _run_upsc_cmd([upsc_bin, "-h", nut_host, "-p", str(port), "-l"], timeout=1.8) or _run_upsc_cmd(
            [upsc_bin, "-h", nut_host, "-p", str(port), "-L"], timeout=1.8
        )
    if _upsc_text_is_error_or_help(raw):
        return []
    names: List[str] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.lower().startswith("error"):
            continue
        if _upsc_is_plausible_device_name(s):
            names.append(s)
    return names


def _collect_ups_info() -> Dict[str, Any]:
    if not _nut_ups_conf_is_mounted():
        return {"present": False}

    upsc_bin = _resolve_cmd("upsc")
    port = _NUT_UPSD_DEFAULT_PORT

    hosts_to_try: List[Optional[str]] = []
    hosts_to_try.append(None)
    gw = _default_ipv4_gateway()
    for h in (gw, "172.17.0.1", "192.168.65.254"):
        if h and h not in hosts_to_try:
            hosts_to_try.append(h)
    if not any(x == "host.docker.internal" for x in hosts_to_try if x):
        hosts_to_try.append("host.docker.internal")

    seen: Set[str] = set()
    names: List[str] = []
    chosen_host: Optional[str] = None
    for h in hosts_to_try:
        key = h or "__local__"
        if key in seen:
            continue
        seen.add(key)
        cand = _upsc_list_device_names(upsc_bin, h, port)
        if cand:
            names = cand
            chosen_host = h
            break

    if not names:
        return {"present": False}

    nutpfx: List[str] = []
    if chosen_host:
        nutpfx = ["-h", chosen_host, "-p", str(port)]

    dev = names[0]
    out = _run_upsc_cmd([upsc_bin] + nutpfx + [dev], timeout=2.0)
    if _upsc_text_is_error_or_help(out):
        return {"present": False}

    kv: Dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        kv[k.strip().lower()] = v.strip()

    if not _upsc_kv_has_ups_vars(kv):
        return {"present": False}

    def _pick_key(keys: List[str]) -> str:
        for k in keys:
            if kv.get(k):
                return str(kv.get(k))
        for k in keys:
            one = _run_upsc_cmd([upsc_bin] + nutpfx + [dev, k], timeout=2.0)
            if one and not _upsc_text_is_error_or_help(one):
                return one.strip().splitlines()[-1].strip()
        return "--"

    power_raw = _pick_key(["ups.status", "input.status", "output.status"])
    if power_raw == "--" or _upsc_text_is_error_or_help(power_raw):
        return {"present": False}

    power_status = _parse_ups_status_text(power_raw)
    conf_sections = _parse_nut_ups_conf_sections(_read_nut_ups_conf_text())
    display_name = _ups_device_display_name(dev, conf_sections, kv, upsc_bin, nutpfx)
    if _upsc_text_is_error_or_help(display_name):
        return {"present": False}
    return {
        "present": True,
        "device": display_name,
        "device_id": dev,
        "power_status": power_status,
    }


