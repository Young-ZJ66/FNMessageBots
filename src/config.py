"""
配置管理模块
"""

import copy
import os
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field, fields
from pathlib import Path
from notifier.channel_params import validate_channel_json
from utils.value_parser import as_bool
from valid_event_ids import filter_monitor_events

_logger = logging.getLogger(__name__)

# 推送标题前缀默认值；配置文件无 title_prefix 项时使用。仅当配置里显式存空字符串时才关闭前缀。
TITLE_PREFIX_DEFAULT = "飞牛NAS"

# 已移除的渠道对应的历史配置键：保留在 config.json 里不再生效，
# 启动时提示一次即可（不报错、不删除用户文件，避免升级即崩）。
_RETIRED_CHANNEL_KEYS = ("magic_push_params", "wecom_app_params")

# 布尔型配置项：config.json 里可能被写成字符串（"1"/"true"/"0"/"false"），
# 通用加载循环必须归一化，否则 "0"/"false" 会被当成真值。
_BOOL_CONFIG_KEYS = frozenset({
    "minimal_push_enabled",
    "push_markdown_enabled",
    "push_emoji_enabled",
    "resume_from_cursor_on_start",
    "ssh_ignore_loopback",
    "media_lib_logger_enabled",
    "dnd_enabled",
    "poll_batch_summary_enabled",
    "media_batch_summary_enabled",
    "nas_patrol_enabled",
    "notification_restart_enabled",
})


