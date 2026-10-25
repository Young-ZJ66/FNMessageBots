"""SMART 权限诊断（对齐上游 access_diagnostics 的巡检部分）。

本模块只做只读探测与结构化诊断，输出数据（设备、账号、工具完整路径、
capability 建议）；授权命令文本由前端按这些数据拼装展示，服务端不生成
也不执行任何命令。

run_diagnostics() 在配置保存时按限时预算逐盘探测，区分：设备打不开
（ACL 权限）、未安装 smartctl、查询超时、无温度传感器、未返回健康数据；
结果缓存供配置页读取（get_state）。
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path
from shutil import which
from typing import Any, Dict, List, Optional

try:
    import pwd  # Unix 专属；Windows 开发环境无此模块
except ImportError:  # pragma: no cover
    pwd = None  # type: ignore

_STATE: Dict[str, Any] = {"issues": [], "checked_at": ""}

_PERMISSION_MARKERS = ("permission denied", "operation not permitted", "access denied")

_BLOCK_NAME_RE = re.compile(r"(sd[a-z]+|nvme\d+n\d+)")


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def app_account() -> str:
    """应用运行账号：FPK 环境固定 FnMessageBot，其余取当前 euid 用户名。"""
    if os.getenv("FNMB_GATEWAY_SOCKET") or os.getenv("TRIM_APPNAME") == "FnMessageBot":
        return "FnMessageBot"
    geteuid = getattr(os, "geteuid", None)
    if pwd is not None and geteuid:
        try:
            return pwd.getpwuid(geteuid()).pw_name
        except Exception:
            pass
    return "FnMessageBot"


def resolve_tool(name: str) -> str:
    """工具完整路径：which 找不到时回退 sbin 目录（SSH PATH 可能不含 sbin）。"""
    found = which(name)
    if found:
        return found
    for cand in (f"/usr/sbin/{name}", f"/usr/bin/{name}", f"/sbin/{name}"):
        if os.path.exists(cand):
            return cand
    return name


def _permission_error(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _PERMISSION_MARKERS)


def list_smart_devices() -> List[str]:
    """宿主机块设备节点：SATA（/dev/sdX）与 NVMe 命名空间（/dev/nvme*n*）。"""
    return sorted(glob.glob("/dev/sd[a-z]")) + sorted(glob.glob("/dev/nvme[0-9]n[0-9]"))


def list_block_names_from_sysfs() -> List[str]:
    """容器内看不到 /dev 节点时，用 sysfs 盘名生成映射建议。"""
    try:
        names = os.listdir("/sys/block")
    except OSError:
        return []
    return sorted(n for n in names if _BLOCK_NAME_RE.fullmatch(n))


def _issue(title: str, message: str, *, action: str = "", code: str = "",
           devices: Optional[List[str]] = None, needs_nvme: bool = False) -> Dict[str, Any]:
    return {
        "title": title,
        "message": message,
        "action": action,
        "code": code,
        "devices": list(devices or []),
        "needs_nvme": needs_nvme,
    }


def run_diagnostics(deadline_seconds: float = 4.0, _access=None, _run=None, _exists=None) -> List[Dict[str, Any]]:
    """逐盘探测 SMART 可用性（限时预算），返回并缓存问题列表。

    只读探测（argv 参数列表、shell=False），不执行任何授权动作。
    """
    from monitor.patrol import disks
    from monitor.patrol.probes import _run_smart_probe

    _access = _access or os.access
    _run = _run or _run_smart_probe
    _exists = _exists or (lambda p: Path(p).exists())
    issues: List[Dict[str, Any]] = []

    disk_names = disks._patrol_list_visible_whole_disks()
    if not disk_names:
        issues.append(_issue(
            "硬盘巡检", "未发现可采集的物理硬盘。", code="source",
            action="检查系统设备可见性；Docker 需要访问宿主 sysfs，不能用提权补出不存在的设备。",
        ))
        _STATE["issues"] = issues
        _STATE["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return issues

    smartctl = disks._resolve_cmd("smartctl")
    if not smartctl or not _exists(smartctl):
        issues.append(_issue(
            "硬盘 SMART", "未安装 smartctl，无法读取健康状态与温度。", code="missing",
            action="宿主机安装 smartmontools 后重新保存检查；hwmon 温度读取不依赖它。",
        ))
        _STATE["issues"] = issues
        _STATE["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return issues

    denied: List[str] = []
    denied_temperature: List[str] = []
    unchecked: List[str] = []
    deadline = time.monotonic() + deadline_seconds
    for disk in disk_names:
        device = f"/dev/{disk}"
        temp = disks._hwmon_temp1_for_block(disk)
        # 先识别所有打不开的设备，避免某块盘超时让后续盘的权限提示被漏掉
        if _exists(device) and not _access(device, os.R_OK):
            denied.append(device)
            if not temp:
                denied_temperature.append(device)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            unchecked.append(device)
            continue
        try:
            result = _run([smartctl, "-H", "-A", "-j", device], timeout=min(1.5, remaining))
            if disks._smartctl_sat_retry_needed(device, result):
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    result = _run([smartctl, "-H", "-A", "-j", "-d", "sat", device], timeout=min(1.5, remaining))
        except FileNotFoundError:
            issues.append(_issue(
                "硬盘 SMART", "未安装 smartctl，无法通过该工具读取健康状态。", code="missing",
                action="宿主机安装 smartmontools 后重新保存检查。",
            ))
            break
        except subprocess.TimeoutExpired:
            issues.append(_issue(
                "硬盘 SMART", f"{device} 查询超时，未判定为权限问题。", code="timeout",
                action="用「立即巡检」或常见问题的手动命令进一步检查设备响应。",
            ))
            continue
        except OSError as exc:
            issues.append(_issue("硬盘 SMART", f"{device} 查询失败：{exc}。", action="核对设备与工具路径。"))
            continue

        text = result or ""
        low = text.lower()
        if _permission_error(text) or (_exists(device) and not _access(device, os.R_OK)):
            denied.append(device)
            if not temp:
                denied_temperature.append(device)
            continue
        if not _exists(device):
            issues.append(_issue(
                "硬盘 SMART", f"设备节点不存在：{device}。", code="missing",
                action="核对系统设备节点和硬盘是否连接；容器部署需映射该设备。",
            ))
            continue
        # 健康数据是否可得：文本形态看 test result / critical_warning，
        # JSON 形态看 smart_status.passed
        health_known = "test result" in low or "critical_warning" in low
        if not health_known:
            try:
                status = json.loads(text).get("smart_status")
                if isinstance(status, dict) and isinstance(status.get("passed"), bool):
                    health_known = True
            except Exception:
                pass
        if not health_known:
            issues.append(_issue(
                "硬盘 SMART", f"{device} 未返回健康数据，不能据此显示正常。", code="source",
                action="对比应用账号与管理员的查询结果，核对权限及 USB/RAID 控制器支持；即使 root 也不能保证设备提供此数据。",
            ))
        if not temp:
            try:
                parsed_temp = disks._parse_celsius_from_smartctl_json(json.loads(text))
            except Exception:
                parsed_temp = "--"
            if parsed_temp in {"--", ""}:
                issues.append(_issue(
                    "硬盘温度", f"{device} 未读到可归属于此盘的温度。", code="source",
                    action=(
                        "此盘没有可读的硬盘 hwmon 温度，SMART 也未返回温度。"
                        "分别用应用账号与管理员查询并对比结果，检查 USB/RAID 桥接支持；"
                        "不要把 CPU/GPU 温度当作硬盘温度。"
                    ),
                    devices=[device],
                    needs_nvme=False,
                ))

    if denied:
        needs_nvme = any("nvme" in d for d in disk_names)
        temp_note = (
            "其中 {} 需通过 SMART 读取温度，授权前温度也会显示 --。".format("、".join(denied_temperature))
            if denied_temperature else ""
        )
        if in_container():
            issues.append(_issue(
                "硬盘 SMART",
                "当前容器无权访问 {} 的 SMART 数据，巡检健康状态将显示 --。{}".format("、".join(denied), temp_note),
                code="permission",
                action=(
                    "在宿主机 compose 配置上述设备的 devices 映射及 cap_add（"
                    + ("SYS_RAWIO, SYS_ADMIN" if needs_nvme else "SYS_RAWIO") +
                    "）后重建容器。"
                ),
                devices=denied,
                needs_nvme=needs_nvme,
            ))
        else:
            issues.append(_issue(
                "硬盘 SMART",
                "当前应用账号无权读取 {} 的 SMART 数据，巡检健康状态将显示 --。{}".format("、".join(denied), temp_note),
                code="permission",
                action=(
                    "在 NAS SSH 用管理员账号执行页面生成的授权命令：给 smartctl 增加 capability，"
                    "并开放上述设备的读取权限。两步均需成功；仅设置 capability 不会开放设备读取权限。"
                    "通常无需重启应用，授权后重新保存检查并点「立即巡检一次」验证。"
                    "只加 disk 组或配置免密 sudo 无法保证生效。"
                ),
                devices=denied,
                needs_nvme=needs_nvme,
            ))
    if unchecked:
        issues.append(_issue(
            "硬盘巡检", "{} 的 SMART 检查未完成。".format("、".join(unchecked)), code="timeout",
            action="保存检查有时间上限，可用「立即巡检」查看各盘实际采集结果。",
        ))

    _STATE["issues"] = issues
    _STATE["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return issues


def build_payload(devices: Optional[List[str]] = None) -> Dict[str, Any]:
    """给前端的授权数据包：命令文本由前端按这些字段拼装展示。"""
    issues: List[Dict[str, Any]] = _STATE["issues"] or []
    permission_issues = [it for it in issues if it.get("code") == "permission" and it.get("devices")]
    if devices is None:
        devices = list(permission_issues[0]["devices"]) if permission_issues else list_smart_devices()
    needs_nvme = any("nvme" in d for d in devices)
    container = in_container()
    try:
        from monitor.patrol import disks

        sat = bool(getattr(disks, "SAT_FALLBACK_USED", False))
    except Exception:
        sat = False
    return {
        "mode": "container" if container else "native",
        "account": app_account(),
        "smartctl": resolve_tool("smartctl"),
        "setcap": resolve_tool("setcap"),
        "setfacl": resolve_tool("setfacl"),
        "caps_native": "cap_sys_rawio,cap_sys_admin+ep" if needs_nvme else "cap_sys_rawio+ep",
        "caps_container": "SYS_RAWIO, SYS_ADMIN" if needs_nvme else "SYS_RAWIO",
        "devices": devices,
        "sat": sat,
        "needed": bool(issues) or _patrol_flag_needed(),
        "issues": [
            {k: it.get(k) for k in ("title", "message", "action", "code")} for it in issues
        ],
        "checked_at": _STATE["checked_at"],
    }


def _patrol_flag_needed() -> bool:
    try:
        from monitor.patrol import disks

        return bool(getattr(disks, "SMART_AUTH_NEEDED", False))
    except Exception:
        return False


def auth_needed() -> bool:
    if _STATE["issues"]:
        return True
    return _patrol_flag_needed()


def get_state() -> Dict[str, Any]:
    """配置页用：build_payload 的稳定入口。"""
    return build_payload()
