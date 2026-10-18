"""巡检载荷组装、通知发送、手动触发与 Cron 调度循环。"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import stat as stat_mod
import socket
import sqlite3
import subprocess
import threading
import time
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import ipaddress
import ssl
import urllib.parse

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

from utils.logtime_display import format_ts_for_display, now_for_display
from ._consts import (
    RETRY_BACKOFF_BASE_SEC,
    RETRY_BACKOFF_MAX_SEC,
    RETRY_MAX_ATTEMPTS,
    _PatrolCfgEmpty,
    _load_state,
    _normalize_patrol_state,
    _save_state,
    _state_path,
)
from .probes import (
    _cpu_disk_temp_c,
    _cpu_mem_disk,
    _fmt_boot_time,
    _fmt_uptime,
    _patrol_read_hostname_fnos_from_logger_db,
    _pick_lan_ip,
    _pick_wan_ip,
    _read_fnos_version,
    _read_hostname,
    _read_system_version,
    _read_update_status,
)
from .ups import _collect_ups_info
from .disks import _collect_disk_items



def _collect_patrol_payload(_cfg: Any, _state: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _cfg if _cfg is not None else _PatrolCfgEmpty()
    cpu_pct, mem_pct, disk_free_gb = _cpu_mem_disk()
    cpu_temp, disk_temp = _cpu_disk_temp_c()
    missing: List[str] = []

    log_hint_h, log_hint_v = _patrol_read_hostname_fnos_from_logger_db(cfg)
    hostname = _read_hostname(log_hint_h)
    lan_ip = _pick_lan_ip()
    wan_ip = _pick_wan_ip()
    system_version = _read_system_version()
    fnos_version = _read_fnos_version(log_hint_v)
    has_update = _read_update_status()
    boot_ts = psutil.boot_time() if psutil else 0.0
    uptime = _fmt_uptime(time.time() - boot_ts) if boot_ts > 0 else "--"
    startup_time = _fmt_boot_time(boot_ts) if boot_ts > 0 else "--"
    ups = _collect_ups_info()
    disks = _collect_disk_items()
    # 汇总盘温：取已采集物理盘温度的最高值（比单一传感器回退更贴近实况）
    disk_temps: List[float] = []
    for d in disks:
        raw_t = str(d.get("temp_c") or "").strip()
        if raw_t in {"", "--", "—"}:
            continue
        try:
            disk_temps.append(float(raw_t))
        except ValueError:
            continue
    if disk_temps:
        mx = max(disk_temps)
        disk_temp = str(int(mx)) if mx.is_integer() else f"{mx:.1f}"

    if hostname == "--":
        missing.append("主机名称")
    if lan_ip == "--":
        missing.append("内网IP")
    if wan_ip == "--":
        missing.append("外网IP")
    if system_version == "--":
        missing.append("系统版本")
    if fnos_version == "--":
        missing.append("飞牛版本")
    if uptime == "--":
        missing.append("运行时间")
    if startup_time == "--":
        missing.append("启动时间")
    if str(cpu_pct) == "—":
        missing.append("CPU使用率")
    if str(cpu_temp) == "—":
        missing.append("CPU温度")
    if str(mem_pct) == "—":
        missing.append("内存使用率")
    if not disks and Path("/sys/block").is_dir():
        missing.append("硬盘状态")
    if disks:
        if all(str(d.get("temp_c") or "--") in {"--", "—"} for d in disks):
            missing.append("硬盘温度")
        if all(str(d.get("status") or "--") in {"--", "—"} for d in disks):
            missing.append("硬盘健康状态")

    return {
        "hostname": hostname,
        "lan_ip": lan_ip,
        "wan_ip": wan_ip,
        "system_version": system_version,
        "fnos_version": fnos_version,
        "has_update": has_update,
        "uptime_text": uptime,
        "startup_time": startup_time,
        "ups": ups,
        "disks": disks,
        "cpu_percent": cpu_pct,
        "cpu_temp_c": cpu_temp,
        "mem_percent": mem_pct,
        "disk_free_gb": disk_free_gb,
        "disk_temp_c": disk_temp,
        "missing_fields": missing,
    }


def _send_patrol_notification(
    app: Any,
    logger: Optional[logging.Logger],
    *,
    require_enabled: bool = True,
) -> bool:
    cfg = app.config
    if not cfg:
        if logger:
            logger.warning("NAS 巡检跳过：配置未加载")
        return False
    if require_enabled and not getattr(cfg, "nas_patrol_enabled", False):
        if logger:
            logger.warning("NAS 巡检跳过：未开启巡检任务")
        return False
    if not app.notifier:
        if logger:
            logger.warning("NAS 巡检跳过：未配置推送渠道（notifier 为空）")
        return False
    payload = _collect_patrol_payload(cfg, {})
    # 巡检触发时顺带检测外网 IP（仅用户勾选 WAN_IP_CHANGED 时生效）
    try:
        from monitor.wan_ip_monitor import check_and_notify_wan_ip_change

        check_and_notify_wan_ip_change(
            app,
            source="patrol",
            known_ip=str(payload.get("wan_ip") or ""),
        )
    except Exception as e:
        if logger:
            logger.warning("巡检触发外网 IP 检测失败: %s", e)
    ts = now_for_display()
    raw_log = json.dumps(payload, ensure_ascii=False)
    try:
        result = app.notifier.send_notification(
            event_type="NAS_PATROL_REPORT",
            event_data=payload,
            raw_log=raw_log,
            timestamp=ts,
        )
    except Exception as e:
        if logger:
            logger.error("NAS 巡检推送异常: %s", e, exc_info=True)
        return False

    if not getattr(result, "success", False):
        if logger:
            logger.warning("NAS 巡检推送未成功（渠道失败或未配置等）")
        return False
    return True


def run_nas_patrol_now(app: Any) -> tuple[bool, str]:
    """立即执行一次巡检推送（供 Web 手动触发；不要求已到 Cron 点）。"""
    log = logging.getLogger(__name__)
    if not app:
        return False, "运行时未就绪"
    if not getattr(app, "notifier", None):
        return False, "未配置推送渠道，请先保存至少一个推送渠道。"
    cfg = getattr(app, "config", None)
    enabled = bool(cfg and getattr(cfg, "nas_patrol_enabled", False))
    print("NAS 巡检：收到立即执行请求", flush=True)
    ok = _send_patrol_notification(app, log, require_enabled=False)
    if ok:
        try:
            if cfg:
                sp = _state_path(getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor")
                state = _load_state(sp)
                _normalize_patrol_state(state)
                state["patrol_anchor_done"] = True
                state["last_success_ts"] = time.time()
                state["retry_until_ts"] = 0.0
                state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                state["retry_count"] = 0
                _save_state(sp, state)
        except Exception as e:
            log.warning("手动巡检成功但更新状态失败: %s", e)
        if enabled:
            msg = "巡检推送已发送。定时任务将按 Cron 在下次触发点继续执行。"
        else:
            msg = (
                "巡检推送已发送，但未勾选「巡检任务」：定时 Cron 不会自动执行。"
                "请勾选并保存配置。"
            )
        print(f"NAS 巡检：{msg}", flush=True)
        return True, msg
    return False, "巡检推送失败，请查看容器日志与推送记录。"


def _patrol_star_interval_seconds(cron_expr: str) -> Optional[int]:
    """识别 ``*/N * * * *``，返回间隔秒数；其它表达式返回 None。"""
    try:
        from utils.cron_util import normalize_cron_expr

        s = normalize_cron_expr(cron_expr or "")
    except Exception:
        s = re.sub(r"\s+", " ", (cron_expr or "").strip())
    parts = s.split(" ")
    if len(parts) != 5:
        return None
    if parts[1:] != ["*", "*", "*", "*"]:
        return None
    m = re.fullmatch(r"\*/(\d+)", parts[0])
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    if 1 <= n <= 59:
        return n * 60
    return None


def nas_patrol_worker_loop(app: Any) -> None:
    log = logging.getLogger(__name__)
    log.info("NAS 定时巡检线程已启动")
    try:
        from utils.cron_util import croniter_available

        if not croniter_available():
            print(
                "NAS 巡检：未检测到 croniter，将使用内置 Cron 回退实现（*/10 等仍可调度）",
                flush=True,
            )
    except Exception:
        pass
    _last_wait_due = 0.0
    _last_idle_msg = ""
    while app.running:
        try:
            cfg = app.config
            if not cfg or not getattr(cfg, "nas_patrol_enabled", False) or not app.notifier:
                reason = []
                if not cfg:
                    reason.append("配置未加载")
                elif not getattr(cfg, "nas_patrol_enabled", False):
                    reason.append("巡检未开启")
                if not getattr(app, "notifier", None):
                    reason.append("无推送渠道")
                msg = "NAS 巡检空闲：" + ("、".join(reason) if reason else "条件未满足")
                # 状态不变只记一次
                if msg != _last_idle_msg:
                    log.info(msg)
                    _last_idle_msg = msg
                time.sleep(30)
                continue
            _last_idle_msg = ""

            from utils.cron_util import next_cron_timestamp, resolve_nas_patrol_cron

            try:
                cron_expr = resolve_nas_patrol_cron(
                    getattr(cfg, "nas_patrol_cron", "") or "",
                    getattr(cfg, "nas_patrol_interval_minutes", 720),
                )
            except Exception as e:
                msg = f"NAS 巡检 Cron 配置无效: {e}"
                log.error(msg)
                time.sleep(60)
                continue

            sp = _state_path(getattr(cfg, "cursor_dir", "./data/cursor") or "./data/cursor")
            state = _load_state(sp)
            _normalize_patrol_state(state)
            if bool(state.get("patrol_anchor_done")) and float(state.get("last_success_ts") or 0) <= 0:
                state["patrol_anchor_done"] = False
                _save_state(sp, state)
            now = time.time()

            if not state.get("patrol_anchor_done"):
                state["patrol_anchor_done"] = True
                last_s = float(state.get("last_success_ts") or 0)
                if last_s <= 0:
                    state["last_success_ts"] = now
                    state["retry_until_ts"] = 0.0
                    state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                    _save_state(sp, state)
                    interval_s0 = _patrol_star_interval_seconds(cron_expr)
                    try:
                        if interval_s0 is not None:
                            nxt = now + float(interval_s0)
                        else:
                            nxt = next_cron_timestamp(cron_expr, now)
                        wait_min = max(0.0, (nxt - now) / 60.0)
                        nxt_str = format_ts_for_display(nxt)
                    except Exception as e:
                        wait_min = 0.0
                        nxt_str = f"（计算失败: {e}）"
                    msg = (
                        f"NAS 巡检已锚定首次周期（Cron={cron_expr}），"
                        f"第一次采集约在 {wait_min:.1f} 分钟后（{nxt_str}，不立即推送）；"
                        f"可在 Web 点「立即巡检一次」马上验证"
                    )
                    log.info(msg)
                    time.sleep(5)
                    continue
                _save_state(sp, state)

            retry_until = float(state.get("retry_until_ts") or 0)
            if now < retry_until:
                time.sleep(min(60.0, max(5.0, retry_until - now)))
                continue

            last_success = float(state.get("last_success_ts") or 0)
            # 状态异常：上次成功落在未来 → 重置，避免干等数小时
            if last_success > now + 60:
                msg = (
                    "NAS 巡检状态异常：last_success_ts 在未来（"
                    f"{format_ts_for_display(last_success)}），"
                    "已重置为当前时间"
                )
                log.warning(msg)
                last_success = now
                state["last_success_ts"] = now
                _save_state(sp, state)

            interval_s = _patrol_star_interval_seconds(cron_expr)
            # */N * * * *：只用「距上次成功 N 分钟」，避开 croniter 时区偏差（曾出现 +8 小时）
            if interval_s is not None:
                base_ls = last_success if last_success > 0 else now
                next_ts = base_ls + float(interval_s)
                if next_ts < now - 5:
                    next_ts = now
            else:
                try:
                    next_ts = next_cron_timestamp(
                        cron_expr, last_success if last_success > 0 else now
                    )
                except Exception as e:
                    msg = f"NAS 巡检 Cron 无效 ({cron_expr}): {e}"
                    log.error(msg)
                    time.sleep(60)
                    continue

            due = now >= next_ts
            if not due:
                due_at = next_ts
                sleep_s = min(60.0, max(5.0, due_at - now))
                if interval_s is not None:
                    sleep_s = min(sleep_s, float(interval_s))
                # 下次触发时间不变只记一次
                if abs(due_at - _last_wait_due) > 1:
                    nxt_str = format_ts_for_display(due_at)
                    log.info("NAS 巡检下次触发：%s（Cron=%s）", nxt_str, cron_expr)
                    _last_wait_due = due_at
                time.sleep(sleep_s)
                continue

            reason = "间隔已到" if interval_s is not None else "Cron 触发"
            log.info("NAS 巡检开始采集并推送（Cron=%s，%s）", cron_expr, reason)
            ok = _send_patrol_notification(app, log)
            now_after = time.time()
            if ok:
                state["last_success_ts"] = now_after
                state["retry_until_ts"] = 0.0
                state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                state["retry_count"] = 0
                log.info("NAS 巡检推送成功")
            else:
                attempts = int(state.get("retry_count") or 0) + 1
                if attempts > RETRY_MAX_ATTEMPTS:
                    state["last_success_ts"] = now_after
                    state["retry_until_ts"] = 0.0
                    state["retry_backoff_sec"] = RETRY_BACKOFF_BASE_SEC
                    state["retry_count"] = 0
                    log.warning("NAS 巡检推送连续失败 %s 次，放弃本周期，等待下次计划时间", attempts)
                else:
                    cur = int(state.get("retry_backoff_sec") or RETRY_BACKOFF_BASE_SEC)
                    cur = max(RETRY_BACKOFF_BASE_SEC, min(cur, RETRY_BACKOFF_MAX_SEC))
                    state["retry_until_ts"] = now_after + float(cur)
                    state["retry_backoff_sec"] = min(cur * 2, RETRY_BACKOFF_MAX_SEC)
                    state["retry_count"] = attempts
                    log.warning(
                        "NAS 巡检推送失败，%s 秒后重试（%s/%s）", cur, attempts, RETRY_MAX_ATTEMPTS
                    )
            _save_state(sp, state)

        except Exception as e:
            log.error("NAS 巡检线程异常: %s", e, exc_info=True)
        time.sleep(30)


def start_nas_patrol_thread(app: Any) -> Optional[threading.Thread]:
    t = threading.Thread(target=nas_patrol_worker_loop, args=(app,), name="NasPatrol", daemon=True)
    t.start()
    return t
