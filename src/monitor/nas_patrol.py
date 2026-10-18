"""NAS 定时巡检：按 Cron 表达式调度，采集本机 CPU/内存/磁盘状态并经已配置渠道推送。

首次启用仅锚定周期起点不立即推送；推送失败时按指数退避重试。

实现拆分为 patrol/ 子包（本文件为兼容门面，转出既有公开名）：
    patrol/_consts.py   共享 import 基座、常量、巡检状态文件读写
    patrol/probes.py    系统探针（CPU/内存/温度/主机名/IP/版本）
    patrol/ups.py       UPS 采集（NUT upsc）
    patrol/disks.py     硬盘采集（lsblk/findmnt/smartctl/sysfs）
    patrol/report.py    载荷组装、通知发送、手动触发、Cron 调度循环
"""

from __future__ import annotations

from .patrol.report import (  # noqa: F401
    _collect_patrol_payload,
    _send_patrol_notification,
    nas_patrol_worker_loop,
    run_nas_patrol_now,
    start_nas_patrol_thread,
)
from .patrol._consts import (  # noqa: F401
    RETRY_BACKOFF_BASE_SEC,
    RETRY_BACKOFF_MAX_SEC,
    RETRY_MAX_ATTEMPTS,
    _load_state,
    _normalize_patrol_state,
    _save_state,
    _state_path,
)
from .patrol.probes import (  # noqa: F401
    _cpu_disk_temp_c,
    _cpu_mem_disk,
    _fmt_boot_time,
    _fmt_uptime,
    _pick_lan_ip,
    _pick_wan_ip,
    _read_fnos_version,
    _read_hostname,
    _read_system_version,
    _read_update_status,
)
from .patrol.ups import _collect_ups_info  # noqa: F401
from .patrol.disks import _collect_disk_items  # noqa: F401

__all__ = [
    "run_nas_patrol_now",
    "start_nas_patrol_thread",
    "nas_patrol_worker_loop",
]
