"""
巡检子包共享基座：常量、巡检状态文件读写。

probes/ups/disks/report 各子模块从这里 `from ._consts import ...` 取常量与状态函数；
各子模块自带各自所需的 stdlib import，此处只保留本文件用到的。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Tuple

_STATE_FILENAME = "nas_patrol_state.json"

RETRY_BACKOFF_BASE_SEC = 300
RETRY_BACKOFF_MAX_SEC = 3600
# 单个巡检周期推送失败后的最大重试次数；用尽后放弃本周期，等下次计划时间
RETRY_MAX_ATTEMPTS = 2

# NUT upsd 默认端口（仅从系统自动探测连接，不提供应用层配置项）
_NUT_UPSD_DEFAULT_PORT = 3493
# 巡检 UPS：compose 挂载 /etc/nut 目录；仅当其中存在可读 ups.conf 时才采集
_NUT_UPS_CONF_MOUNT_PATH = "/etc/nut/ups.conf"

# psutil disk_partitions(all=True) 时按 fstype 排除的伪/容器文件系统（路径上仍会再挡 Docker）
_PATROL_PSUTIL_NOISE_FSTYPES: frozenset = frozenset(
    {
        "tmpfs",
        "devtmpfs",
        "proc",
        "sysfs",
        "cgroup2",
        "overlay",
        "squashfs",
        "fuse.portal",
        "rpc_pipefs",
        "securityfs",
        "debugfs",
        "pstore",
        "bpf",
        "tracefs",
        "autofs",
        "binfmt_misc",
        "nsfs",
    }
)

_PATROL_CONTAINER_FILE_BIND_MOUNTS: frozenset = frozenset(
    {
        "/etc/hostname",
        "/etc/hosts",
        "/etc/localtime",
        "/etc/resolv.conf",
        "/etc/timezone",
    }
)

_PATROL_CONTAINER_APP_MOUNT_PREFIXES: Tuple[str, ...] = (
    "/app",
    "/workspace",
    "/config",
)

_PATROL_HOST_STORAGE_PATH_CANDIDATES: Tuple[str, ...] = (
    "/vol1",
    "/vol2",
    "/vol3",
    "/vol4",
    "/volume1",
    "/volume2",
    "/volume3",
    "/mnt/vol1",
    "/mnt/volume1",
    "/usr/trim/var/eventlogger_service",
    "/usr/trim/var/backup_service",
    "/usr/local/apps/@appdata/trim.media/database",
    "/usr/local/apps/@appdata/trim.photos/db",
    "/usr/local/apps/@appdata/fn-scheduler",
)


class _PatrolCfgEmpty:
    """探测脚本无 Config 时仅提供 logger_db_path（可由 LOGGER_DB_PATH 推断）。"""

    logger_db_path: str = ""


def _state_path(cursor_dir: str) -> Path:
    p = Path(cursor_dir or "./data/cursor")
    p.mkdir(parents=True, exist_ok=True)
    return p / _STATE_FILENAME


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.getLogger(__name__).warning("写入巡检状态失败: %s", e)


def _normalize_patrol_state(state: Dict[str, Any]) -> None:
    if "last_success_ts" not in state and "last_report_ts" in state:
        try:
            state["last_success_ts"] = float(state["last_report_ts"])
        except (TypeError, ValueError):
            state["last_success_ts"] = 0.0
    if "last_success_ts" not in state:
        state["last_success_ts"] = 0.0
    state.pop("last_report_ts", None)


