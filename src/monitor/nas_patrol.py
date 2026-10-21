"""NAS 定时巡检：按 Cron 表达式调度，采集本机 CPU/内存/磁盘状态并经已配置渠道推送。

首次启用仅锚定周期起点不立即推送；推送失败时按指数退避重试。

实现拆分为 patrol/ 子包（本文件为兼容门面）：
    patrol/_consts.py   共享 import 基座、常量、巡检状态文件读写
    patrol/probes.py    系统探针（CPU/内存/温度/主机名/IP/版本）
    patrol/ups.py       UPS 采集（NUT upsc）
    patrol/disks.py     硬盘采集（lsblk/findmnt/smartctl/sysfs）
    patrol/report.py    载荷组装、通知发送、手动触发、Cron 调度循环

外部只需从这里导入 run_nas_patrol_now / start_nas_patrol_thread /
nas_patrol_worker_loop；内部符号请直接从对应子模块导入。
"""

from __future__ import annotations

from .patrol.report import (
    nas_patrol_worker_loop,
    run_nas_patrol_now,
    start_nas_patrol_thread,
)

__all__ = [
    "run_nas_patrol_now",
    "start_nas_patrol_thread",
    "nas_patrol_worker_loop",
]