@dataclass
class Config:
    """应用配置"""
    
    # Webhook配置
    wechat_webhook_url: str = ""  # 企业微信Webhook URL
    dingtalk_webhook_url: str = ""  # 钉钉Webhook URL
    feishu_webhook_url: str = ""   # 飞书Webhook URL
    webhook_params: str = ""  # 通用 Webhook（JSON：url，可选 method/content_type/headers/body；多个用 | 分隔）

    # 通知标题配置；默认「飞牛NAS」。仅在配置中显式写入空字符串时，推送标题不包含此前缀。
    title_prefix: str = field(default=TITLE_PREFIX_DEFAULT)
    # 极简推送：开启后所有渠道仅推送一行关键信息（默认关闭）
    minimal_push_enabled: bool = False
    # 结构化消息：企业微信 markdown、钉钉 markdown、飞书富文本 post（默认开启；渠道不支持时自动回退纯文本）
    push_markdown_enabled: bool = True
    # 推送文案是否保留装饰性图标（emoji）。默认关闭，保持文案简洁
    push_emoji_enabled: bool = False
    # 忽略本机回环（127.0.0.1 / ::1）的 SSH 登录成功与断开；SSH 经内网穿透接入时来源同为回环，勿开启
    ssh_ignore_loopback: bool = False

    # 监控配置
    monitor_events: List[str] = field(default_factory=lambda: [
        "LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout", "FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED", "APP_CRASH",
        "APP_UPDATE_FAILED", "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
        "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",         "CPU_USAGE_ALARM",
        "CPU_USAGE_RESTORED",
        "MEMORY_USAGE_ALARM",
        "MEMORY_USAGE_RESTORED",
        "CPU_TEMPERATURE_ALARM", "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE",
        "UPS_ENABLE", "UPS_DISABLE",
        "DiskWakeup", "DiskSpindown",
        "SSH_INVALID_USER", "SSH_AUTH_FAILED",
        "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
        "DISK_IO_ERR",
        "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    ])
    
    # 日志配置
    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30  # 原始推送日志保留天数
    
    # 连接池配置
    http_pool_size: int = 10
    http_retry_count: int = 3
    http_timeout: int = 10
    dedup_window: int = 300
    
    # 系统路径配置（默认空：请在 config.json 或环境变量 LOGGER_DB_PATH / BACKUP_DB_PATH 等中配置）
    cursor_dir: str = "./data/cursor"  # 数据库轮询游标等
    logger_db_path: str = ""
    backup_db_path: str = ""
    # 影视库：logger 中按 serviceId/parameter 匹配；trimmedia / trimactivity 路径为空则不启用对应轮询
    media_lib_logger_enabled: bool = False
    media_lib_service_patterns: List[str] = field(default_factory=lambda: ["mediadb", "trimmedia"])
    media_lib_app_name_patterns: List[str] = field(default_factory=lambda: ["影视"])
    trim_media_db_path: str = ""
    trim_activity_db_path: str = ""
    photo_db_path: str = ""  # 相册 photo.db，空则不轮询相册事件
    scheduler_db_path: str = ""  # fn-scheduler scheduler.db，空则不轮询任务计划事件
    docker_socket_path: str = "/var/run/docker.sock"  # Docker Engine Unix socket；勾选 Docker 容器事件且文件可访问时监听
    logger_poll_interval: int = 5  # 秒，轮询间隔
    # 启动时是否沿用已保存游标（默认 False：与历史行为一致，只处理启动后的新记录）。
    # 开启后会补推「停机期间写入的日志」，适合容器短期重启不想漏事件的场景。
    resume_from_cursor_on_start: bool = False

    # 轮询汇总模式：开启后同一轮查询到的多条事件合并为一条通知；关闭则逐条推送（易触发渠道限流）
    poll_batch_summary_enabled: bool = False
    # 媒体库事件聚合推送：影视入库/相册等高频同类事件在静默窗口内合并为一条结构化消息（默认开启）
    media_batch_summary_enabled: bool = True
    # 聚合静默窗口（秒）：窗口内没有新事件才发送；一次批量刮削会合并为一条消息。0 表示按轮询轮次立即发送
    media_batch_window: int = 90
    
    # 勿扰模式（开启后在该时段内不推送，结束后汇总为一条消息）
    dnd_enabled: bool = False
    dnd_start_time: str = "22:00"  # HH:MM，如 22:00
    dnd_end_time: str = "07:00"   # HH:MM，如 07:00（跨日则到次日该时间结束）

    # NAS 定时巡检（Cron 调度；不参与日志事件勾选）
    nas_patrol_enabled: bool = False
    nas_patrol_cron: str = "0 12 * * *"  # 标准 5 段：每天中午 12:00
    # 旧字段：仅用于迁移；新配置以 nas_patrol_cron 为准
    nas_patrol_interval_minutes: int = 720

    # 高级配置
    max_log_age: int = 7  # 应用运行日志 monitor_*.log 保留天数
    # 推送失败多为渠道限流/额度用尽，重启无法恢复且会产生启停通知与重复推送，默认关闭
    notification_restart_enabled: bool = False
    notification_restart_consecutive_failures: int = 10
    notification_restart_window: int = 1800  # 30分钟
    notification_restart_cooldown: int = 21600  # 6小时
    

    
    def __post_init__(self):
        """初始化后处理"""
        # 记录哪些配置项是从环境变量设置的
        self._env_set_keys = set()
        # 首先从环境变量加载配置
        self._load_from_env()
        # 然后从配置文件加载，但仅当配置项未从环境变量设置时才覆盖
        self._load_from_file_skip_if_set()
        self._apply_discovered_db_paths()
        self.title_prefix = (self.title_prefix or "").strip()
        self._validate()
        self._ensure_directories()
    
    def _get_config_file_path(self) -> Path:
        """获取配置文件路径：Docker 下用 /app/config，本地用项目根目录 config。"""
        app_home = os.getenv("APP_HOME")
        if app_home:
            return Path(app_home) / "config" / "config.json"
        if Path("/app/config/config.json").exists():
            return Path("/app/config/config.json")
        # 从 src/config.py 向上到项目根
        candidate = Path(__file__).resolve().parent.parent / "config" / "config.json"
        if candidate.exists():
            return candidate
        return Path("/app/config/config.json")

    @staticmethod
    def _read_json_config(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
        """读取并解析配置文件，返回 (数据, 错误说明)。空错误串表示成功。

        **必须显式用 UTF-8**：项目自带的 config.json 含中文（`title_prefix: "飞牛NAS"`），
        在 Windows 等默认编码为 GBK 的环境下用 open(path, "r") 会抛 UnicodeDecodeError；
        旧实现把它包在 except 里只打印一行警告，结果是**整个文件配置被静默忽略**，极难排查。
        UTF-8 失败时回退平台默认编码，兼容历史上以 GBK 写出的文件。
        """
        last_decode_error: Optional[Exception] = None
        # 依次尝试：UTF-8（项目标准）→ 平台默认编码 → GBK。
        # 显式保留 GBK 兜底：系统开启「UTF-8 模式」时 None 也解析为 UTF-8，
        # 历史上以 GBK 写出的 config.json 会两次失败，导致整个文件配置被静默忽略。
        for encoding in ("utf-8", None, "gbk"):
            try:
                with open(path, "r", encoding=encoding) as f:
                    data = json.load(f)
            except UnicodeDecodeError as e:
                last_decode_error = e
                continue
            except json.JSONDecodeError as e:
                return None, f"JSON 格式错误：{e}"
            except OSError as e:
                return None, f"读取失败：{e}"
            if not isinstance(data, dict):
                return None, "配置文件顶层必须是 JSON 对象"
            return data, ""
        return None, f"文件编码无法识别（UTF-8/系统默认/GBK 均失败）：{last_decode_error}"

    def _load_from_file_skip_if_set(self):
        """从配置文件加载（可选），但跳过已从环境变量设置的配置项"""
        config_file = self._get_config_file_path()
        if not config_file.exists():
            return
        data, err = self._read_json_config(config_file)
        if err:
            _logger.error("配置文件读取失败，本次不加载任何文件配置项：%s -> %s", config_file, err)
            print(f"警告: 配置文件读取失败 - {err}")
            return
        assert data is not None

        # 已移除的渠道：残留配置不再生效，但要明确提示，避免用户以为还在推送
        for retired in _RETIRED_CHANNEL_KEYS:
            if data.get(retired):
                _logger.info(
                    "配置项 `%s` 对应的推送渠道已移除，该项被忽略（可安全删除）；"
                    "如需该渠道可改用「通用 Webhook」自行对接",
                    retired,
                )

        # 覆盖配置 - 仅当配置项未从环境变量设置时才使用配置文件的值
        for key, value in data.items():
            if hasattr(self, key):
                # 如果这个配置项已经从环境变量设置过，跳过
                if key in self._env_set_keys:
                    continue

                # 如果值是字符串且包含环境变量占位符，则进行替换
                if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                    env_var_name = value[2:-1]  # 提取变量名
                    env_value = os.getenv(env_var_name, '')  # 获取环境变量值，不存在则为空字符串
                    setattr(self, key, env_value)
                elif key in _BOOL_CONFIG_KEYS:
                    # 布尔项归一化：兼容 "1"/"true"/"yes"/"on" 与 "0"/"false"/"no"/"off"
                    setattr(self, key, as_bool(value, bool(getattr(self, key, False))))
                else:
                    setattr(self, key, value)

    def _apply_discovered_db_paths(self) -> None:
        """配置项为空时，按 compose 常见挂载路径自动探测可读的数据库文件。"""
        from config_db_paths import DB_PATH_CONFIG_KEYS, discover_db_path

        for key in DB_PATH_CONFIG_KEYS:
            if key in self._env_set_keys:
                continue
            if not str(getattr(self, key, "") or "").strip():
                found = discover_db_path(key)
                if found:
                    setattr(self, key, found)

    def reload_from_file(self, config_path: Path) -> bool:
        """从配置文件重新加载 Web UI 可修改的项（保存时热加载用）。返回是否成功。"""
        if not config_path.exists():
            return False
        data, err = self._read_json_config(config_path)
        if err or data is None:
            # 原先静默 return False，用户点了保存却看不到任何原因
            _logger.error("配置热加载失败，沿用当前配置：%s → %s", config_path, err or "内容为空")
            return False

        # 先备份全部 dataclass 字段，校验失败时回滚，避免热加载后处于未调用 _validate 的不一致状态
        _backup = {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}

        def env_skip(field: str) -> bool:
            return field in self._env_set_keys

        if not env_skip("monitor_events") and "monitor_events" in data and isinstance(data["monitor_events"], list):
            self.monitor_events = data["monitor_events"]
        if not env_skip("wechat_webhook_url") and "wechat_webhook_url" in data and isinstance(data["wechat_webhook_url"], str):
            self.wechat_webhook_url = data["wechat_webhook_url"]
        if not env_skip("dingtalk_webhook_url") and "dingtalk_webhook_url" in data and isinstance(data["dingtalk_webhook_url"], str):
            self.dingtalk_webhook_url = data["dingtalk_webhook_url"]
        if not env_skip("feishu_webhook_url") and "feishu_webhook_url" in data and isinstance(data["feishu_webhook_url"], str):
            self.feishu_webhook_url = data["feishu_webhook_url"]
        if not env_skip("minimal_push_enabled") and "minimal_push_enabled" in data:
            self.minimal_push_enabled = as_bool(data["minimal_push_enabled"], False)
        if not env_skip("push_markdown_enabled") and "push_markdown_enabled" in data:
            self.push_markdown_enabled = as_bool(data["push_markdown_enabled"], True)
        if not env_skip("push_emoji_enabled") and "push_emoji_enabled" in data:
            self.push_emoji_enabled = as_bool(data["push_emoji_enabled"], False)
        if not env_skip("ssh_ignore_loopback") and "ssh_ignore_loopback" in data:
            self.ssh_ignore_loopback = as_bool(data["ssh_ignore_loopback"], False)
        # 已移除的渠道（Bark/PushPlus/SMTP/MeoW 及更早的 magic_push/wecom_app）：
        # 残留配置不再生效，但要明确提示，避免用户以为还在推送
        _removed_channel_keys = _RETIRED_CHANNEL_KEYS + (
            "bark_url", "bark_icon", "pushplus_params", "smtp_params", "meow_params",
        )
        for retired in _removed_channel_keys:
            if data.get(retired):
                _logger.info(
                    "配置项 `%s` 对应的推送渠道已移除，该项被忽略（可安全删除）；"
                    "如需该渠道可改用「通用 Webhook」自行对接",
                    retired,
                )
        if not env_skip("webhook_params") and isinstance(data.get("webhook_params"), str):
            self.webhook_params = data["webhook_params"]
        if "title_prefix" in data and isinstance(data["title_prefix"], str):
            self.title_prefix = (data["title_prefix"] or "").strip()
        if not env_skip("log_retention_days") and "log_retention_days" in data and data["log_retention_days"] is not None:
            try:
                self.log_retention_days = int(data["log_retention_days"])
            except (TypeError, ValueError):
                pass
        if not env_skip("logger_poll_interval") and "logger_poll_interval" in data and data["logger_poll_interval"] is not None:
            try:
                self.logger_poll_interval = int(data["logger_poll_interval"])
            except (TypeError, ValueError):
                pass
        if not env_skip("resume_from_cursor_on_start") and "resume_from_cursor_on_start" in data:
            self.resume_from_cursor_on_start = as_bool(data["resume_from_cursor_on_start"], False)
        if not env_skip("logger_db_path") and "logger_db_path" in data and isinstance(data["logger_db_path"], str):
            self.logger_db_path = data["logger_db_path"].strip()
        if not env_skip("backup_db_path") and "backup_db_path" in data and isinstance(data["backup_db_path"], str):
            self.backup_db_path = data["backup_db_path"].strip()
        if "dnd_enabled" in data:
            self.dnd_enabled = as_bool(data["dnd_enabled"], False)
        if "dnd_start_time" in data and isinstance(data["dnd_start_time"], str):
            self.dnd_start_time = data["dnd_start_time"].strip()
        if "dnd_end_time" in data and isinstance(data["dnd_end_time"], str):
            self.dnd_end_time = data["dnd_end_time"].strip()
        if "nas_patrol_enabled" in data:
            self.nas_patrol_enabled = as_bool(data["nas_patrol_enabled"], False)
        if "nas_patrol_cron" in data and isinstance(data["nas_patrol_cron"], str):
            self.nas_patrol_cron = (data["nas_patrol_cron"] or "").strip()
        if "nas_patrol_interval_minutes" in data and data["nas_patrol_interval_minutes"] is not None:
            try:
                self.nas_patrol_interval_minutes = int(data["nas_patrol_interval_minutes"])
            except (TypeError, ValueError):
                pass
        if "poll_batch_summary_enabled" in data:
            self.poll_batch_summary_enabled = as_bool(data["poll_batch_summary_enabled"], False)
        if "media_batch_summary_enabled" in data:
            self.media_batch_summary_enabled = as_bool(data["media_batch_summary_enabled"], True)
        if "media_batch_window" in data and data["media_batch_window"] is not None:
            try:
                self.media_batch_window = int(data["media_batch_window"])
            except (TypeError, ValueError):
                pass
        if not env_skip("media_lib_logger_enabled") and "media_lib_logger_enabled" in data:
            self.media_lib_logger_enabled = as_bool(data["media_lib_logger_enabled"], False)
        if not env_skip("media_lib_service_patterns") and "media_lib_service_patterns" in data and isinstance(data["media_lib_service_patterns"], list):
            self.media_lib_service_patterns = [str(x).strip() for x in data["media_lib_service_patterns"] if str(x).strip()]
        if not env_skip("media_lib_app_name_patterns") and "media_lib_app_name_patterns" in data and isinstance(data["media_lib_app_name_patterns"], list):
            self.media_lib_app_name_patterns = [str(x).strip() for x in data["media_lib_app_name_patterns"] if str(x).strip()]
        if not env_skip("trim_media_db_path") and "trim_media_db_path" in data and isinstance(data["trim_media_db_path"], str):
            self.trim_media_db_path = data["trim_media_db_path"].strip()
        if not env_skip("trim_activity_db_path") and "trim_activity_db_path" in data and isinstance(data["trim_activity_db_path"], str):
            self.trim_activity_db_path = data["trim_activity_db_path"].strip()
        if not env_skip("photo_db_path") and "photo_db_path" in data and isinstance(data["photo_db_path"], str):
            self.photo_db_path = data["photo_db_path"].strip()
        if not env_skip("scheduler_db_path") and "scheduler_db_path" in data and isinstance(data["scheduler_db_path"], str):
            self.scheduler_db_path = data["scheduler_db_path"].strip()
        if not env_skip("docker_socket_path") and "docker_socket_path" in data and isinstance(data["docker_socket_path"], str):
            self.docker_socket_path = (data["docker_socket_path"] or "").strip() or "/var/run/docker.sock"
        self._apply_discovered_db_paths()
        self.title_prefix = (self.title_prefix or "").strip()
        try:
            self._validate()
        except ValueError as e:
            for f in fields(self):
                setattr(self, f.name, _backup[f.name])
            print(f"警告: 热加载配置未通过校验，已保持热加载前内存中的配置: {e}")
            return False
        return True

    def _load_from_env(self):
        """从环境变量加载配置"""
        # 端口配置（保留此行以兼容旧版本，但不实际使用）
        # port = os.getenv('PORT')  # 未使用，保留会造成误导

        # Webhook URLs
        if wechat_webhook := os.getenv('WECHAT_WEBHOOK_URL'):
            self.wechat_webhook_url = wechat_webhook
            self._env_set_keys.add('wechat_webhook_url')
        elif webhook := os.getenv('WEBHOOK_URL'):  # 兼容旧的环境变量名
            self.wechat_webhook_url = webhook
            self._env_set_keys.add('wechat_webhook_url')

        if dingtalk_webhook := os.getenv('DINGTALK_WEBHOOK_URL'):
            self.dingtalk_webhook_url = dingtalk_webhook
            self._env_set_keys.add('dingtalk_webhook_url')

        if feishu_webhook := os.getenv('FEISHU_WEBHOOK_URL'):
            self.feishu_webhook_url = feishu_webhook
            self._env_set_keys.add('feishu_webhook_url')

        if minimal_push_enabled := os.getenv('MINIMAL_PUSH_ENABLED'):
            self.minimal_push_enabled = minimal_push_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('minimal_push_enabled')
        if push_markdown_enabled := os.getenv('PUSH_MARKDOWN_ENABLED'):
            self.push_markdown_enabled = push_markdown_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('push_markdown_enabled')
        if push_emoji_enabled := os.getenv('PUSH_EMOJI_ENABLED'):
            self.push_emoji_enabled = push_emoji_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('push_emoji_enabled')
        if ssh_ignore_loopback := os.getenv('SSH_IGNORE_LOOPBACK'):
            self.ssh_ignore_loopback = ssh_ignore_loopback.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('ssh_ignore_loopback')
        if webhook_params := os.getenv('WEBHOOK_PARAMS'):
            self.webhook_params = webhook_params
            self._env_set_keys.add('webhook_params')
        # 已移除渠道的历史环境变量：不生效但提示一次，避免用户以为还在推送
        for retired_env in (
            "WECOM_APP_PARAMS", "MAGIC_PUSH_PARAMS",
            "BARK_URL", "BARK_ICON", "SMTP_PARAMS", "MEOW_PARAMS",
        ):
            if os.getenv(retired_env):
                _logger.info("环境变量 %s 对应的推送渠道已移除，该变量被忽略", retired_env)

        # 监控事件
        if events := os.getenv('MONITOR_EVENTS'):
            self.monitor_events = [e.strip() for e in events.split(',')]
            self._env_set_keys.add('monitor_events')

        # 日志级别
        if log_level := os.getenv('LOG_LEVEL'):
            self.log_level = log_level.upper()
            self._env_set_keys.add('log_level')


        # HTTP配置
        if pool_size := os.getenv('HTTP_POOL_SIZE'):
            try:
                self.http_pool_size = int(pool_size)
                self._env_set_keys.add('http_pool_size')
            except (TypeError, ValueError):
                print(f"警告: HTTP_POOL_SIZE 不是整数，已忽略: {pool_size}")

        if retry_count := os.getenv('HTTP_RETRY_COUNT'):
            try:
                self.http_retry_count = int(retry_count)
                self._env_set_keys.add('http_retry_count')
            except (TypeError, ValueError):
                print(f"警告: HTTP_RETRY_COUNT 不是整数，已忽略: {retry_count}")

        if timeout := os.getenv('HTTP_TIMEOUT'):
            try:
                self.http_timeout = int(timeout)
                self._env_set_keys.add('http_timeout')
            except (TypeError, ValueError):
                print(f"警告: HTTP_TIMEOUT 不是整数，已忽略: {timeout}")

        if dedup_window := os.getenv('DEDUP_WINDOW'):
            try:
                self.dedup_window = int(dedup_window)
                self._env_set_keys.add('dedup_window')
            except (TypeError, ValueError):
                print(f"警告: DEDUP_WINDOW 不是整数，已忽略: {dedup_window}")

        # 数据库日志监控
        if db_path := os.getenv('LOGGER_DB_PATH'):
            self.logger_db_path = db_path
            self._env_set_keys.add('logger_db_path')
        if backup_db_path := os.getenv('BACKUP_DB_PATH'):
            self.backup_db_path = backup_db_path
            self._env_set_keys.add('backup_db_path')
        if poll_interval := os.getenv('LOGGER_POLL_INTERVAL'):
            try:
                self.logger_poll_interval = int(poll_interval)
                self._env_set_keys.add('logger_poll_interval')
            except (TypeError, ValueError):
                print(f"警告: LOGGER_POLL_INTERVAL 不是整数，已忽略: {poll_interval}")

        if resume := os.getenv('RESUME_FROM_CURSOR_ON_START'):
            self.resume_from_cursor_on_start = resume.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('resume_from_cursor_on_start')

        if os.getenv('MEDIA_LIB_LOGGER_ENABLED', '').strip().lower() in ('1', 'true', 'yes', 'on'):
            self.media_lib_logger_enabled = True
            self._env_set_keys.add('media_lib_logger_enabled')
        if media_batch_enabled := os.getenv('MEDIA_BATCH_SUMMARY_ENABLED'):
            self.media_batch_summary_enabled = media_batch_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('media_batch_summary_enabled')
        if media_batch_window := os.getenv('MEDIA_BATCH_WINDOW'):
            try:
                self.media_batch_window = int(media_batch_window)
                self._env_set_keys.add('media_batch_window')
            except (TypeError, ValueError):
                print(f"警告: MEDIA_BATCH_WINDOW 不是整数，已忽略: {media_batch_window}")
        if trim_m := os.getenv('TRIM_MEDIA_DB_PATH'):
            self.trim_media_db_path = trim_m.strip()
            self._env_set_keys.add('trim_media_db_path')
        if trim_a := os.getenv('TRIM_ACTIVITY_DB_PATH'):
            self.trim_activity_db_path = trim_a.strip()
            self._env_set_keys.add('trim_activity_db_path')
        if photo_p := os.getenv('PHOTO_DB_PATH'):
            self.photo_db_path = photo_p.strip()
            self._env_set_keys.add('photo_db_path')
        if scheduler_p := os.getenv('SCHEDULER_DB_PATH'):
            self.scheduler_db_path = scheduler_p.strip()
            self._env_set_keys.add('scheduler_db_path')

        if docker_sock := os.getenv('DOCKER_SOCKET_PATH'):
            self.docker_socket_path = docker_sock.strip() or '/var/run/docker.sock'
            self._env_set_keys.add('docker_socket_path')

        # 高级配置
        if max_age := os.getenv('MAX_LOG_AGE'):
            try:
                self.max_log_age = int(max_age)
                self._env_set_keys.add('max_log_age')
            except (TypeError, ValueError):
                print(f"警告: MAX_LOG_AGE 不是整数，已忽略: {max_age}")

        if log_retention := os.getenv('LOG_RETENTION_DAYS'):
            try:
                self.log_retention_days = int(log_retention)
                self._env_set_keys.add('log_retention_days')
            except (TypeError, ValueError):
                print(f"警告: LOG_RETENTION_DAYS 不是整数，已忽略: {log_retention}")

        if notify_restart_enabled := os.getenv('NOTIFY_RESTART_ENABLED'):
            self.notification_restart_enabled = notify_restart_enabled.lower() in ['1', 'true', 'yes', 'on']
            self._env_set_keys.add('notification_restart_enabled')

        if notify_restart_failures := os.getenv('NOTIFY_RESTART_CONSECUTIVE'):
            try:
                self.notification_restart_consecutive_failures = int(notify_restart_failures)
                self._env_set_keys.add('notification_restart_consecutive_failures')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_CONSECUTIVE 不是整数，已忽略: {notify_restart_failures}")

        if notify_restart_window := os.getenv('NOTIFY_RESTART_WINDOW'):
            try:
                self.notification_restart_window = int(notify_restart_window)
                self._env_set_keys.add('notification_restart_window')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_WINDOW 不是整数，已忽略: {notify_restart_window}")

        if notify_restart_cooldown := os.getenv('NOTIFY_RESTART_COOLDOWN'):
            try:
                self.notification_restart_cooldown = int(notify_restart_cooldown)
                self._env_set_keys.add('notification_restart_cooldown')
            except (TypeError, ValueError):
                print(f"警告: NOTIFY_RESTART_COOLDOWN 不是整数，已忽略: {notify_restart_cooldown}")

    
    def _validate(self):
        """验证配置（部署时不强制 Webhook，可在 UI 中配置）"""
        if self.wechat_webhook_url and not self.wechat_webhook_url.startswith('http'):
            raise ValueError("WECHAT_WEBHOOK_URL 必须是有效的URL")

        if self.dingtalk_webhook_url and not self.dingtalk_webhook_url.startswith('http'):
            raise ValueError("DINGTALK_WEBHOOK_URL 必须是有效的URL")

        if self.feishu_webhook_url and not self.feishu_webhook_url.startswith('http'):
            raise ValueError("FEISHU_WEBHOOK_URL 必须是有效的URL")

        # 已移除渠道（bark/pushplus/smtp/meow）的残留配置不再校验、不再生效

        for ch_type, key in (("webhook", "webhook_params"),):
            for part in (p.strip() for p in (getattr(self, key) or "").split("|") if p.strip()):
                err = validate_channel_json(ch_type, part)
                if err:
                    raise ValueError(err)

        if not self.monitor_events:
            raise ValueError("必须配置至少一个监控事件")
        
        # 过滤未知事件类型（白名单见 valid_event_ids.MONITOR_EVENT_IDS；含 VM 的动态 eventId）
        self.monitor_events = filter_monitor_events(self.monitor_events)
        if not self.monitor_events:
            raise ValueError("必须配置至少一个监控事件")

        try:
            _iv = int(self.nas_patrol_interval_minutes)
        except (TypeError, ValueError):
            _iv = 720
        self.nas_patrol_interval_minutes = max(5, min(10080, _iv))

        try:
            _mbw = int(self.media_batch_window)
        except (TypeError, ValueError):
            _mbw = 90
        # 0 允许（表示按轮询轮次立即发送）；上限 1 小时，防止事件长期滞留
        self.media_batch_window = max(0, min(3600, _mbw))

        from utils.cron_util import resolve_nas_patrol_cron

        self.nas_patrol_cron = resolve_nas_patrol_cron(
            getattr(self, "nas_patrol_cron", "") or "",
            self.nas_patrol_interval_minutes,
        )

    def _ensure_directories(self):
        """确保目录存在"""
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.cursor_dir).mkdir(parents=True, exist_ok=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'monitor_events': self.monitor_events,
            'log_level': self.log_level,
            'http_pool_size': self.http_pool_size,
            'dedup_window': self.dedup_window,
            'wechat_webhook_url': self.wechat_webhook_url[:50] + '...' 
                if len(self.wechat_webhook_url) > 50 else self.wechat_webhook_url
        }
