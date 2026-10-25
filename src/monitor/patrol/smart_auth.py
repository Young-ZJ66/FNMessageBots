"""SMART 授权命令生成：按当前系统实际设备与运行形态，生成可直接在 SSH
执行授权的命令（与上游常见问题页的设计一致）。

- native（应用以普通账号运行）：setcap + setfacl + 验证命令；
- 容器（root）：compose devices 映射片段（按容器内可见的盘名生成）。

同时承载两个运行期标记（由 patrol.disks 在巡检时更新）：
SAT_FALLBACK_USED（-d sat 回退拿到过数据）、SMART_AUTH_NEEDED（最近一轮
巡检存在温度未取得的盘）——配置页据此决定是否主动展示授权命令。
"""

from __future__ import annotations

import glob
import os
import re
from shutil import which
from typing import List

try:
    import pwd  # Unix 专属；Windows 开发环境无此模块
except ImportError:  # pragma: no cover
    pwd = None  # type: ignore

SAT_FALLBACK_USED = False
SMART_AUTH_NEEDED = False

_BLOCK_NAME_RE = re.compile(r"(sd[a-z]+|nvme\d+n\d+)")


def list_smart_devices() -> List[str]:
    """宿主机块设备节点：SATA（/dev/sdX）与 NVMe 命名空间（/dev/nvme*n*）。"""
    return sorted(glob.glob("/dev/sd[a-z]")) + sorted(glob.glob("/dev/nvme[0-9]n[0-9]"))


def list_block_names_from_sysfs() -> List[str]:
    """容器内看不到 /dev 节点时，用 sysfs 盘名生成 compose 映射片段。"""
    try:
        names = os.listdir("/sys/block")
    except OSError:
        return []
    return sorted(n for n in names if _BLOCK_NAME_RE.fullmatch(n))


def app_account() -> str:
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            pass
    return "FnMessageBot"


def is_container_mode() -> bool:
    """容器内以 root 运行（镜像未降权）；native 应用为普通账号。"""
    getuid = getattr(os, "getuid", None)
    return bool(getuid) and getuid() == 0


def _sat_hint() -> str:
    from monitor.patrol import disks  # 延迟导入，避免模块环

    return " -d sat" if getattr(disks, "SAT_FALLBACK_USED", False) else ""


def auth_needed() -> bool:
    from monitor.patrol import disks  # 延迟导入，避免模块环

    return bool(getattr(disks, "SMART_AUTH_NEEDED", False))


def generate_auth_commands() -> str:
    """生成授权命令文本；native 为 SSH 命令，容器为 compose 映射片段。"""
    if is_container_mode():
        names = list_block_names_from_sysfs()
        if not names:
            return (
                "# 容器内未发现物理盘名（/sys/block 为空）。"
                "请按宿主机实际盘名，在 compose 的 services 段加入 devices 映射后重建容器"
            )
        lines = ["# 在宿主机 compose 的 services 段加入以下映射，然后重建容器：", "    devices:"]
        for name in names:
            lines.append(f"      - /dev/{name}:/dev/{name}")
        return "\n".join(lines)

    account = app_account()
    devs = list_smart_devices()
    lines: List[str] = []
    if not which("smartctl"):
        lines.append("sudo apt-get install -y smartmontools libcap2-bin acl")
    lines.append('sudo setcap cap_sys_rawio,cap_sys_admin+ep "$(command -v smartctl)"')
    if devs:
        lines.append("sudo setfacl -m u:{}:r -- {}".format(account, " ".join(devs)))
    else:
        lines.append("# 未发现 /dev/sdX 设备节点，请按实际盘名自行补充 setfacl 一行")
    first = devs[0] if devs else "/dev/sda"
    lines.append(
        "sudo -u {acct} smartctl -A{sat} {dev} | grep -i temper".format(
            acct=account, sat=_sat_hint(), dev=first
        )
    )
    lines.append("# 最后一行能打印温度即授权成功；重启 NAS 后需重跑 setfacl（升级应用会自动重授）")
    return "\n".join(lines)
