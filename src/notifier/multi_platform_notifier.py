"""
多平台通知器（发送编排核心）

支持企业微信、钉钉、飞书、Bark、PushPlus、SMTP、通用 Webhook、MeoW 的 WebHook 通知。
分层结构：
    message_format    消息体上限 / 截断 / markdown 转换 / 渠道注册表 / 富文本能力记忆
    event_texts       事件标题 / Bark 文案 / 备注 三张文案表
    channel_senders   各渠道实际投递（ChannelSendersMixin）
    content_builders  事件标题与正文文案构建（ContentBuildersMixin）
    message_render    整体消息组装与 markdown 预渲染（MessageRenderMixin）
    user_display      ——（用户名反查保留在本文件，见 _display_user）
本模块保留 MultiPlatformNotifier（编排：去重、磁盘事件合并、分发、健康统计）
并转发旧导入路径（normalize_disk_io_payload、MultiPlatformMessage 等），兼容既有调用方。
"""

import hashlib
import logging
import os
import re
import time
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from monitor.sqlite_uri import connect_readonly_with_fallback
from .channel_senders import ChannelSendersMixin
from .connection_pool import ConnectionPool, safe_url_for_log
from .content_builders import ContentBuildersMixin
from .event_texts import EVENT_NOTES, EVENT_TITLES
from .message_format import (
    CHANNEL_SPECS,
    ChannelSpec,
    DOCKER_CONTAINER_EVENT_TYPES,
    DINGTALK_TEXT_MAX_CHARS,
    DINGTALK_TITLE_MAX_CHARS,
    FEISHU_BODY_MAX_BYTES,
    MultiPlatformMessage,
    PUSHPLUS_URL,
    TRUNCATE_SUFFIX,
    WECOM_CONTENT_MAX_BYTES,
    _TRUNCATE_MARGIN,
    _feishu_post_content,
    _looks_like_format_rejection,
    _looks_like_security_rejection,
    _markdown_body_from_plain,
    _remember_rich_text_rejection,
    _rejection_text,
    _truncate_bytes,
    _truncate_chars,
    _truncate_utf8,
    normalize_disk_io_payload,
    reset_rich_text_rejected_channels,
    rich_text_rejected_channels,
)
from .message_render import MessageRenderMixin


# ── 渠道分发注册表（定义见 message_format，此处随导入转发）──────────────────────


