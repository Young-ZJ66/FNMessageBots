"""内容构建层：事件标题/正文文案的组装。

ContentBuildersMixin —— 把单个事件的 event_data 渲染为人类可读的纯文本正文，
供 message_render（整体消息组装）与 core（发送编排）调用。
依赖宿主提供的 _EMOJI_RE、plain_emoji_enabled、title_prefix 与 user lookup 路径等状态；
事件文案表（EVENT_TITLES / EVENT_NOTES）来自 event_texts 模块。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .event_texts import EVENT_NOTES, EVENT_TITLES
from .message_format import DOCKER_CONTAINER_EVENT_TYPES, MultiPlatformMessage, normalize_disk_io_payload


class ContentBuildersMixin:
    """事件标题与正文构建（宿主需提供 _EMOJI_RE / title_prefix / user lookup 路径）。"""

    def _fallback_event_title(self, event_type: str) -> str:
        """未知事件类型的标题模板（与 EVENT_TITLES 中占位格式一致）。"""
        if self.title_prefix:
            return f"{self.title_prefix}-系统事件: {event_type}"
        return f"系统事件: {event_type}"

    def _markdown_event_title(self, event_type: str) -> str:
        """结构化消息用的加粗标题（复用 EVENT_TITLES 文案，仅去掉装饰性图标）。"""
        raw = self.EVENT_TITLES.get(event_type) or self._fallback_event_title(event_type)
        return f"**{self._clean_text(raw)}**"

    def _clean_text(self, text: str) -> str:
        """按配置清理展示文本。

        plain_emoji_enabled 默认关闭：推送内容不再带装饰性图标（emoji/ZWJ/变体选择符），
        标题与正文统一在此处理，避免只有正文被清理、标题仍带图标的不一致。

        仅当原文确实以图标开头时才顺带去掉其后残留的「-」等分隔符，
        否则会误删「<前缀>-」这类正常文案（如「飞牛NAS-登录成功通知」）。
        """
        if not text:
            return ""
        raw = str(text)
        if self.plain_emoji_enabled:
            return raw
        cleaned = self._EMOJI_RE.sub("", raw)
        if cleaned != raw and raw[:1] != cleaned[:1]:
            cleaned = re.sub(r"^[\s\-·:：]+", "", cleaned)
        return cleaned.strip() or raw.strip()

    def _with_title_prefix(self, title: str) -> str:
        """把标题中的默认「飞牛NAS」替换为配置前缀；前缀留空时去掉「飞牛NAS-」仅保留事件类型部分。"""
        if not isinstance(title, str):
            return str(title)
        if not self.title_prefix:
            t = title.replace("飞牛NAS-", "", 1)
            if "飞牛NAS" in t:
                t = t.replace("飞牛NAS", "")
            return t
        return title.replace("飞牛NAS", self.title_prefix)

    def _strip_emoji(self, text: str) -> str:
        if not text:
            return ""
        return self._EMOJI_RE.sub("", text).strip()

    def _strip_body_emojis(self, text: str) -> str:
        """正文去掉符号/表情类字符（标题单独构建，不在此处理）。"""
        if not text:
            return ""
        out_lines = []
        for line in text.split("\n"):
            cleaned = self._EMOJI_RE.sub("", line)
            if cleaned.startswith("\t"):
                out_lines.append("\t" + cleaned[1:].lstrip())
            else:
                out_lines.append(cleaned.lstrip())
        return "\n".join(out_lines)

    def build_history_summary(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        raw_log: str,
        timestamp: str,
    ) -> str:
        """
        用于「推送记录」列表的摘要：尽量还原实际推送文案，但去掉表情/emoji，并压成单行。
        记录列表已单独展示时间列，这里不再重复时间行。
        """
        try:
            msg = self._build_message(event_type, event_data, timestamp, raw_log)
            _ts_line, body, note = self._build_content_parts(event_type, event_data, timestamp, raw_log)
            # 概要先于明细，与推送正文的分层一致
            segments = [msg.title or ""]
            for seg in (note, body):
                if seg:
                    segments.append(seg)
            merged = "\n".join(segments)
            merged = self._strip_emoji(merged)
            merged = re.sub(r"[ \t]+", " ", merged)
            merged = re.sub(r"\n{3,}", "\n\n", merged)
            # 表格里更易读：压成单行
            merged = merged.replace("\r\n", "\n").replace("\r", "\n")
            merged = " / ".join([p.strip() for p in merged.split("\n") if p.strip()])
            return merged[:500]
        except Exception:
            # 回退：至少保证有事件类型
            return str(event_type)[:200]

    def _build_content_parts(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        timestamp: str,
        raw_log: str,
        *,
        for_batch_inner: bool = False,
    ) -> Tuple[str, str, str]:
        """构建消息的三段结构：(时间行, 明细正文, 备注文案)。

        时间行带「时间: 」前缀。纯文本渠道由 _build_content 以「概要 → 明细 → 时间」
        顺序拼接（时间收尾）；markdown 渲染层把时间行与明细正文整体收进「>」引用块，
        时间同为末行。for_batch_inner=True 时用于多事件汇总内的子块：无时间行、无备注。
        """
        # 批量汇总：多条时整段在入口处理（仅一条时间行 + 子块无时间/无备注）
        if event_type == 'POLL_BATCH_SUMMARY':
            single_event = self._extract_single_event_from_batch(event_data)
            if single_event:
                original_type, original_data, _original_ts, original_raw = single_event
                return self._build_content_parts(
                    original_type,
                    original_data,
                    timestamp,
                    original_raw,
                    for_batch_inner=False,
                )
            body = self._build_poll_batch_summary_content(event_data)
            # 单类型批量：每条条目自带「时间」行，不再需要顶部的汇总时间行
            grouped_raw = event_data.get("grouped_events")
            single_type = isinstance(grouped_raw, dict) and len(grouped_raw) == 1
            ts_line = "" if single_type else f"时间: {timestamp}"
            return (ts_line, self._strip_body_emojis(body), "")

        if event_type == "NAS_PATROL_REPORT":
            return (f"时间: {timestamp}", self._strip_body_emojis(self._build_nas_patrol_content(event_data)), "")

        fragments: List[str] = []
        if not for_batch_inner:
            fragments.append(str(timestamp))

        detail = ""
        if event_type in ['LoginSucc', 'LoginSucc2FA1', 'LoginFail', 'Logout']:
            detail = self._build_login_content(event_data)
        elif event_type in ['SSH_INVALID_USER', 'SSH_AUTH_FAILED', 'SSH_LOGIN_SUCCESS', 'SSH_DISCONNECTED']:
            detail = self._build_ssh_content(event_type, event_data)
        elif event_type in ('FoundDisk', 'InsertDisk', 'EjectDisk'):
            detail = self._build_disk_content(event_data)
        elif event_type == 'StorageBroken':
            detail = self._build_storage_broken_content(event_data)
        elif event_type == 'STORAGE_DEGRADED':
            detail = self._build_storage_degraded_content(event_data)
        elif event_type == 'APP_CRASH':
            detail = self._build_app_crash_content(event_data)
        elif event_type in ('APP_STARTED', 'APP_STOPPED', 'APP_UPDATED', 'APP_INSTALLED', 'APP_AUTO_STARTED', 'APP_UNINSTALLED'):
            detail = self._build_app_lifecycle_content(event_data)
        elif event_type == 'APP_UPDATE_FAILED':
            detail = self._build_app_update_failed_content(event_data)
        elif event_type == 'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION':
            detail = self._build_app_start_failed_content(event_data)
        elif event_type == 'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE':
            detail = self._build_app_auto_start_failed_content(event_data)
        elif event_type == 'CPU_USAGE_ALARM':
            detail = self._build_cpu_usage_alarm_content(event_data)
        elif event_type == 'CPU_USAGE_RESTORED':
            detail = self._build_cpu_usage_restored_content(event_data)
        elif event_type == 'MEMORY_USAGE_ALARM':
            detail = self._build_memory_usage_alarm_content(event_data)
        elif event_type == 'MEMORY_USAGE_RESTORED':
            detail = self._build_memory_usage_restored_content(event_data)
        elif event_type == 'CPU_TEMPERATURE_ALARM':
            detail = self._build_cpu_temperature_alarm_content(event_data)
        elif event_type == 'UPS_ONBATT':
            detail = self._build_ups_onbatt_content(event_data)
        elif event_type == 'UPS_ONBATT_LOWBATT':
            detail = self._build_ups_onbatt_lowbatt_content(event_data)
        elif event_type == 'UPS_ONLINE':
            detail = self._build_ups_online_content(event_data)
        elif event_type == 'UPS_ENABLE':
            detail = self._build_ups_enable_disable_content('UPS_ENABLE', event_data)
        elif event_type == 'UPS_DISABLE':
            detail = self._build_ups_enable_disable_content('UPS_DISABLE', event_data)
        elif event_type == 'DiskWakeup':
            if 'merged_disks' in event_data:
                detail = self._build_merged_disk_wakeup_content(event_data)
            else:
                detail = self._build_disk_wakeup_content(event_data)
        elif event_type == 'DiskSpindown':
            if 'merged_disks' in event_data:
                detail = self._build_merged_disk_spindown_content(event_data)
            else:
                detail = self._build_disk_spindown_content(event_data)
        elif event_type == 'DISK_IO_ERR':
            detail = self._build_disk_io_err_content(event_data)
        elif event_type in {'BACKUP_TASK_SUCCESS', 'BACKUP_TASK_FAILED', 'BACKUP_TASK_PARTIAL_SUCCESS'}:
            detail = self._build_backup_task_content(event_data)
        elif event_type in {'SCHEDULER_TASK_SUCCESS', 'SCHEDULER_TASK_FAILED', 'SCHEDULER_TASK_CONDITION_FAILED'}:
            detail = self._build_scheduler_task_content(event_data)
        elif event_type in DOCKER_CONTAINER_EVENT_TYPES:
            detail = self._build_docker_container_content(event_data)
        elif event_type in {'SHUTDOWN_VM', 'STATUS_RUNNING_VM', 'DESTROY_VM'} or ("VM" in str(event_type).upper()):
            detail = self._build_vm_content(event_data)
        elif event_type in {
            'ARCHIVING_SUCCESS', 'DeleteFile', 'MovetoTrashbin', 'SHARE_EVENTID_DEL', 'SHARE_EVENTID_PUT',
            'WEBDAV_ENABLED', 'WEBDAV_DISABLED', 'SAMBA_ENABLED', 'SAMBA_DISABLED',
            'DLNA_ENABLED', 'DLNA_DISABLED', 'FTP_ENABLED', 'FTP_DISABLED', 'NFS_ENABLED', 'NFS_DISABLED',
            'FW_ENABLE', 'FW_DISABLE', 'SECURITY_PORTCHANGED',
        }:
            detail = self._build_simple_content(event_data)
        elif event_type == 'WAN_IP_CHANGED':
            detail = self._build_wan_ip_changed_content(event_data)
        elif event_type in ('MEDIA_LOGIN_SUCC', 'MEDIA_LOGOUT'):
            detail = self._build_media_login_content(event_data)
        elif event_type in (
            'MEDIA_USER_CREATED', 'TRIM_RESOURCE_ADDED', 'TRIM_SCRAPE_SUCCESS', 'TRIM_RESOURCE_DELETED'
        ):
            detail = self._build_trim_library_content(event_type, event_data)
        elif event_type in (
            'PHOTO_SHARE_CREATED',
            'PHOTO_SHARE_EXPIRED',
            'PHOTO_DEVICE_REGISTERED',
            'FACE_RECOGNITION_UPDATED',
        ):
            detail = self._build_photo_album_content(event_type, event_data)

        body = self._strip_body_emojis(detail.rstrip('\n')) if detail else ""
        # 备注仅在单事件正文追加；批量子块与巡检不带备注
        note = ""
        if not for_batch_inner and event_type != "NAS_PATROL_REPORT":
            note = self.EVENT_NOTES.get(event_type, '')
        ts_line = f"时间: {timestamp}" if not for_batch_inner else ""
        return (ts_line, body, note)

    def _build_content(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        timestamp: str,
        raw_log: str,
        *,
        for_batch_inner: bool = False,
    ) -> str:
        """构建纯文本消息内容：概要 + 明细正文 + 时间行（时间收尾）。"""
        ts_line, body, note = self._build_content_parts(
            event_type, event_data, timestamp, raw_log, for_batch_inner=for_batch_inner
        )
        segments = [seg for seg in (note, body, ts_line) if seg]
        return "\n".join(segments)

    def _extract_single_event_from_batch(
        self, event_data: Dict[str, Any]
    ) -> Optional[Tuple[str, Dict[str, Any], str, str]]:
        """从批量事件中提取唯一的一条原始事件。"""
        try:
            total = int(event_data.get('count') or 0)
        except Exception:
            total = 0
        if total != 1:
            return None

        grouped_events_raw = event_data.get('grouped_events')
        grouped_events: Dict[str, Any] = grouped_events_raw if isinstance(grouped_events_raw, dict) else {}
        if len(grouped_events) != 1:
            return None

        original_type = str(next(iter(grouped_events.keys())))
        events_raw = grouped_events.get(original_type)
        events: List[Dict[str, Any]] = events_raw if isinstance(events_raw, list) else []
        if not events:
            return None

        first = events[0] if isinstance(events[0], dict) else {}
        ed_raw = first.get('event_data')
        original_data: Dict[str, Any] = ed_raw if isinstance(ed_raw, dict) else {}
        original_ts = str(first.get('timestamp') or '')
        original_raw = str(first.get('raw_log') or '')
        return original_type, original_data, original_ts, original_raw

    def _strip_batch_inner_title_prefix(self, title: str) -> str:
        """批量汇总内部类型标题去前缀（如“飞牛NAS-”）。"""
        result = title
        result = result.replace("飞牛NAS-", "", 1)
        custom_prefix = (self.title_prefix or "").strip()
        if custom_prefix:
            result = result.replace(f"{custom_prefix}-", "", 1)
        return result

    def _build_nas_patrol_content(self, event_data: Dict[str, Any]) -> str:
        """NAS 定时巡检正文：标题由 MultiPlatformMessage.title 提供，此处仅分组版式。"""

        def _v(key: str, default: str = "--") -> str:
            s = str(event_data.get(key) or "").strip()
            return s if s else default

        def _pct(v: str) -> str:
            if v in {"--", "—"}:
                return "--"
            return f"{v}%"

        def _temp(v: str) -> str:
            if v in {"--", "—"}:
                return "--"
            return f"{v}℃"

        def _size(v: str) -> str:
            if v in {"--", "—"}:
                return "--"
            try:
                gb = float(v)
            except (TypeError, ValueError):
                return "--"
            if gb >= 1024:
                return f"{gb / 1024:.1f}TB"
            return f"{gb:.1f}GB"

        host = _v("hostname")
        lan_ip = _v("lan_ip")
        wan_ip = _v("wan_ip")
        sys_ver = _v("system_version")
        fnos_ver = _v("fnos_version")
        uptime = _v("uptime_text")
        startup = _v("startup_time")

        cpu = _pct(_v("cpu_percent"))
        cpu_t = _temp(_v("cpu_temp_c"))
        mem = _pct(_v("mem_percent"))

        lines: List[str] = []
        lines.append("【基础信息】")
        lines.append(f"主机名称: {host}")
        lines.append(f"内网IP: {lan_ip}")
        lines.append(f"外网IP: {wan_ip}")
        lines.append(f"系统版本: {sys_ver}")
        lines.append(f"飞牛版本: {fnos_ver}")
        lines.append(f"运行时间: {uptime}")
        lines.append(f"启动时间: {startup}")
        lines.append("")

        ups_raw = event_data.get("ups")
        ups_info: Dict[str, Any] = ups_raw if isinstance(ups_raw, dict) else {}
        if ups_info and bool(ups_info.get("present")):
            lines.append("【UPS】")
            lines.append(f"设备名: {str(ups_info.get('device') or '--')}")
            lines.append(f"供电状态: {str(ups_info.get('power_status') or '--')}")
            lines.append("")

        lines.append("【系统资源】")
        lines.append(f"CPU使用率: {cpu}")
        lines.append(f"CPU温度: {cpu_t}")
        lines.append(f"内存使用率: {mem}")
        lines.append("")

        disks_raw = event_data.get("disks")
        disks: List[Dict[str, Any]] = disks_raw if isinstance(disks_raw, list) else []
        if disks:
            lines.append("【硬盘状态】")
            n = len(disks)
            for i, d in enumerate(disks):
                dev = str(d.get("device") or "").strip()
                label = str(d.get("name") or "").strip()
                if label and dev and label != dev:
                    head = f"{label} ({dev})"
                else:
                    head = label or dev or "unknown"
                free_gb = str(d.get("free_gb") or "--")
                free_text = _size(free_gb)
                size_text = _size(str(d.get("size_gb") or "--"))
                temp_c = _temp(str(d.get("temp_c") or "--"))
                status = str(d.get("status") or "--")
                if status == "健康":
                    status = "正常"
                lines.append(f"【{head}】")
                if size_text != "--":
                    free_show = free_text if free_text != "--" else "--"
                    lines.append(f"剩余空间: {free_show} / {size_text}")
                elif free_text != "--":
                    lines.append(f"剩余空间: {free_text}")
                else:
                    lines.append("剩余空间: -- / --")
                lines.append(f"温度: {temp_c}")
                lines.append(f"健康状态: {status}")
        return "\n".join(lines)

    def _build_login_content(self, event_data: Dict[str, Any]) -> str:
        """构建登录相关事件内容"""
        content = ""

        user = self._display_user(event_data.get('user', ''))
        if user:
            content += f"用户名: {user}\n"
        else:
            content += "用户名: \n"

        ip = event_data.get('IP', '')
        if ip:
            content += f"IP地址: {ip}\n"
        else:
            content += "IP地址: \n"

        via = event_data.get('via', '')
        if via:
            content += f"认证方式: {via}\n"

        return content

    def _build_media_login_content(self, event_data: Dict[str, Any]) -> str:
        """影视库登录/退出内容（简洁版，减少图标）。"""
        lines: List[str] = []
        user = self._display_user(event_data.get('user', ''))
        ip = event_data.get('IP', '')
        via = event_data.get('via', '')
        lines.append(f"用户名: {user}")
        lines.append(f"IP地址: {ip}")
        if via:
            lines.append(f"认证方式: {via}")
        return "\n".join(lines)

    def _build_disk_content(self, event_data: Dict[str, Any]) -> str:
        """构建硬盘发现事件内容"""
        content = ""

        if name := event_data.get('name', ''):
            content += f"设备名称: {name}\n"

        if model := event_data.get('model', ''):
            content += f"硬盘型号: {model}\n"

        if serial := event_data.get('serial', ''):
            content += f"序列号: {serial}\n"

        return content

    def _build_storage_broken_content(self, event_data: Dict[str, Any]) -> str:
        """存储空间损坏事件内容。"""
        content = ""
        vol = event_data.get('volume') or event_data.get('VOL') or event_data.get('vol')
        if vol not in (None, ""):
            content += f"存储卷: {vol}\n"
        name = event_data.get('name', '')
        if name:
            content += f"设备名称: {name}\n"
        model = event_data.get('model', '')
        if model:
            content += f"硬盘型号: {model}\n"
        serial = event_data.get('serial', '')
        if serial:
            content += f"序列号: {serial}\n"
        return content or "（无额外详情）"

    def _build_storage_degraded_content(self, event_data: Dict[str, Any]) -> str:
        """存储空间降级事件内容（parameter 顶层 volume）。"""
        content = ""
        vol = event_data.get('volume') or event_data.get('VOL') or event_data.get('vol')
        if vol not in (None, ""):
            content += f"存储空间{vol}已降级\n"
        else:
            content += "存储空间已降级\n"
        tpl = (event_data.get('template') or '').strip()
        if tpl:
            content += f"模板: {tpl}\n"
        return content.rstrip('\n') or "（无额外详情）"

    def _build_vm_content(self, event_data: Dict[str, Any]) -> str:
        """虚拟机事件内容：展示 VM_TITLE、USER_NAME 等（parameter 中 data）。"""
        content = ""
        data = event_data.get('data', {}) or {}
        vm_title = data.get('VM_TITLE', data.get('vm_title', ''))
        if vm_title:
            content += f"虚拟机: {vm_title}\n"
        user = event_data.get('user') or data.get('USER_NAME', data.get('user', ''))
        if user:
            content += f"操作用户: {user}\n"
        from_src = event_data.get('from', '')
        if from_src:
            content += f"来源: {from_src}\n"
        return content or "（无额外详情）"

    def _build_simple_content(self, event_data: Dict[str, Any]) -> str:
        """可选事件通用内容：展示操作用户、来源等（parameter 中 data.USER_NAME、from 等）。"""
        content = ""
        data = event_data.get('data', {}) or {}
        user = self._display_user(event_data.get('user') or data.get('USER_NAME', data.get('user', '')))
        if user:
            content += f"操作用户: {user}\n"
        from_src = event_data.get('from', '')
        if from_src:
            content += f"来源: {from_src}\n"
        for key in ('path', 'PATH', 'name', 'share_name', 'SHARE_NAME'):
            val = event_data.get(key) or data.get(key)
            if val:
                content += f"{key}: {val}\n"
                break
        return content or "（无额外详情）"

    def _build_wan_ip_changed_content(self, event_data: Dict[str, Any]) -> str:
        """外网 IP 变化正文。"""
        lines: List[str] = []
        old_ip = (
            event_data.get("old_wan_ip")
            or event_data.get("previous_wan_ip")
            or ""
        )
        new_ip = event_data.get("new_wan_ip") or event_data.get("wan_ip") or ""
        if old_ip:
            lines.append(f"原外网IP: {old_ip}")
        if new_ip:
            lines.append(f"新外网IP: {new_ip}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_trim_library_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """影视库事件正文：仅保留用户可读信息。"""
        lines: List[str] = []
        data_raw = event_data.get("data")
        data: Dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
        title = (event_data.get("title") or data.get("title") or "").strip()
        item_type = (event_data.get("item_type") or data.get("item_type") or "").strip()
        season_number = event_data.get("season_number") if event_data.get("season_number") is not None else data.get("season_number")
        episode_number = event_data.get("episode_number") if event_data.get("episode_number") is not None else data.get("episode_number")
        path = (event_data.get("path") or data.get("path") or "").strip()
        app_name = (event_data.get("app_name") or data.get("app_name") or "").strip()
        user = self._display_user((event_data.get("user") or data.get("USER_NAME") or data.get("user") or "").strip())
        ip = (event_data.get("IP") or data.get("ip") or "").strip()
        runtime = event_data.get("runtime")
        if runtime in (None, ""):
            runtime = data.get("runtime")
        release_date = (event_data.get("release_date") or data.get("release_date") or "").strip()
        overview = (event_data.get("overview") or data.get("overview") or "").strip()

        if title:
            lines.append(f"资源名称: {title}")
        if item_type:
            type_map = {
                "Episode": "单集",
                "Season": "季",
                "TV": "剧集",
                "Movie": "电影",
            }
            lines.append(f"资源类型: {type_map.get(item_type, item_type)}")
        if event_type in ("TRIM_SCRAPE_SUCCESS", "TRIM_RESOURCE_ADDED", "TRIM_RESOURCE_DELETED"):
            if season_number not in (None, "", 0) and episode_number not in (None, "", 0):
                lines.append(f"集: 第{season_number}季{episode_number}集")
            elif episode_number not in (None, "", 0):
                lines.append(f"集: 第{episode_number}集")
            elif season_number not in (None, "", 0):
                lines.append(f"季: 第{season_number}季")
        else:
            if season_number not in (None, "", 0):
                lines.append(f"季: {season_number}")
            if episode_number not in (None, "", 0):
                lines.append(f"集: {episode_number}")
        if event_type == "TRIM_SCRAPE_SUCCESS":
            try:
                rt = int(runtime or 0)
            except (TypeError, ValueError):
                rt = 0
            if rt > 0:
                lines.append(f"影片时长: {rt}分钟")
            if release_date:
                lines.append(f"上映时间: {release_date}")
            if overview:
                lines.append(f"影片介绍: {overview[:120]}{'...' if len(overview) > 120 else ''}")
        if path:
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            lines.append(f"文件: {filename}")
        if app_name and event_type in ("MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED"):
            lines.append(f"应用: {app_name}")
        if user:
            if event_type == "MEDIA_USER_CREATED":
                lines.append(f"操作人: {user}")
            else:
                lines.append(f"用户: {user}")
        if ip:
            lines.append(f"IP: {ip}")

        if event_type == "MEDIA_USER_CREATED":
            # 创建用户仅展示常见可读字段，不暴露原始技术字段
            new_user = (
                data.get("NEW_USER")
                or data.get("new_user")
                or data.get("username")
                or data.get("USER_NAME")
                or ""
            )
            role = data.get("ROLE") or data.get("role") or ""
            email = data.get("EMAIL") or data.get("email") or ""
            if str(new_user).strip():
                lines.append(f"新建用户: {self._display_user(new_user)}")
            if str(role).strip():
                lines.append(f"角色: {role}")
            if str(email).strip():
                lines.append(f"邮箱: {email}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_photo_album_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """相册 photo.db 事件正文：与影视库一致，仅字段行（总述与收尾见标题 / EVENT_NOTES）。"""
        lines: List[str] = []
        if event_type == "PHOTO_SHARE_CREATED":
            if event_data.get("share_name"):
                lines.append(f"分享名: {event_data['share_name']}")
            if event_data.get("share_id"):
                lines.append(f"分享ID: {event_data['share_id']}")
            self._append_album_owner_display_line(lines, event_data)
            if event_data.get("valid_to_str"):
                lines.append(f"过期时间: {event_data['valid_to_str']}")
        elif event_type == "PHOTO_SHARE_EXPIRED":
            if event_data.get("share_name"):
                lines.append(f"分享名: {event_data['share_name']}")
            if event_data.get("share_id"):
                lines.append(f"分享ID: {event_data['share_id']}")
            if event_data.get("expired_at_str"):
                lines.append(f"过期时间: {event_data['expired_at_str']}")
            self._append_album_owner_display_line(lines, event_data)
        elif event_type == "PHOTO_DEVICE_REGISTERED":
            if event_data.get("device_display"):
                lines.append(f"设备名称: {event_data['device_display']}")
            if event_data.get("device_id"):
                lines.append(f"设备ID: {event_data['device_id']}")
            self._append_album_owner_display_line(lines, event_data)
        elif event_type == "FACE_RECOGNITION_UPDATED":
            if event_data.get("record_count") not in (None, "", 0):
                lines.append(f"本批任务记录: {event_data['record_count']}")
            if event_data.get("photos_with_face") not in (None, "", 0):
                lines.append(f"检出人脸的照片: {event_data['photos_with_face']}")
            if event_data.get("faces_detected") not in (None, "", 0):
                lines.append(f"检出人脸数: {event_data['faces_detected']}")
            names_text = (event_data.get("person_names_text") or "").strip()
            if not names_text and isinstance(event_data.get("person_names"), list):
                names_text = "、".join(str(x) for x in event_data["person_names"] if x)
            if names_text:
                lines.append(f"已命名人物: {names_text}")
            if event_data.get("debounce_sec") not in (None, "", 0):
                lines.append(f"冷却汇总: 静默 {event_data['debounce_sec']} 秒后推送")
            if event_data.get("first_task_log_id") and event_data.get("last_task_log_id"):
                lines.append(
                    f"任务区间: #{event_data['first_task_log_id']}–#{event_data['last_task_log_id']}"
                )
            elif event_data.get("photo_id") is not None:
                lines.append(f"照片: {event_data['photo_id']}")
            if not lines and event_data.get("task_log_id") is not None:
                lines.append(f"识别记录: {event_data['task_log_id']}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_backup_task_content(self, event_data: Dict[str, Any]) -> str:
        """构建备份任务事件内容。"""
        def _fmt_ts(v: Any) -> str:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return ""
            if iv > 10_000_000_000:
                iv = int(iv / 1000)
            try:
                return datetime.fromtimestamp(iv).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, ValueError):
                return ""

        def _fmt_size(v: Any) -> str:
            try:
                n = float(v)
            except (TypeError, ValueError):
                return str(v) if v not in (None, "") else ""
            units = ["B", "KB", "MB", "GB", "TB"]
            i = 0
            while n >= 1024 and i < len(units) - 1:
                n /= 1024
                i += 1
            return f"{n:.2f} {units[i]}" if i > 0 else f"{int(n)} {units[i]}"

        task_name = str(event_data.get("task_name") or "")
        operation_id = event_data.get("operation_id")
        storage_name = str(event_data.get("storage_name") or "")
        storage_addr = str(event_data.get("storage_address") or "")
        target_path = str(event_data.get("target_path") or "")
        source_paths = str(event_data.get("source_paths") or "")
        start_time = _fmt_ts(event_data.get("start_time"))
        end_time = _fmt_ts(event_data.get("finished_time"))
        files_count = event_data.get("files_count")
        completed_count = event_data.get("completed_count")
        actual_count = event_data.get("actual_count")
        actual_size = _fmt_size(event_data.get("actual_size"))
        ignoring_files = event_data.get("ignoring_files")
        status = event_data.get("status")
        error_code = event_data.get("error_code")
        error_message = str(event_data.get("error_message") or "")

        # 无变更跳过 = 已处理文件数 - 实际传输文件数
        skipped: Optional[int] = None
        try:
            if completed_count is not None and actual_count is not None:
                v = int(completed_count) - int(actual_count)
                skipped = max(0, v)
        except (TypeError, ValueError):
            pass

        # 耗时 = finished_time - start_time（秒）
        elapsed_str = ""
        try:
            s = int(event_data.get("start_time") or 0)
            f = int(event_data.get("finished_time") or 0)
            if s > 10_000_000_000:
                s = int(s / 1000)
            if f > 10_000_000_000:
                f = int(f / 1000)
            if s > 0 and f > 0 and f >= s:
                sec = f - s
                if sec < 60:
                    elapsed_str = f"{sec} 秒"
                elif sec < 3600:
                    elapsed_str = f"{sec // 60} 分 {sec % 60} 秒"
                else:
                    elapsed_str = f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"
        except (TypeError, ValueError):
            pass

        lines: List[str] = []
        if task_name:
            lines.append(f"任务名称: {task_name}")
        if operation_id not in (None, ""):
            lines.append(f"执行ID: {operation_id}")
        if storage_name or storage_addr:
            where = storage_name
            if storage_addr:
                where = f"{where} ({storage_addr})" if where else storage_addr
            lines.append(f"存储位置: {where}")
        if target_path:
            lines.append(f"备份路径: {target_path}")
        if source_paths:
            lines.append(f"源路径: {source_paths}")
        if start_time:
            lines.append(f"开始时间: {start_time}")
        if end_time:
            lines.append(f"结束时间: {end_time}")
        if actual_count not in (None, ""):
            lines.append(f"传输文件数: {actual_count}")
        if actual_size:
            lines.append(f"传输文件大小: {actual_size}")
        if ignoring_files not in (None, ""):
            lines.append(f"排除文件数: {ignoring_files}")
        if skipped is not None:
            lines.append(f"无变更跳过: {skipped}")
        if files_count not in (None, ""):
            lines.append(f"总文件数: {files_count}")
        if elapsed_str:
            lines.append(f"耗时: {elapsed_str}")
        if status not in (None, ""):
            lines.append(f"状态码: {status}")
        if error_code not in (None, "") and int(error_code or 0) != 0:
            lines.append(f"错误码: {error_code}")
        if error_message:
            lines.append(f"错误信息: {error_message[:300]}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_scheduler_task_content(self, event_data: Dict[str, Any]) -> str:
        """构建 fn-scheduler 任务执行结果内容。"""
        task_name = str(event_data.get("task_name") or "")
        result_id = event_data.get("result_id")
        account = str(event_data.get("account") or "")
        trigger_label = str(event_data.get("trigger_reason_label") or event_data.get("trigger_reason") or "")
        schedule_expr = str(event_data.get("schedule_expression") or "")
        started_at = str(event_data.get("started_at") or "")
        finished_at = str(event_data.get("finished_at") or "")
        duration = str(event_data.get("duration") or "")
        log_preview = str(event_data.get("log_preview") or "")
        status = str(event_data.get("status") or "")

        lines: List[str] = []
        if task_name:
            lines.append(f"任务名称: {task_name}")
        if result_id not in (None, ""):
            lines.append(f"执行ID: {result_id}")
        if account:
            lines.append(f"执行账户: {account}")
        if trigger_label:
            lines.append(f"触发方式: {trigger_label}")
        if schedule_expr:
            lines.append(f"计划表达式: {schedule_expr}")
        if started_at:
            lines.append(f"开始时间: {started_at}")
        if finished_at:
            lines.append(f"结束时间: {finished_at}")
        if duration:
            lines.append(f"耗时: {duration}")
        if log_preview:
            log_title = "【执行结果】" if status == "success" else "【执行日志】"
            lines.append(log_title)
            lines.append(log_preview)
        return "\n".join(lines) if lines else "（无额外详情）"

    def _build_docker_container_content(self, event_data: Dict[str, Any]) -> str:
        """Docker Engine 容器事件正文。"""
        lines: List[str] = []
        name = str(event_data.get("container_name") or "").strip()
        cid = str(event_data.get("container_id") or "").strip()
        img = str(event_data.get("image") or "").strip()
        dact = str(event_data.get("docker_action") or "").strip()
        if name:
            lines.append(f"容器名称: {name}")
        if cid:
            lines.append(f"容器ID: {cid}")
        if img:
            lines.append(f"镜像: {img}")
        if dact:
            lines.append(f"引擎动作: {dact}")
        ec = event_data.get("exit_code")
        if ec is not None and str(ec).strip() != "":
            lines.append(f"退出码: {ec}")
        return "\n".join(lines) if lines else "（无额外详情）"

    def _batch_summary_item_lines(self, event_type: str, ed: Dict[str, Any]) -> List[str]:
        """多事件合并时，单条事件只展示关键字段（无图标，多行时每条一行）。"""
        data_raw = ed.get("data")
        data: Dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
        et = str(event_type)

        if et in ("LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout"):
            login_bits: List[str] = []
            if ed.get("user") is not None:
                login_bits.append(f"用户名: {self._display_user(ed.get('user', ''))}")
            if ed.get("IP"):
                login_bits.append(f"IP地址: {ed.get('IP')}")
            if ed.get("via"):
                login_bits.append(f"认证: {ed.get('via')}")
            return [" · ".join(login_bits)] if login_bits else [et]

        if et in ("SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED"):
            parts = []
            if ed.get("user") is not None:
                parts.append(f"用户名: {self._display_user(ed.get('user', ''))}")
            if ed.get("IP"):
                parts.append(f"IP地址: {ed.get('IP')}")
            if ed.get("port"):
                parts.append(f"端口: {ed.get('port')}")
            if ed.get("reason"):
                parts.append(f"原因: {ed.get('reason')}")
            return [" · ".join(parts)] if parts else [et]

        if et == "APP_CRASH":
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else ["应用: （未知）"]

        if et == "APP_UPDATE_FAILED":
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else ["应用更新失败"]

        if et in (
            "APP_STARTED",
            "APP_STOPPED",
            "APP_UPDATED",
            "APP_INSTALLED",
            "APP_AUTO_STARTED",
            "APP_UNINSTALLED",
        ):
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else [et]

        if et in (
            "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
            "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
        ):
            name = data.get("DISPLAY_NAME") or data.get("APP_NAME")
            return [f"应用名称: {name}"] if name else [et]

        if et == "CPU_USAGE_ALARM":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("cpu_usage", data.get("USAGE", data.get("usage", "")))
            cpu_bits: List[str] = []
            if usage != "" and usage is not None:
                cpu_bits.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                cpu_bits.append(f"使用率阈值: {th}%")
            return [" · ".join(cpu_bits)] if cpu_bits else ["CPU使用率告警"]

        if et == "CPU_USAGE_RESTORED":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("cpu_usage", "")
            cpu_bits2: List[str] = []
            if usage != "" and usage is not None:
                cpu_bits2.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                cpu_bits2.append(f"阈值: {th}%")
            return [" · ".join(cpu_bits2)] if cpu_bits2 else ["CPU使用率已恢复"]

        if et == "MEMORY_USAGE_ALARM":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("memory_usage", data.get("USAGE", data.get("usage", "")))
            mem_bits: List[str] = []
            if usage != "" and usage is not None:
                mem_bits.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                mem_bits.append(f"使用率阈值: {th}%")
            return [" · ".join(mem_bits)] if mem_bits else ["内存使用率告警"]

        if et == "MEMORY_USAGE_RESTORED":
            th = data.get("THRESHOLD", ed.get("threshold", ""))
            usage = ed.get("memory_usage", "")
            mem_bits2: List[str] = []
            if usage != "" and usage is not None:
                mem_bits2.append(f"当前使用率: {usage}%")
            if th != "" and th is not None:
                mem_bits2.append(f"阈值: {th}%")
            return [" · ".join(mem_bits2)] if mem_bits2 else ["内存使用率已恢复"]

        if et == "CPU_TEMPERATURE_ALARM":
            th = data.get("THRESHOLD", ed.get("threshold", 0))
            return [f"温度阈值: {th}°C"]

        if et in ("DiskWakeup", "DiskSpindown"):
            d = ed.get("disk", "")
            if not d:
                return ["磁盘事件"]
            bits = [d]
            if ed.get("model"):
                bits.append(str(ed.get("model")))
            if ed.get("serial"):
                bits.append(str(ed.get("serial")))
            return [f"磁盘: {' / '.join(bits)}"]

        if et == "FoundDisk":
            out: List[str] = []
            if ed.get("name"):
                out.append(f"设备名称: {ed.get('name')}")
            if ed.get("model"):
                out.append(f"型号: {ed.get('model')}")
            return out or ["发现新硬盘"]

        if et == "STORAGE_DEGRADED":
            vol = ed.get("volume", ed.get("VOL"))
            if vol is not None and str(vol).strip() != "":
                return [f"存储空间{vol}已降级"]
            return ["存储空间降级"]

        if et == "DISK_IO_ERR":
            dev, model, sn, cnt = normalize_disk_io_payload(ed)
            lines = []
            if dev:
                lines.append(f"设备: {dev}")
            if model:
                lines.append(f"型号: {model}")
            if sn:
                lines.append(f"序列号: {sn}")
            if cnt != "" and cnt is not None:
                lines.append(f"错误次数: {cnt}")
            tpl = (ed.get("template") or "").strip()
            if tpl:
                lines.append(f"模板: {tpl}")
            return lines or ["磁盘IO错误"]

        if et in ("BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS"):
            backup_lines: List[str] = []
            if ed.get("task_name"):
                backup_lines.append(f"任务: {ed.get('task_name')}")
            if ed.get("operation_id") is not None:
                backup_lines.append(f"执行ID: {ed.get('operation_id')}")
            if ed.get("actual_count") is not None or ed.get("files_count") is not None:
                backup_lines.append(f"传输: {ed.get('actual_count', 0)}/{ed.get('files_count', 0)}")
            if int(ed.get("error_code") or 0) != 0:
                backup_lines.append(f"错误码: {ed.get('error_code')}")
            return [" · ".join(backup_lines)] if backup_lines else [et]

        if et in ("SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED"):
            sched_lines: List[str] = []
            if ed.get("task_name"):
                sched_lines.append(f"任务: {ed.get('task_name')}")
            if ed.get("trigger_reason_label"):
                sched_lines.append(str(ed.get("trigger_reason_label")))
            if ed.get("duration"):
                sched_lines.append(f"耗时: {ed.get('duration')}")
            return [" · ".join(sched_lines)] if sched_lines else [et]

        if et in DOCKER_CONTAINER_EVENT_TYPES:
            bits: List[str] = []
            if ed.get("container_name"):
                bits.append(str(ed.get("container_name")))
            elif ed.get("container_id"):
                bits.append(str(ed.get("container_id")))
            if ed.get("image"):
                bits.append(str(ed.get("image")))
            if ed.get("exit_code") is not None and str(ed.get("exit_code")).strip() != "":
                bits.append(f"exit={ed.get('exit_code')}")
            return [" · ".join(bits)] if bits else [et]

        if et in ("UPS_ONBATT", "UPS_ONBATT_LOWBATT"):
            ups_bits: List[str] = []
            if ed.get("battery") is not None:
                ups_bits.append(f"电量: {ed.get('battery')}%")
            if ed.get("load") is not None:
                ups_bits.append(f"负载: {ed.get('load')}%")
            return [" · ".join(ups_bits)] if ups_bits else [et]

        if et == "UPS_ONLINE":
            if ed.get("battery") is not None:
                return [f"电量: {ed.get('battery')}%"]
            return [et]

        if et in ("UPS_ENABLE", "UPS_DISABLE"):
            un = data.get("USERNAME", "")
            return [f"用户: {un}"] if un else [et]

        if et in {"SHUTDOWN_VM", "STATUS_RUNNING_VM", "DESTROY_VM"} or ("VM" in et.upper()):
            vm = data.get("VM_TITLE", data.get("vm_title", ""))
            user = ed.get("user") or data.get("USER_NAME", "")
            lines = []
            if vm:
                lines.append(f"虚拟机: {vm}")
            if user:
                lines.append(f"用户: {user}")
            return lines or [et]

        if et in {
            "ARCHIVING_SUCCESS",
            "DeleteFile",
            "MovetoTrashbin",
            "SHARE_EVENTID_DEL",
            "SHARE_EVENTID_PUT",
            "WEBDAV_ENABLED",
            "WEBDAV_DISABLED",
            "SAMBA_ENABLED",
            "SAMBA_DISABLED",
            "DLNA_ENABLED",
            "DLNA_DISABLED",
            "FTP_ENABLED",
            "FTP_DISABLED",
            "NFS_ENABLED",
            "NFS_DISABLED",
            "FW_ENABLE",
            "FW_DISABLE",
            "SECURITY_PORTCHANGED",
        }:
            user = ed.get("user") or data.get("USER_NAME", "")
            if user:
                return [f"用户: {user}"]
            if ed.get("from"):
                return [f"来源: {ed.get('from')}"]
            for key in ("path", "PATH", "share_name", "SHARE_NAME", "name"):
                v = ed.get(key) or data.get(key)
                if v:
                    return [f"{key}: {v}"]
            return [et]

        if et == "WAN_IP_CHANGED":
            old_ip = ed.get("old_wan_ip") or ed.get("previous_wan_ip") or ""
            new_ip = ed.get("new_wan_ip") or ed.get("wan_ip") or ""
            if old_ip or new_ip:
                return [f"外网IP: {old_ip or '?'} → {new_ip or '?'}"]
            return ["外网IP变化"]

        if et in ("TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS", "TRIM_RESOURCE_DELETED"):
            # 影视批量条目：一行一个键值对（时间行由调用方按条补在末尾）
            title = (ed.get("title") or data.get("title") or "").strip()
            item_type = (ed.get("item_type") or data.get("item_type") or "").strip()
            path = (ed.get("path") or data.get("path") or "").strip()
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            season_number = (
                ed.get("season_number")
                if ed.get("season_number") is not None
                else data.get("season_number")
            )
            episode_number = (
                ed.get("episode_number")
                if ed.get("episode_number") is not None
                else data.get("episode_number")
            )
            type_map = {
                "Episode": "单集",
                "Season": "季",
                "TV": "剧集",
                "Movie": "电影",
                "Video": "视频",
            }
            out: List[str] = []
            if title:
                out.append(f"片名: {title}")
            if item_type:
                out.append(f"类型: {type_map.get(item_type, item_type)}")
            if season_number not in (None, "", 0) and episode_number not in (None, "", 0):
                out.append(f"集数: 第{season_number}季{episode_number}集")
            elif episode_number not in (None, "", 0):
                out.append(f"集数: 第{episode_number}集")
            elif season_number not in (None, "", 0):
                out.append(f"集数: 第{season_number}季")
            if filename and (not title or filename != title):
                out.append(f"文件: {filename}")
            return out or [et]

        if et in ("MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED"):
            media_bits: List[str] = []
            user = self._display_user(
                (ed.get("user") or data.get("USER_NAME") or data.get("user") or "").strip()
            )
            ip = (ed.get("IP") or data.get("ip") or "").strip()
            app_name = (ed.get("app_name") or data.get("app_name") or "").strip()
            if user:
                media_bits.append(f"用户: {user}")
            if ip:
                media_bits.append(f"IP: {ip}")
            if app_name:
                media_bits.append(f"应用: {app_name}")
            return [" · ".join(media_bits)] if media_bits else [et]

        # 通用回退
        bits: List[str] = []
        if ed.get("user") or ed.get("IP"):
            bits.append(f"{ed.get('user', '')}@{ed.get('IP', '')}".strip("@"))
        dn = data.get("DISPLAY_NAME") or data.get("APP_NAME")
        if dn:
            bits.append(str(dn))
        if ed.get("name"):
            bits.append(str(ed.get("name")))
        if ed.get("title"):
            bits.append(str(ed.get("title")))
        path_fb = ed.get("path") or data.get("path")
        if path_fb:
            bits.append(str(path_fb).rsplit("/", 1)[-1])
        if bits:
            return [" · ".join(b for b in bits if b)[:200]]
        return [et]

    def _build_poll_batch_summary_content(self, event_data: Dict[str, Any]) -> str:
        """构建轮询批量汇总内容（按类型分组并展示每条差异数据）。"""
        total = int(event_data.get('count') or 0)
        by_type_raw = event_data.get('by_type')
        by_type: Dict[str, Any] = by_type_raw if isinstance(by_type_raw, dict) else {}
        items_raw = event_data.get('items')
        items: List[Dict[str, Any]] = items_raw if isinstance(items_raw, list) else []
        grouped_events_raw_in = event_data.get('grouped_events')
        grouped_events_raw: Dict[str, Any] = grouped_events_raw_in if isinstance(grouped_events_raw_in, dict) else {}
        grouped_events: Dict[str, List[Dict[str, Any]]] = {
            str(k): v for k, v in grouped_events_raw.items() if isinstance(v, list)
        }

        lines: List[str] = []
        current_len = 0
        truncated = False
        omitted_types = 0
        omitted_items = 0

        def _append(line: str) -> bool:
            nonlocal current_len, truncated
            addition = ("" if not lines else "\n") + line
            if current_len + len(addition) > self.BATCH_MAX_CHARS:
                truncated = True
                return False
            lines.append(line)
            current_len += len(addition)
            return True

        # 优先展示按类型分组后的完整明细；无分组数据时回退到简版预览
        if grouped_events:
            sorted_types = sorted(
                grouped_events.keys(),
                key=lambda et: (-int(by_type.get(et, len(grouped_events.get(et, [])))), str(et)),
            )
            selected_types = sorted_types[: self.BATCH_MAX_TYPES]
            omitted_types = max(0, len(sorted_types) - len(selected_types))
            # 单一事件类型（如影视批量入库）：标题已带类型与条数，正文不再重复组名，明细上限更高
            single_type = len(grouped_events) == 1
            per_type_limit = self.BATCH_SINGLE_TYPE_LIMIT if single_type else self.BATCH_PER_TYPE_LIMIT
            for ti, event_type in enumerate(selected_types):
                evs = grouped_events.get(event_type) or []
                if not single_type:
                    title = self._with_title_prefix(self.EVENT_TITLES.get(str(event_type), self._fallback_event_title(str(event_type))))
                    title = self._strip_batch_inner_title_prefix(title)
                    title = self._strip_body_emojis(title).strip()
                    if not _append(f"【{title}】"):
                        break
                shown = evs[:per_type_limit]
                for idx, item in enumerate(shown):
                    ed_raw = item.get('event_data')
                    ed = ed_raw if isinstance(ed_raw, dict) else {}
                    try:
                        key_lines = self._batch_summary_item_lines(str(event_type), ed)
                    except Exception:
                        brief = str(item.get('brief') or str(event_type))
                        key_lines = [self._strip_body_emojis(f"解析失败: {brief}").strip() or str(event_type)]
                    if single_type:
                        # 单类型批量：一行一个键值对，每条以自身时间收尾，条目间空行分隔
                        for kl in key_lines:
                            if not _append("\t" + kl):
                                break
                        item_ts = str(item.get('timestamp') or "")
                        if item_ts:
                            _append("\t时间: " + item_ts)
                        if idx < len(shown) - 1:
                            if not _append(""):
                                break
                    else:
                        # 多类型汇总：单条压成一行紧凑展示
                        if not _append("\t" + " · ".join(key_lines)):
                            break
                if len(evs) > per_type_limit:
                    left = len(evs) - per_type_limit
                    omitted_items += left
                    if not _append(f"… 其余 {left} 条请查看推送日志"):
                        break
                if ti < len(selected_types) - 1:
                    if not _append(""):
                        break
            if omitted_types > 0:
                _append(f"… 其余 {omitted_types} 类事件已省略，请查看推送日志")
        elif items:
            _append("事件预览:")
            for idx, it in enumerate(items[:8], 1):
                brief = str(it.get('brief') or it.get('event_type') or '（无摘要）')
                ts = str(it.get('timestamp') or '')
                line = f"\t{idx}. [{ts}] {brief}" if ts else f"\t{idx}. {brief}"
                if not _append(line):
                    break
            if total > len(items):
                omitted_items += total - len(items)
                _append(f"  … 其余 {total - len(items)} 条请查看推送日志")
        if truncated:
            _append("… 内容已截断，请查看推送日志")

        # 暴露渲染元数据，便于推送记录详情展示
        event_data["batch_render_meta"] = {
            "truncated": truncated,
            "max_chars": self.BATCH_MAX_CHARS,
            "max_types": self.BATCH_MAX_TYPES,
            "per_type_limit": self.BATCH_PER_TYPE_LIMIT,
            "omitted_types": omitted_types,
            "omitted_items": omitted_items,
        }
        return "\n".join(lines)

    def _build_disk_io_err_content(self, event_data: Dict[str, Any]) -> str:
        """构建磁盘IO错误事件内容（data 嵌套或顶层 disk/model/serial）。"""
        dev, model, sn, err_cnt = normalize_disk_io_payload(event_data)
        content = ""
        if dev:
            content += f"设备: {dev}\n"
        if model:
            content += f"型号: {model}\n"
        if sn:
            content += f"序列号: {sn}\n"
        tpl = (event_data.get("template") or "").strip()
        if tpl:
            content += f"模板: {tpl}\n"
        content += f"错误次数: {err_cnt}\n"
        return content

    def _build_ssh_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """构建SSH相关事件内容"""
        content = ""
        if event_type == 'SSH_INVALID_USER':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            content += f"用户名: {user}\n"
            content += f"IP地址: {ip}\n"
            if port:
                content += f"端口: {port}\n"
        elif event_type == 'SSH_AUTH_FAILED':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            reason = event_data.get('reason', '')
            if user:
                content += f"用户名: {user}\n"
            if ip:
                content += f"IP地址: {ip}\n"
            if port:
                content += f"端口: {port}\n"
            if reason:
                content += f"失败原因: {reason}\n"
        elif event_type == 'SSH_LOGIN_SUCCESS':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            content += f"用户名: {user}\n"
            content += f"IP地址: {ip}\n"
            if port:
                content += f"端口: {port}\n"
        elif event_type == 'SSH_DISCONNECTED':
            user = event_data.get('user', '')
            ip = event_data.get('IP', '')
            port = event_data.get('port', '')
            content += f"用户名: {user}\n"
            content += f"IP地址: {ip}\n"
            if port:
                content += f"端口: {port}\n"
        return content

    def _build_disk_wakeup_content(self, event_data: Dict[str, Any]) -> str:
        """构建单个磁盘唤醒事件内容"""
        content = ""

        if disk := event_data.get('disk', ''):
            content += f"磁盘设备: {disk}\n"

        if model := event_data.get('model', ''):
            content += f"硬盘型号: {model}\n"

        if serial := event_data.get('serial', ''):
            content += f"序列号: {serial}\n"

        return content

    def _build_disk_spindown_content(self, event_data: Dict[str, Any]) -> str:
        """构建单个磁盘休眠事件内容"""
        content = ""

        if disk := event_data.get('disk', ''):
            content += f"磁盘设备: {disk}\n"

        if model := event_data.get('model', ''):
            content += f"硬盘型号: {model}\n"

        if serial := event_data.get('serial', ''):
            content += f"序列号: {serial}\n"

        return content

    def _format_disk_fallback(self, disk_info: Dict[str, Any]) -> str:
        """当 disk/model/serial 都为空时，从 full_event_data 或 data 中拼一条可读摘要（支持飞牛顶层 template/user/model/serial）"""
        raw = disk_info.get('full_event_data') or disk_info.get('data')
        if not isinstance(raw, dict):
            return ""
        # 优先从 data 子段取，否则用顶层（飞牛格式：template, user, model, serial 在顶层）
        data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
        if not isinstance(data, dict):
            return ""
        parts = []
        for key in ('disk', 'device', 'path', 'name', 'DEVICE', 'DISK', 'deviceName', 'dev'):
            if key in data and data[key]:
                v = data[key]
                if isinstance(v, dict):
                    v = v.get('path') or v.get('device') or v.get('name') or str(v)[:80]
                parts.append(f"设备: {v}")
                break
        for key in ('model', 'MODEL', 'Model', 'modelName'):
            if key in data and data[key]:
                parts.append(f"型号: {data[key]}")
                break
        for key in ('serial', 'SERIAL', 'Serial', 'sn', 'SN', 'serialNumber'):
            if key in data and data[key]:
                parts.append(f"序列号: {data[key]}")
                break
        if parts:
            return " ".join(parts)
        # 无标准字段时，列出 data 中部分键值便于排查
        skip = {'raw', 'datetime', 'eventId', 'level', 'from', 'template', 'cat'}
        extra = [f"{k}: {v}" for k, v in list(data.items())[:5] if k not in skip and v]
        if extra:
            return "原始: " + ", ".join(extra)[:120]
        return ""

    def _build_merged_disk_wakeup_content(self, event_data: Dict[str, Any]) -> str:
        """构建合并磁盘唤醒事件内容"""
        content = ""

        merged_disks = event_data.get('merged_disks', [])
        for i, disk_info in enumerate(merged_disks, 1):
            content += f"【磁盘 #{i}】\n"
            disk = disk_info.get('disk', '') or ''
            model = disk_info.get('model', '') or ''
            serial = disk_info.get('serial', '') or ''
            if not (disk or model or serial) and isinstance(disk_info.get('full_event_data'), dict):
                raw = disk_info['full_event_data']
                model = model or raw.get('model') or raw.get('MODEL') or raw.get('Model') or ''
                serial = serial or raw.get('serial') or raw.get('SERIAL') or raw.get('Serial') or raw.get('sn') or raw.get('SN') or ''
                disk = disk or raw.get('disk') or raw.get('device') or raw.get('dev') or ''
            if disk:
                content += f"  磁盘设备: {disk}\n"
            if model:
                content += f"  硬盘型号: {model}\n"
            if serial:
                content += f"  序列号: {serial}\n"
            if not (disk or model or serial):
                fallback = self._format_disk_fallback(disk_info)
                if fallback:
                    content += f"  {fallback}\n"
                else:
                    content += "  （未解析到磁盘详情，请查看系统日志）\n"
            if i < len(merged_disks):
                content += "\n"

        return content

    def _build_merged_disk_spindown_content(self, event_data: Dict[str, Any]) -> str:
        """构建合并磁盘休眠事件内容"""
        content = ""

        merged_disks = event_data.get('merged_disks', [])
        for i, disk_info in enumerate(merged_disks, 1):
            content += f"【磁盘 #{i}】\n"
            disk = disk_info.get('disk', '') or ''
            model = disk_info.get('model', '') or ''
            serial = disk_info.get('serial', '') or ''
            if not (disk or model or serial) and isinstance(disk_info.get('full_event_data'), dict):
                raw = disk_info['full_event_data']
                model = model or raw.get('model') or raw.get('MODEL') or raw.get('Model') or ''
                serial = serial or raw.get('serial') or raw.get('SERIAL') or raw.get('Serial') or raw.get('sn') or raw.get('SN') or ''
                disk = disk or raw.get('disk') or raw.get('device') or raw.get('dev') or ''
            if disk:
                content += f"  磁盘设备: {disk}\n"
            if model:
                content += f"  硬盘型号: {model}\n"
            if serial:
                content += f"  序列号: {serial}\n"
            if not (disk or model or serial):
                fallback = self._format_disk_fallback(disk_info)
                if fallback:
                    content += f"  {fallback}\n"
                else:
                    content += "  （未解析到磁盘详情，请查看系统日志）\n"
            if i < len(merged_disks):
                content += "\n"

        return content

    def _build_app_lifecycle_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用生命周期事件内容（APP_STARTED/STOPPED/UPDATED 等，与 APP_CRASH 同结构）"""
        return self._build_app_crash_content(event_data)

    def _build_app_crash_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用崩溃事件内容"""
        content = ""
        data = event_data.get('data', {})

        if app_name := data.get('DISPLAY_NAME', data.get('APP_NAME', '')):
            content += f"应用名称: {app_name}\n"

        if app_id := data.get('APP_ID', ''):
            content += f"应用ID: {app_id}\n"

        if from_src := event_data.get('from', ''):
            content += f"来源模块: {from_src}\n"

        return content

    def _build_app_update_failed_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用更新失败事件内容"""
        content = ""
        data = event_data.get('data', {})

        if app_name := data.get('DISPLAY_NAME', data.get('APP_NAME', '')):
            content += f"应用名称: {app_name}\n"

        if app_id := data.get('APP_ID', ''):
            content += f"应用ID: {app_id}\n"

        if from_src := event_data.get('from', ''):
            content += f"来源模块: {from_src}\n"

        return content

    def _build_app_start_failed_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用启动失败（本地运行异常）事件内容"""
        return self._build_app_crash_content(event_data)

    def _build_app_auto_start_failed_content(self, event_data: Dict[str, Any]) -> str:
        """构建应用自启动失败（Docker 不可用）事件内容"""
        content = self._build_app_crash_content(event_data)
        content += "原因: Docker 服务不可用\n"
        return content

    def _build_cpu_usage_alarm_content(self, event_data: Dict[str, Any]) -> str:
        """构建 CPU 使用率告警内容（parameter 格式: data.THRESHOLD）"""
        content = ""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        content += f"使用率阈值: {threshold}%\n"
        return content

    def _build_cpu_usage_restored_content(self, event_data: Dict[str, Any]) -> str:
        """构建 CPU 使用率恢复内容（parameter 格式: data.THRESHOLD）"""
        content = ""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        content += f"使用率已恢复至阈值 {threshold}% 以下\n"
        return content

    def _build_memory_usage_alarm_content(self, event_data: Dict[str, Any]) -> str:
        """构建内存使用率告警内容（parameter: data.THRESHOLD）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        return f"内存使用率阈值: {threshold}%\n"

    def _build_memory_usage_restored_content(self, event_data: Dict[str, Any]) -> str:
        """构建内存使用率恢复内容（parameter: data.THRESHOLD）"""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        return f"内存使用率已恢复至阈值 {threshold}% 以下\n"

    def _build_cpu_temperature_alarm_content(self, event_data: Dict[str, Any]) -> str:
        """构建 CPU 温度告警内容"""
        content = ""
        data = event_data.get('data', {})
        threshold = data.get('THRESHOLD', 0)
        content += f"温度阈值: {threshold}°C\n"
        return content

    def _build_ups_onbatt_content(self, event_data: Dict[str, Any]) -> str:
        """构建UPS切换到电池供电事件内容（建议语见 EVENT_NOTES 概要）"""
        return "UPS状态: 切换到电池供电\n"

    def _build_ups_onbatt_lowbatt_content(self, event_data: Dict[str, Any]) -> str:
        """构建UPS电池电量低警告事件内容（建议语见 EVENT_NOTES 概要）"""
        return "UPS状态: 电池供电，电量过低\n"

    def _build_ups_online_content(self, event_data: Dict[str, Any]) -> str:
        """构建UPS切换到市电供电事件内容（补充说明见 EVENT_NOTES 概要）"""
        return "UPS状态: 已恢复市电供电\n"

    def _build_ups_enable_disable_content(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """构建开启/关闭 UPS 支持事件内容（parameter 含 data.USERNAME, from）"""
        content = ""
        data = event_data.get('data', {})
        if username := data.get('USERNAME', ''):
            content += f"操作用户: {username}\n"
        if from_src := event_data.get('from', ''):
            content += f"来源: {from_src}\n"
        return content

    def _build_system_content(self, event_type: str, event_data: Dict[str, Any], message: str) -> str:
        """构建系统事件消息内容：正文在前，「时间: 」行收尾（与事件消息一致）。"""
        ts = str(event_data.get("timestamp") or "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"{message}\n时间: {ts}"
        return self._strip_body_emojis(content)

    def _build_minimal_system_one_line(self, event_type: str, message: str) -> str:
        """极简模式下系统通知一行文案。"""
        prefix = (self.title_prefix or "").strip()
        action_map = {
            "APP_START": "监控启动",
            "APP_STOP": "监控停止",
            "APP_ERROR": "监控异常",
            "DND_SUMMARY": "勿扰汇总",
            "TEST_PUSH": "测试推送",
        }
        action = action_map.get(event_type, event_type)
        msg = (message or "").strip().replace("\n", " ")
        body = f"{action} {msg}".strip()
        if prefix:
            return f"{prefix}-{body}"
        return body

    def _append_album_owner_display_line(self, lines: List[str], event_data: Dict[str, Any]) -> None:
        """优先输出自动匹配到的用户名称；否则输出 UID/id 兜底信息。"""
        nick = self._album_nickname_from_map(
            event_data.get("owner_nas_uid"), event_data.get("owner_photo_user_id")
        )
        if nick:
            lines.append(f"用户: {nick}")
            return
        fb = (event_data.get("owner_label") or "").strip()
        if fb:
            lines.append(f"用户: {fb}")
