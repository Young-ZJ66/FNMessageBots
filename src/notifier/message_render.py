"""消息渲染层：整体消息（标题 + 正文 + markdown）的组装。

MessageRenderMixin —— 依赖宿主（MultiPlatformNotifier）与 ContentBuildersMixin 提供的
_build_content / EVENT_TITLES / title_prefix 等能力；这里只负责：
选标题（含批量汇总的条数标题）、预渲染 markdown、极简一行文案。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .message_format import MultiPlatformMessage, _markdown_body_from_plain


class MessageRenderMixin:
    """消息组装（宿主需提供 EVENT_TITLES / _build_content / _clean_text 等）。"""

    def _minimal_push_effective(self) -> bool:
        """极简是否实际生效：开启轮询汇总时关闭极简（汇总需完整正文）。"""
        return bool(self.minimal_push_enabled) and not bool(self.poll_batch_summary_enabled)

    def _build_message(self, event_type: str, event_data: Dict[str, Any],
                      timestamp: str, raw_log: str) -> MultiPlatformMessage:
        """构建多平台消息"""
        # 轮询批量汇总、NAS 定时巡检：不受极简模式折叠为一行
        if self._minimal_push_effective() and event_type not in ("POLL_BATCH_SUMMARY", "NAS_PATROL_REPORT"):
            one_line = self._build_minimal_one_line(event_type, event_data)
            return MultiPlatformMessage(title=one_line, content="")

        title_event_type = event_type
        # 批量汇总若仅包含单一事件类型，则标题回退为该事件原始标题；多于一条时带上条数
        if event_type == 'POLL_BATCH_SUMMARY':
            try:
                total = int(event_data.get('count') or 0)
            except Exception:
                total = 0
            by_type_raw = event_data.get('by_type')
            by_type: Dict[str, Any] = by_type_raw if isinstance(by_type_raw, dict) else {}
            if total >= 1 and len(by_type) == 1:
                title_event_type = str(next(iter(by_type.keys())))
                if total > 1:
                    base = self._with_title_prefix(
                        self.EVENT_TITLES.get(title_event_type, self._fallback_event_title(title_event_type))
                    )
                    title = f"{self._clean_text(base)}（共 {total} 条）"
                    content = self._build_content(event_type, event_data, timestamp, raw_log)
                    msg = MultiPlatformMessage(title=title, content=content)
                    if self._markdown_wanted():
                        msg.markdown = self._render_markdown_body(event_type, event_data, timestamp, raw_log)
                    return msg

        if event_type == "NAS_PATROL_REPORT":
            # 与 _build_nas_patrol_content 原首行一致；正文不再重复标题（merged_plain_text 会拼接 title）
            pfx = (self.title_prefix or "").strip()
            title = f"{pfx}-巡检任务完成" if pfx else "飞牛NAS-巡检任务完成"
        else:
            title = self._with_title_prefix(
                self.EVENT_TITLES.get(title_event_type, self._fallback_event_title(title_event_type))
            )
        title = self._clean_text(title)
        content = self._build_content(event_type, event_data, timestamp, raw_log)
        msg = MultiPlatformMessage(title=title, content=content)
        if self._markdown_wanted():
            msg.markdown = self._render_markdown_body(event_type, event_data, timestamp, raw_log)
        return msg

    def _markdown_wanted(self) -> bool:
        """是否需要预渲染结构化（markdown）正文。"""
        return bool(self.markdown_enabled)

    def _render_markdown_body(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        timestamp: str,
        raw_log: str,
    ) -> str:
        """生成 markdown 正文：概要在明处、明细整体收进「>」引用块。

        版式（引用块外 → 引用块内）：
            时间行 + 备注概要（EVENT_NOTES，紧随时间之后）
            明细正文（键值/列表，逐行加「>」前缀；空行保持引用块连续）
        不做二次业务渲染，避免主文案与结构化文案出现信息差异。
        """
        try:
            ts_line, body, note = self._build_content_parts(event_type, event_data, timestamp, raw_log)
        except Exception:
            return ""
        segments: List[str] = []
        if ts_line:
            segments.append(self._clean_text(ts_line))
        if note:
            segments.append(self._clean_text(note))
        if body.strip():
            # 注意：正文不能过 _clean_text（其整体 strip 会吃掉首行缩进，破坏列表项识别），
            # 用逐行去图标、保留缩进的 _strip_body_emojis
            quoted = _markdown_body_from_plain(self._strip_body_emojis(body))
            if quoted.strip():
                quote_lines = [f"> {ln}" if ln.strip() else ">" for ln in quoted.split("\n")]
                segments.append("\n".join(quote_lines))
        return "\n\n".join(segments)

    def _disk_minimal_label(self, disk_info: Dict[str, Any]) -> str:
        """单块磁盘在极简推送中的短标识（设备名优先）。"""
        d = (disk_info.get("disk") or "").strip()
        if d:
            return d
        model = (disk_info.get("model") or "").strip()
        if model:
            return model
        serial = (disk_info.get("serial") or "").strip()
        if serial:
            return serial[:32]
        if isinstance(disk_info.get("full_event_data"), dict):
            fb = self._format_disk_fallback(disk_info)
            if fb:
                return fb[:80]
        return ""

    def _minimal_disk_body(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """磁盘唤醒/休眠极简正文（不含 title_prefix）。"""
        verb = "磁盘唤醒" if event_type == "DiskWakeup" else "磁盘休眠"
        merged_raw = event_data.get("merged_disks")
        if isinstance(merged_raw, list) and merged_raw:
            labels: List[str] = []
            for x in merged_raw:
                if isinstance(x, dict):
                    lab = self._disk_minimal_label(x)
                    if lab:
                        labels.append(lab)
            n = len(merged_raw)
            if n == 1:
                return f"{verb} {labels[0]}" if labels else verb
            if labels:
                joined = "、".join(labels[:5])
                if len(labels) > 5:
                    joined += "…"
                if len(labels) < n or len(labels) > 5:
                    return f"{verb}×{n} {joined}"
                return f"{verb} {joined}"
            return f"{verb}×{n}"
        lab = self._disk_minimal_label(event_data) if isinstance(event_data, dict) else ""
        return f"{verb} {lab}" if lab else verb

    def _build_minimal_one_line(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """极简推送：仅一行核心信息。"""
        if event_type in ("DiskWakeup", "DiskSpindown"):
            body = self._minimal_disk_body(event_type, event_data)
            prefix = (self.title_prefix or "").strip()
            if prefix:
                return f"{prefix}-{body}"
            return body

        if event_type == "STORAGE_DEGRADED":
            vol = event_data.get("volume") or event_data.get("VOL")
            if vol is not None and str(vol).strip() != "":
                body = f"存储空间{vol}已降级"
            else:
                body = "存储空间降级"
            prefix = (self.title_prefix or "").strip()
            if prefix:
                return f"{prefix}-{body}"
            return body

        if event_type in self.DOCKER_CONTAINER_EVENT_TYPES:
            nm = (event_data.get("container_name") or "").strip()
            cid = (event_data.get("container_id") or "").strip()
            label = nm or cid or "容器"
            verb = self._strip_body_emojis(
                self.EVENT_TITLES.get(event_type, "").replace("飞牛NAS-", "").replace("飞牛NAS", "").strip(" -")
            ) or event_type
            body = f"{label} {verb}".strip()
            prefix = (self.title_prefix or "").strip()
            if prefix:
                return f"{prefix}-{body}"
            return body

        if event_type == "WAN_IP_CHANGED":
            old_ip = event_data.get("old_wan_ip") or event_data.get("previous_wan_ip") or "?"
            new_ip = event_data.get("new_wan_ip") or event_data.get("wan_ip") or "?"
            body = f"外网IP变化 {old_ip}→{new_ip}"
            prefix = (self.title_prefix or "").strip()
            if prefix:
                return f"{prefix}-{body}"
            return body

        user = self._display_user(
            event_data.get("user")
            or event_data.get("user_guid")
            or event_data.get("uname")
            or ""
        )
        app_name = (event_data.get("app_name") or "").strip()
        task_name = (event_data.get("task_name") or "").strip()
        title = (event_data.get("title") or event_data.get("name") or "").strip()

        action_map = {
            "LoginSucc": "登录成功",
            "LoginSucc2FA1": "二次验证登录",
            "LoginFail": "登录失败",
            "Logout": "退出登录",
            "MEDIA_LOGIN_SUCC": "影视库登录成功",
            "MEDIA_LOGOUT": "影视库退出登录",
            "MEDIA_USER_CREATED": "影视库创建用户",
            "TRIM_RESOURCE_ADDED": "影视资源入库",
            "TRIM_SCRAPE_SUCCESS": "影视刮削完成",
            "TRIM_RESOURCE_DELETED": "影视资源删除",
            "APP_CRASH": "应用崩溃",
            "APP_UPDATE_FAILED": "应用更新失败",
            "BACKUP_TASK_SUCCESS": "备份任务完成",
            "BACKUP_TASK_FAILED": "备份任务失败",
            "BACKUP_TASK_PARTIAL_SUCCESS": "备份任务部分成功",
            "SCHEDULER_TASK_SUCCESS": "任务计划完成",
            "SCHEDULER_TASK_FAILED": "任务计划失败",
            "SCHEDULER_TASK_CONDITION_FAILED": "任务计划条件不满足",
            "DOCKER_CONTAINER_CREATE": "Docker容器创建",
            "DOCKER_CONTAINER_START": "Docker容器启动",
            "DOCKER_CONTAINER_STOP": "Docker容器停止",
            "DOCKER_CONTAINER_DIE": "Docker容器退出",
            "DOCKER_CONTAINER_OOM": "Docker容器OOM",
            "DOCKER_CONTAINER_KILL": "Docker容器Kill",
            "DOCKER_CONTAINER_PAUSE": "Docker容器暂停",
            "DOCKER_CONTAINER_UNPAUSE": "Docker容器恢复",
            "DOCKER_CONTAINER_RESTART": "Docker容器重启",
            "DOCKER_CONTAINER_DESTROY": "Docker容器删除",
        }
        action = action_map.get(event_type)
        if not action:
            fallback_title = self.EVENT_TITLES.get(event_type, event_type)
            action = self._strip_body_emojis(fallback_title)
            action = action.replace("飞牛NAS-", "").replace("飞牛NAS", "").strip(" -")

        subject = user or app_name or task_name or title
        prefix = (self.title_prefix or "").strip()
        body = f"{action} {subject}".strip() if subject else action
        if prefix:
            return f"{prefix}-{body}"
        return body