class MultiPlatformNotifier(ChannelSendersMixin, ContentBuildersMixin, MessageRenderMixin):
    """多平台通知器（发送编排核心）"""

    BATCH_PER_TYPE_LIMIT = 3
    # 单一事件类型的批量汇总（如影视批量入库）明细上限：条目短，可展示更多
    BATCH_SINGLE_TYPE_LIMIT = 20
    BATCH_MAX_TYPES = 6
    BATCH_MAX_CHARS = 3200
    # Docker 容器事件类型（转发自 message_format，供 mixins 以 self. 引用）
    DOCKER_CONTAINER_EVENT_TYPES = DOCKER_CONTAINER_EVENT_TYPES
    _EMOJI_RE = re.compile(
        "["  # 常见 emoji / pictographs 范围（覆盖面足够用于“去表情展示”）
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # geometric extended
        "\U0001F800-\U0001F8FF"  # arrows-c
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA00-\U0001FAFF"  # symbols & pictographs extended
        "\u2600-\u27BF"          # misc symbols + dingbats
        "\u200d"                 # zero width joiner
        "\ufe0f"                 # variation selector
        "]+",
        flags=re.UNICODE,
    )
    
    # 三张文案表定义见 event_texts 模块；此处保留类属性访问兼容
    EVENT_TITLES = EVENT_TITLES

    EVENT_NOTES = EVENT_NOTES
    
    def __init__(self, 
                 wechat_webhook_url: str = "",
                 dingtalk_webhook_url: str = "",
                 feishu_webhook_url: str = "",
                 webhook_params: str = "",
                 title_prefix: str = "",
                 minimal_push_enabled: bool = False,
                 markdown_enabled: bool = True,
                 plain_emoji_enabled: bool = False,
                 poll_batch_summary_enabled: bool = False,
                 user_lookup_db_path: str = "",
                 activity_user_lookup_db_path: str = "",
                 logger_user_lookup_db_path: str = "",
                 dedup_window: int = 300,
                 pool_size: int = 10,
                 retries: int = 3,
                 timeout: int = 10,
                 merged_event_recorder: Optional[Callable[[str, int, bool, List[Dict[str, Any]], List[Dict[str, Any]]], None]] = None,
                 ):
        """
        初始化通知器

        Args:
            wechat_webhook_url: 企业微信Webhook URL
            dingtalk_webhook_url: 钉钉Webhook URL
            feishu_webhook_url: 飞书Webhook URL
            webhook_params: 通用 Webhook（JSON：url，可选 method/content_type/headers/body）
            dedup_window: 去重时间窗口（秒）
            pool_size: 连接池大小
            retries: 重试次数
            timeout: 超时时间
            merged_event_recorder: 合并事件（磁盘唤醒/休眠）真正发出后的回调，
                (event_type, count, success, channel_results, items)；用于把合并推送补记入
                推送统计与推送记录（合并发送发生在定时线程，不在 send_notification 调用栈内）。
                items 是本次合并的逐条明细（磁盘设备/型号/序列号），推送记录里要靠它
                才能展开看到「到底哪几块盘醒了」——只记条数的话，明细就只剩推送消息里
                那一份，事后无法回看。
                已移除渠道（Bark/PushPlus/SMTP/MeoW）不再出现在签名中；
                旧调用方以关键字传入会触发 TypeError，属预期——这些渠道已不再支持。
            """
        # 日志对象先于合并定时线程建立，避免线程异常分支访问未赋值的 self.logger
        self.logger = logging.getLogger(__name__)
        self.merged_event_recorder = merged_event_recorder
        self.wechat_webhook_url = wechat_webhook_url
        self.dingtalk_webhook_url = dingtalk_webhook_url
        self.feishu_webhook_url = feishu_webhook_url
        self.webhook_params = webhook_params or ""
        if not isinstance(title_prefix, str):
            self.title_prefix = ""
        else:
            self.title_prefix = (title_prefix or "").strip()
        self.minimal_push_enabled = bool(minimal_push_enabled)
        # 结构化消息（企业微信/钉钉 markdown、飞书 post）；不支持时自动回退纯文本
        self.markdown_enabled = bool(markdown_enabled)
        # 是否保留推送文案里的装饰性图标（默认不保留）
        self.plain_emoji_enabled = bool(plain_emoji_enabled)
        self.poll_batch_summary_enabled = bool(poll_batch_summary_enabled)
        self.user_lookup_db_path = (user_lookup_db_path or "").strip()
        self.activity_user_lookup_db_path = (activity_user_lookup_db_path or "").strip()
        self.logger_user_lookup_db_path = (logger_user_lookup_db_path or "").strip()
        self._user_db_cache = {}
        self._user_db_cache_loaded_at = 0.0
        self._nas_uid_name_cache = {}
        self._nas_uid_name_cache_loaded_at = 0.0
        self.dedup_window = dedup_window
        
        # 连接池
        self.connection_pool = ConnectionPool(
            pool_size=pool_size,
            max_retries=retries,
            timeout=timeout
        )
        
        # 事件去重缓存
        self.sent_events = {}
        
        # 磁盘事件合并缓存 - 使用时间窗口缓存多个磁盘事件
        self.disk_wakeup_cache = {}  # {time_window: [event_data_list]}
        self.disk_spindown_cache = {}  # {time_window: [event_data_list]}
        self.merge_window = 5  # 5秒合并窗口

        # 线程控制
        self._stop_flag = False
        self._cache_lock = threading.Lock()  # 保护磁盘事件缓存的线程锁

        # 发送健康状态
        self._health_lock = threading.Lock()
        self.last_attempt_time = None
        self.last_success_time = None
        self.consecutive_failures = 0
        self.first_failure_time = None
        self.total_failures_since_success = 0

        # 合并事件定时发送线程
        self._start_merge_timer()

        platforms = []
        if self.wechat_webhook_url:
            platforms.append('企业微信')
        if self.dingtalk_webhook_url:
            platforms.append('钉钉')
        if self.feishu_webhook_url:
            platforms.append('飞书')
        if self.webhook_params:
            platforms.append('通用Webhook')

        self.logger.info(f"多平台通知器初始化完成，支持平台: {', '.join(platforms) if platforms else '无'}, 去重窗口: {dedup_window}秒")

    def _record_send_result(self, success: bool):
        """记录发送结果用于健康监控"""
        now = time.time()
        with self._health_lock:
            self.last_attempt_time = now
            if success:
                self.last_success_time = now
                self.consecutive_failures = 0
                self.first_failure_time = None
                self.total_failures_since_success = 0
            else:
                if self.first_failure_time is None:
                    self.first_failure_time = now
                self.consecutive_failures += 1
                self.total_failures_since_success += 1

    def get_delivery_health(self) -> Dict[str, Any]:
        """获取通知发送健康状态"""
        with self._health_lock:
            return {
                'last_attempt_time': self.last_attempt_time,
                'last_success_time': self.last_success_time,
                'consecutive_failures': self.consecutive_failures,
                'first_failure_time': self.first_failure_time,
                'total_failures_since_success': self.total_failures_since_success,
                'active_platforms': {
                    'wechat': bool(self.wechat_webhook_url),
                    'dingtalk': bool(self.dingtalk_webhook_url),
                    'feishu': bool(self.feishu_webhook_url),
                    'webhook': bool(self.webhook_params),
                }
            }
    
    def _start_merge_timer(self):
        """启动合并事件定时处理线程"""
        self.timer_thread = threading.Thread(target=self._merge_timer_worker, daemon=True)
        self.timer_thread.start()
    
    def _merge_timer_worker(self):
        """合并事件定时处理工作线程"""
        while not self._stop_flag:
            try:
                # 检查并处理过期的合并事件
                current_time = time.time()
                current_window = int(current_time / self.merge_window)

                # 检查前一个窗口是否有待合并的事件
                prev_window = current_window - 1

                # 使用锁保护缓存访问
                with self._cache_lock:
                    # 处理待合并的磁盘唤醒事件
                    if prev_window in self.disk_wakeup_cache and self.disk_wakeup_cache[prev_window]:
                        self._send_merged_disk_event('DiskWakeup', self.disk_wakeup_cache[prev_window], prev_window)
                        del self.disk_wakeup_cache[prev_window]

                    # 处理待合并的磁盘休眠事件
                    if prev_window in self.disk_spindown_cache and self.disk_spindown_cache[prev_window]:
                        self._send_merged_disk_event('DiskSpindown', self.disk_spindown_cache[prev_window], prev_window)
                        del self.disk_spindown_cache[prev_window]

                    # 清理太久之前的缓存（超过2个窗口的）
                    too_old_window = current_window - 3
                    self.disk_wakeup_cache = {k: v for k, v in self.disk_wakeup_cache.items() if k > too_old_window}
                    self.disk_spindown_cache = {k: v for k, v in self.disk_spindown_cache.items() if k > too_old_window}

                # 使用短间隔睡眠以便快速响应停止信号
                for _ in range(10):
                    if self._stop_flag:
                        break
                    time.sleep(0.5)
            except Exception as e:
                self.logger.error(f"合并定时器工作线程出错: {e}", exc_info=True)
                if self._stop_flag:
                    break
    
    def _send_merged_disk_event(
        self, event_type: str, event_list: List[Dict], time_window: int = 0,
        notify_recorder: bool = True,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """发送合并的磁盘事件。event_list 为磁盘信息列表，每项含 disk/model/serial 等字段。
        返回 (是否至少一渠道成功, 各渠道结果列表)，与 send_notification 一致供推送记录展示。

        notify_recorder：是否通过合并事件回调「补记」推送统计与推送记录。
        补记是给**不经过 send_notification 调用栈**的调用方用的（合并窗口定时线程、
        关停时 flush）——它们没有别的地方记账。反之，当调用方本身就是
        send_notification（event_data 已带 merged_disks 的那一支）时，上层会自己写记录，
        再补记就会让同一次推送出现两条记录、统计加两次，故那一支传 False。
        """
        if not event_list:
            return False, []

        # 创建合并事件数据
        merged_data = {
            'merged_disks': event_list,
            'count': len(event_list),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self._minimal_push_effective():
            one_line = self._build_minimal_one_line(event_type, merged_data)
            message = MultiPlatformMessage(title=one_line, content="")
        else:
            title = self._with_title_prefix(
                self.EVENT_TITLES.get(event_type, self._fallback_event_title(event_type))
            )
            content = self._build_content(event_type, merged_data, ts, "")
            message = MultiPlatformMessage(title=self._clean_text(title), content=content)
        if self._markdown_wanted() and message.content:
            message.markdown = self._render_markdown_body(event_type, merged_data, ts, "")

        results, channel_results = self._dispatch_channels(message, log_prefix="合并事件")
        if results and any(results):
            self._record_send_result(True)
            self.logger.info(f"合并事件发送成功: {event_type}, 数量: {len(event_list)}")
            if notify_recorder:
                self._notify_merged_event_sent(
                    event_type, len(event_list), True, channel_results, event_list
                )
            return True, channel_results
        self._record_send_result(False)
        self.logger.warning(f"合并事件发送失败: {event_type}, 数量: {len(event_list)}")
        if notify_recorder:
            self._notify_merged_event_sent(
                event_type, len(event_list), False, channel_results, event_list
            )
        return False, channel_results

    def _dispatch_channels(
        self,
        message: MultiPlatformMessage,
        *,
        log_prefix: str,
    ) -> Tuple[List[bool], List[Dict[str, Any]]]:
        """按 CHANNEL_SPECS 依次投递到所有已配置渠道。

        未配置的渠道（param_attr 为空）直接跳过。
        返回 (各渠道成功标志, 各渠道结果)。
        """
        results: List[bool] = []
        channel_results: List[Dict[str, Any]] = []
        for spec in CHANNEL_SPECS:
            if not getattr(self, spec.param_attr, ""):
                continue
            ok, cr = getattr(self, spec.sender)(message)
            results.append(ok)
            channel_results.append(cr)
            self.logger.debug("%s-%s: %s", log_prefix, spec.label, cr)
        return results, channel_results

    def _notify_merged_event_sent(
        self,
        event_type: str,
        count: int,
        success: bool,
        channel_results: List[Dict[str, Any]],
        items: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """通知外层把「合并事件已真正发出」补记入统计与推送记录（回调可选，失败不影响推送）。"""
        cb = getattr(self, "merged_event_recorder", None)
        if cb is None:
            return
        try:
            cb(event_type, count, success, channel_results, items)
        except Exception as e:
            self.logger.warning("合并事件记录回调失败: %s", e)
    
    def send_notification(self, 
                         event_type: str,
                         event_data: Dict[str, Any],
                         raw_log: str,
                         timestamp: str):
        """
        发送通知
        
        Args:
            event_type: 事件类型
            event_data: 事件数据
            raw_log: 原始日志
            timestamp: 时间戳
            
        Returns:
            (success, channel_results): 是否发送成功；channel_results 为 [{"channel": "企业微信", "success": True}, ...]
        """
        # 特殊处理磁盘事件的合并
        if event_type in ['DiskWakeup', 'DiskSpindown']:
            merged_disks = event_data.get('merged_disks') if isinstance(event_data.get('merged_disks'), list) else None
            if merged_disks:
                # notify_recorder=False：本支就在 send_notification 调用栈内，
                # 上层（UnifiedNotifier）会依据返回的真实结果自己写记录与统计。
                success, crs = self._send_merged_disk_event(
                    event_type, merged_disks, 0, notify_recorder=False
                )
                return success, crs
            return self._handle_disk_event(event_type, event_data, raw_log, timestamp)
        
        # 生成事件指纹（用于去重）。NAS 定时巡检为按计划主动上报，不走日志风暴去重。
        skip_dedup = event_type == "NAS_PATROL_REPORT"
        event_fingerprint = ""
        if not skip_dedup:
            event_fingerprint = self._generate_fingerprint(event_type, event_data)
            if self._is_duplicate(event_fingerprint):
                self.logger.debug(f"跳过重复事件: {event_type}")
                return False, [
                    {
                        "channel": "推送去重",
                        "success": False,
                        "response": None,
                        "error": None,
                        "skipped": "duplicate",
                    }
                ]

        # 构建消息
        message = self._build_message(event_type, event_data, timestamp, raw_log)

        results, channel_results = self._dispatch_channels(message, log_prefix="通知")
        
        if results and any(results):  # 至少一个平台发送成功
            if not skip_dedup and event_fingerprint:
                self.sent_events[event_fingerprint] = time.time()
            self._record_send_result(True)
            self.logger.info(f"通知发送成功: {event_type}")
            return True, channel_results
        else:
            self._record_send_result(False)
            self.logger.warning(f"所有通知发送失败: {event_type}")
            return False, channel_results

    def _handle_disk_event(
        self, event_type: str, event_data: Dict[str, Any], raw_log: str, timestamp: str
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """处理磁盘事件：仅加入合并缓存，稍后由合并窗口统一发送。

        注意：本方法**不代表已发送**。返回结果带 skipped="deferred" 标记，供上层区分
        「已入队待合并」与「已投递成功」——否则推送统计/推送记录会把入队误记为成功，
        而真正发出合并通知时反而不计数。
        """
        # 获取当前时间窗口
        current_time = time.time()
        current_window = int(current_time / self.merge_window)

        # 使用锁保护缓存访问，防止竞态条件
        with self._cache_lock:
            # 将事件数据添加到对应类型的缓存中
            if event_type == 'DiskWakeup':
                if current_window not in self.disk_wakeup_cache:
                    self.disk_wakeup_cache[current_window] = []
                self.disk_wakeup_cache[current_window].append(event_data.copy())  # 复制数据以避免后续修改影响
            elif event_type == 'DiskSpindown':
                if current_window not in self.disk_spindown_cache:
                    self.disk_spindown_cache[current_window] = []
                self.disk_spindown_cache[current_window].append(event_data.copy())

        self.logger.debug(f"磁盘事件已加入合并队列: {event_type} -> 窗口 {current_window}")
        return True, [
            {
                "channel": "磁盘事件合并",
                "success": True,
                "response": None,
                "error": None,
                "skipped": "deferred",
            }
        ]
    
    def _source_fingerprint_key(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """源记录级指纹：同一源记录只推一次，不合并不同数据库行。"""
        for key in ("_source_key", "_source_cursor", "_source_row_id"):
            value = event_data.get(key)
            if value not in (None, ""):
                source = str(event_data.get("_source") or "db").strip() or "db"
                return f"{source}:{event_type}:{value}"
        return ""

    def _generate_fingerprint(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """生成事件指纹（用于去重）"""
        source_key = self._source_fingerprint_key(event_type, event_data)
        if source_key:
            return hashlib.md5(source_key.encode()).hexdigest()

        # 根据不同事件类型生成不同的指纹
        
        if event_type == 'FoundDisk':
            # 硬盘发现：按设备名和时间（小时）去重
            name = event_data.get('name', 'unknown')
            hour_window = int(time.time() / 3600)
            key = f"disk_{name}_{hour_window}"
        
        elif event_type == 'APP_CRASH':
            # 应用崩溃：按应用名和时间（5分钟）去重
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"crash_{app_name}_{minute_window}"
        
        elif event_type == 'APP_UPDATE_FAILED':
            # 应用更新失败：按应用名和时间（5分钟）去重
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"update_failed_{app_name}_{minute_window}"
        elif event_type in ['APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION', 'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE']:
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{app_name}_{minute_window}"
        elif event_type in ['APP_STARTED', 'APP_STOPPED', 'APP_UPDATED', 'APP_INSTALLED', 'APP_AUTO_STARTED', 'APP_UNINSTALLED']:
            data = event_data.get('data', {})
            app_name = data.get('DISPLAY_NAME', data.get('APP_NAME', 'unknown'))
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{app_name}_{minute_window}"
        elif event_type in [
            'CPU_USAGE_ALARM', 'CPU_USAGE_RESTORED',
            'MEMORY_USAGE_ALARM', 'MEMORY_USAGE_RESTORED',
            'CPU_TEMPERATURE_ALARM',
        ]:
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{minute_window}"
        
        elif event_type == 'UPS_ONBATT_LOWBATT':
            # UPS切换到电池供电：按时间（5分钟）去重
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"ups_battery_{minute_window}"
        
        elif event_type == 'UPS_ONLINE':
            # UPS切换到市电供电：按时间（5分钟）去重
            minute_window = int(time.time() / 300)  # 5分钟窗口
            key = f"ups_online_{minute_window}"
        elif event_type in ['UPS_ENABLE', 'UPS_DISABLE']:
            minute_window = int(time.time() / 300)
            key = f"{event_type}_{minute_window}"
        elif event_type in ['SSH_INVALID_USER', 'SSH_AUTH_FAILED', 'SSH_LOGIN_SUCCESS', 'SSH_DISCONNECTED']:
            # SSH事件：按用户/IP和时间（分钟）去重
            user = event_data.get('user', 'unknown')
            ip = event_data.get('IP', 'unknown')
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{user}_{ip}_{minute_window}"
        elif event_type == 'DISK_IO_ERR':
            dev, _, _, _ = normalize_disk_io_payload(event_data)
            dev_key = dev or "unknown"
            minute_window = int(time.time() / 60)
            key = f"disk_io_err_{dev_key}_{minute_window}"
        elif event_type == 'STORAGE_DEGRADED':
            vol = event_data.get('volume', event_data.get('VOL'))
            vol_key = str(vol).strip() if vol not in (None, '') else 'unknown'
            minute_window = int(time.time() / 60)
            key = f"storage_degraded_{vol_key}_{minute_window}"
        elif event_type in DOCKER_CONTAINER_EVENT_TYPES:
            cid = str(event_data.get("container_id_full") or event_data.get("container_id") or "unknown")
            nano_raw = event_data.get("engine_time_nano") or 0
            try:
                nano = int(nano_raw)
            except (TypeError, ValueError):
                nano = 0
            if nano > 0:
                key = f"{event_type}_{cid}_{nano}"
            else:
                minute_window = int(time.time() / 60)
                key = f"{event_type}_{cid}_{minute_window}"
        elif event_type in (
            "TRIM_RESOURCE_ADDED",
            "TRIM_SCRAPE_SUCCESS",
            "TRIM_RESOURCE_DELETED",
            "MEDIA_USER_CREATED",
        ):
            gid = str(event_data.get("guid") or "").strip()
            if gid:
                key = f"{event_type}_{gid}"
            else:
                path = str(event_data.get("path") or "")
                title = str(event_data.get("title") or "")
                msg = str(event_data.get("message") or "")
                digest = hashlib.md5(
                    f"{path}|{title}|{msg}".encode("utf-8", errors="ignore")
                ).hexdigest()[:20]
                key = f"{event_type}_{digest}"
        elif event_type in (
            "PHOTO_SHARE_CREATED",
            "PHOTO_SHARE_EXPIRED",
            "PHOTO_DEVICE_REGISTERED",
            "FACE_RECOGNITION_UPDATED",
        ):
            if event_type == "PHOTO_SHARE_CREATED":
                sid = int(event_data.get("share_link_id") or 0)
                ext = str(event_data.get("share_id") or "").strip()
                key = f"{event_type}_{sid or ext or 'na'}"
            elif event_type == "PHOTO_SHARE_EXPIRED":
                key = (
                    f"{event_type}_"
                    f"{str(event_data.get('share_id') or '').strip()}_"
                    f"{str(event_data.get('expired_at_str') or '')}"
                )
            elif event_type == "PHOTO_DEVICE_REGISTERED":
                key = (
                    f"{event_type}_"
                    f"{str(event_data.get('device_id') or '').strip()}_"
                    f"{str(event_data.get('device_uniq_name') or '').strip()}"
                )
            else:
                key = f"{event_type}_{int(event_data.get('task_log_id') or 0)}"
        elif event_type in ("BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS"):
            op = event_data.get("operation_id")
            if op is not None and str(op).strip() != "":
                key = f"{event_type}_{op}"
            else:
                minute_window = int(time.time() / 60)
                tn = str(event_data.get("task_name") or "")
                key = f"{event_type}_{tn}_{minute_window}"
        elif event_type in (
            "SCHEDULER_TASK_SUCCESS",
            "SCHEDULER_TASK_FAILED",
            "SCHEDULER_TASK_CONDITION_FAILED",
        ):
            rid = event_data.get("result_id")
            if rid is not None and str(rid).strip() != "":
                key = f"{event_type}_{rid}"
            else:
                minute_window = int(time.time() / 60)
                key = f"{event_type}_{str(event_data.get('task_name') or '')}_{minute_window}"
        elif event_type in {
            'ARCHIVING_SUCCESS', 'DeleteFile', 'MovetoTrashbin', 'SHARE_EVENTID_DEL', 'SHARE_EVENTID_PUT',
            'WEBDAV_ENABLED', 'WEBDAV_DISABLED', 'SAMBA_ENABLED', 'SAMBA_DISABLED',
            'DLNA_ENABLED', 'DLNA_DISABLED', 'FTP_ENABLED', 'FTP_DISABLED', 'NFS_ENABLED', 'NFS_DISABLED',
            'FW_ENABLE', 'FW_DISABLE', 'SECURITY_PORTCHANGED',
        }:
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{minute_window}"
        elif event_type == 'WAN_IP_CHANGED':
            old_ip = str(event_data.get('old_wan_ip') or event_data.get('previous_wan_ip') or '')
            new_ip = str(event_data.get('new_wan_ip') or event_data.get('wan_ip') or '')
            key = f"{event_type}_{old_ip}_{new_ip}"
        elif event_type == 'POLL_BATCH_SUMMARY':
            minute_window = int(time.time() / 60)
            count = int(event_data.get('count') or 0)
            by_type_raw = event_data.get('by_type')
            by_type: Dict[str, Any] = by_type_raw if isinstance(by_type_raw, dict) else {}
            type_count = len(by_type)
            # 避免同一分钟内“总数和类型数相同但内容不同”的批次被误去重
            by_type_sig = "|".join(f"{k}:{by_type[k]}" for k in sorted(by_type.keys()))
            items_raw = event_data.get('items')
            items: List[Dict[str, Any]] = items_raw if isinstance(items_raw, list) else []
            preview_sig_parts = []
            for it in items[:5]:
                if not isinstance(it, dict):
                    continue
                et = str(it.get('event_type') or '')
                ts = str(it.get('timestamp') or '')
                brief = str(it.get('brief') or '')[:30]
                preview_sig_parts.append(f"{et}@{ts}#{brief}")
            preview_sig = "|".join(preview_sig_parts)
            key = f"{event_type}_{minute_window}_{count}_{type_count}_{by_type_sig}_{preview_sig}"
        elif event_type == "NAS_PATROL_REPORT":
            key = f"{event_type}_{int(time.time())}"
        else:
            # 登录/退出：按用户、IP和时间（分钟）去重
            user = event_data.get('user', 'unknown')
            ip = event_data.get('IP', 'unknown')
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{user}_{ip}_{minute_window}"
        
        # 使用MD5生成固定长度的指纹
        return hashlib.md5(key.encode()).hexdigest()
    
    def _is_duplicate(self, fingerprint: str) -> bool:
        """检查是否为重复事件"""
        if fingerprint in self.sent_events:
            last_time = self.sent_events[fingerprint]
            if time.time() - last_time < self.dedup_window:
                return True
            else:
                # 超过去重窗口，删除旧记录
                del self.sent_events[fingerprint]
        
        return False

    def _display_user(self, user_value: Any) -> str:
        """将 user_guid 等标识转换为可读用户名（命中映射时）。"""
        raw = str(user_value or "").strip()
        if not raw:
            return ""
        # 从 trimmedia.db 的 user(guid, username) 自动映射
        db_name = self._lookup_username_from_db(raw)
        if db_name:
            return db_name
        return raw

    def _lookup_username_from_db(self, guid: str) -> str:
        """从媒体相关数据库自动读取 guid 对应可读用户名（带短时缓存）。"""
        if not guid:
            return ""
        db_paths = []
        if self.user_lookup_db_path and os.path.exists(self.user_lookup_db_path):
            db_paths.append(self.user_lookup_db_path)
        if self.activity_user_lookup_db_path and os.path.exists(self.activity_user_lookup_db_path):
            db_paths.append(self.activity_user_lookup_db_path)
        if not db_paths:
            return ""
        now = time.time()
        # 60 秒缓存，避免每条推送都查库
        if now - self._user_db_cache_loaded_at > 60:
            mapping = {}

            def _absorb(rows):
                for g, u in rows:
                    sg = str(g or "").strip()
                    su = str(u or "").strip()
                    if sg and su and sg not in mapping:
                        mapping[sg] = su

            for db_path in db_paths:
                conn = connect_readonly_with_fallback(
                    db_path,
                    timeout=3.0,
                    prefer_immutable=True,
                )
                try:
                    # 静态白名单查询（表/列来自飞牛 trimmedia 库已知结构），逐个尝试
                    try:
                        _absorb(conn.execute('SELECT "guid", "username" FROM "user" WHERE "guid" IS NOT NULL AND "username" IS NOT NULL').fetchall())
                    except Exception:
                        pass
                    try:
                        _absorb(conn.execute('SELECT "user_guid", "username" FROM "user" WHERE "user_guid" IS NOT NULL AND "username" IS NOT NULL').fetchall())
                    except Exception:
                        pass
                finally:
                    conn.close()

            if mapping:
                self._user_db_cache = mapping
            else:
                self.logger.warning(
                    "用户名映射不可用：媒体库未找到 user(guid, username) 表，"
                    "影视事件将显示原始 guid（如飞牛更新了表结构请提 issue）"
                )
            self._user_db_cache_loaded_at = now
        return self._user_db_cache.get(guid, "")
        return self._user_db_cache.get(guid, "")

    def _lookup_nas_uid_name_from_logger_db(self, nas_uid: Any) -> str:
        """从 logger_data.db3 的 log(uid, uname) 自动映射 NAS UID -> 昵称。"""
        try:
            uid = int(nas_uid) if nas_uid is not None and str(nas_uid).strip() != "" else None
        except (TypeError, ValueError):
            uid = None
        if uid is None or not self.logger_user_lookup_db_path:
            return ""
        if not os.path.exists(self.logger_user_lookup_db_path):
            return ""
        now = time.time()
        if now - self._nas_uid_name_cache_loaded_at > 60:
            try:
                conn = connect_readonly_with_fallback(
                    self.logger_user_lookup_db_path,
                    timeout=3.0,
                    prefer_immutable=True,
                )
                cur = conn.execute(
                    "SELECT uid, uname FROM log WHERE uid IS NOT NULL AND uname IS NOT NULL "
                    "AND TRIM(uname) != '' ORDER BY id DESC LIMIT 20000"
                )
                mapping = {}
                for u, n in cur.fetchall():
                    su = str(u or "").strip()
                    sn = str(n or "").strip()
                    if su and sn and su not in mapping:
                        mapping[su] = sn
                conn.close()
                self._nas_uid_name_cache = mapping
                self._nas_uid_name_cache_loaded_at = now
            except Exception:
                return ""
        return self._nas_uid_name_cache.get(str(uid), "")

    def _album_nickname_from_map(self, nas_uid: Any, _photo_user_id: Any) -> str:
        """相册所有者/设备所属用户：自动从 logger_data.db3 反查用户名称。"""
        try:
            nuid = int(nas_uid) if nas_uid is not None and str(nas_uid).strip() != "" else None
        except (TypeError, ValueError):
            nuid = None
        # 自动从 logger_data.db3 反查 UID 的最近用户名
        auto_name = self._lookup_nas_uid_name_from_logger_db(nuid)
        if auto_name:
            return auto_name
        return ""

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
    
    def send_system_notification(self, event_type: str, message: str, additional_info: Optional[Dict[str, Any]] = None):
        """
        发送系统事件通知
        
        Args:
            event_type: 事件类型 ('APP_START', 'APP_STOP', 'APP_ERROR', 'DND_SUMMARY')
            message: 详细消息
            additional_info: 额外信息字典
            
        Returns:
            dict: success、success_count、fail_count、channel_results（每渠道发送明细）；
            去重跳过时有 skipped=\"duplicate\"，channel_results 为空。
        """
        self.logger.debug(f"准备发送系统事件通知: {event_type}")
        
        # 构建事件数据
        event_data = {
            'message': message,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'hostname': additional_info.get('hostname', '') if additional_info else '',
            'version': additional_info.get('version', '1.0') if additional_info else '1.0',
        }
        
        # 生成事件指纹
        event_fingerprint = self._generate_system_fingerprint(event_type, event_data)
        
        # 检查去重
        if self._is_duplicate(event_fingerprint):
            self.logger.debug(f"跳过重复系统事件: {event_type}")
            return {
                "success": False,
                "success_count": 0,
                "fail_count": 0,
                "channel_results": [],
                "skipped": "duplicate",
            }

        # 构建消息（勿扰汇总始终完整文案）
        if self._minimal_push_effective() and event_type != "DND_SUMMARY":
            one_line = self._build_minimal_system_one_line(event_type, message)
            multi_msg = MultiPlatformMessage(title=one_line, content="")
        else:
            title = self._with_title_prefix(
                self.EVENT_TITLES.get(event_type, self._fallback_event_title(event_type))
            )
            content = self._build_system_content(event_type, event_data, message)
            multi_msg = MultiPlatformMessage(title=self._clean_text(title), content=content)
            if self._markdown_wanted() and content:
                multi_msg.markdown = _markdown_body_from_plain(self._clean_text(content))
        
        results, channel_results = self._dispatch_channels(multi_msg, log_prefix="系统通知")
        success_count = sum(1 for r in results if r)
        fail_count = len(results) - success_count
        any_ok = bool(results and success_count > 0)
        if any_ok:
            self.sent_events[event_fingerprint] = time.time()
            self._record_send_result(True)
            self.logger.info(f"系统事件通知发送成功: {event_type}")
        else:
            self._record_send_result(False)
            self.logger.warning(f"系统事件通知发送失败: {event_type}")
        return {
            "success": any_ok,
            "success_count": success_count,
            "fail_count": fail_count,
            "channel_results": channel_results,
        }
    
    def _generate_system_fingerprint(self, event_type: str, event_data: Dict[str, Any]) -> str:
        """生成系统事件指纹（用于去重）"""
        # 根据事件类型生成不同的指纹
        if event_type == 'APP_START':
            # 启动事件：按小时去重
            hour_window = int(time.time() / 3600)
            key = f"{event_type}_{hour_window}"
        elif event_type == 'APP_STOP':
            # 停止事件：按分钟去重
            minute_window = int(time.time() / 60)
            key = f"{event_type}_{minute_window}"
        elif event_type == 'APP_ERROR':
            # 错误事件：按5分钟去重
            window = int(time.time() / 300)  # 5分钟窗口
            key = f"{event_type}_{window}"
        elif event_type == 'TEST_PUSH':
            # 测试推送：每次发送独立，不去重
            key = f"TEST_PUSH_{time.time()}"
        else:
            # 其他事件：按分钟去重
            minute_window = int(time.time() / 60)
            key = f"sys_{event_type}_{minute_window}"
        
        # 使用MD5生成固定长度的指纹
        return hashlib.md5(key.encode()).hexdigest()
    
    def cleanup_cache(self):
        """清理过期的缓存"""
        current_time = time.time()
        expired_keys = [
            key for key, ts in self.sent_events.items()
            if current_time - ts > self.dedup_window * 2
        ]
        
        for key in expired_keys:
            del self.sent_events[key]
        
        if expired_keys:
            self.logger.debug(f"清理了 {len(expired_keys)} 个过期缓存")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        pool_stats = self.connection_pool.get_stats()
        
        return {
            **pool_stats,
            'cache_size': len(self.sent_events),
            'dedup_window': self.dedup_window,
            'has_wechat_webhook': bool(self.wechat_webhook_url),
            'has_dingtalk_webhook': bool(self.dingtalk_webhook_url),
            'has_feishu_webhook': bool(self.feishu_webhook_url),
            'disk_wakeup_cache_size': len(self.disk_wakeup_cache),
            'disk_spindown_cache_size': len(self.disk_spindown_cache),
            'merge_window': self.merge_window,
            # 脱敏：原先只截断前 50 字符，而飞书 webhook 的密钥紧随 46 字符前缀，等于泄露密钥
            'wechat_webhook_url': safe_url_for_log(self.wechat_webhook_url),
            'dingtalk_webhook_url': safe_url_for_log(self.dingtalk_webhook_url),
            'feishu_webhook_url': safe_url_for_log(self.feishu_webhook_url),
        }
    
    def close(self):
        """关闭通知器"""
        self.logger.debug("正在关闭多平台通知器...")

        # 设置停止标志
        self._stop_flag = True

        # 刷新所有待发送的磁盘事件
        self._flush_pending_disk_events()

        # 等待定时器线程结束
        if hasattr(self, 'timer_thread') and self.timer_thread.is_alive():
            self.timer_thread.join(timeout=10)

        # 关闭连接池
        self.connection_pool.close()

        # 清理缓存
        self.cleanup_cache()

        self.logger.debug("多平台通知器已关闭")

    def _flush_pending_disk_events(self):
        """刷新所有待发送的磁盘事件"""
        try:
            with self._cache_lock:
                # 发送所有待发送的磁盘唤醒事件
                for time_window, event_list in self.disk_wakeup_cache.items():
                    if event_list:
                        self.logger.info(f"刷新待发送的磁盘唤醒事件: {len(event_list)} 个")
                        self._send_merged_disk_event('DiskWakeup', event_list, time_window)

                # 发送所有待发送的磁盘休眠事件
                for time_window, event_list in self.disk_spindown_cache.items():
                    if event_list:
                        self.logger.info(f"刷新待发送的磁盘休眠事件: {len(event_list)} 个")
                        self._send_merged_disk_event('DiskSpindown', event_list, time_window)

                # 清空缓存
                self.disk_wakeup_cache.clear()
                self.disk_spindown_cache.clear()
        except Exception as e:
            self.logger.error(f"刷新待发送事件时出错: {e}", exc_info=True)
