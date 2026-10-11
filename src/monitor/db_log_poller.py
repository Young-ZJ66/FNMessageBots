"""
数据库日志轮询器
从 eventlogger 的 SQLite 数据库 log 表轮询新记录（路径由配置 logger_db_path / LOGGER_DB_PATH 指定）。
表结构: id, serviceId, uid, uname, logtime(10位时间戳), loglevel, eventId, parameter(JSON), category
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any, Sequence

from utils.logtime_display import format_ts_for_display, get_logtime_display_offset_seconds, now_for_display

from .models import JournalEntry
from .schema_probe import probe as probe_schema
from .sqlite_uri import connect_readonly_with_fallback
from .ssh_journal_poller import should_ignore_loopback_ssh


# 数据库 eventId -> 项目内 event_type（通知/处理器使用的类型）
# 数据库与项目一致的直接同名字符串；不一致的映射到项目已有类型
DB_EVENT_ID_TO_PROJECT: Dict[str, str] = {
    "LoginSucc": "LoginSucc",
    "LoginSucc2FA1": "LoginSucc2FA1",
    "LoginFail": "LoginFail",
    "Logout": "Logout",
    "FoundDisk": "FoundDisk",
    "InsertDisk": "InsertDisk",
    "EjectDisk": "EjectDisk",
    "StorageBroken": "StorageBroken",
    "StorageDegraded1": "STORAGE_DEGRADED",
    "APP_CRASH": "APP_CRASH",
    "SshdLoginSucc": "SSH_LOGIN_SUCCESS",
    "SshdLoginAuthFail": "SSH_AUTH_FAILED",
    "SshdLogonout": "SSH_DISCONNECTED",
    "APP_INSTALL_FAILED_INIT_DOCKER_EXCEPTION": "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "UPS_ONLINE": "UPS_ONLINE",
    "UPS_ONBATT_LOWBATT": "UPS_ONBATT_LOWBATT",
    "UPS_DISCONNET": "UPS_ONBATT",
    "UPS_CONNET_OL": "UPS_ONLINE",
    "UPS_ENABLE": "UPS_ENABLE",
    "UPS_DISABLE": "UPS_DISABLE",
    "APP_UPDATE_FAILED": "APP_UPDATE_FAILED",
    "APP_STARTED": "APP_STARTED",
    "APP_STOPPED": "APP_STOPPED",
    "APP_UPDATED": "APP_UPDATED",
    "APP_INSTALLED": "APP_INSTALLED",
    "APP_AUTO_STARTED": "APP_AUTO_STARTED",
    "APP_UNINSTALLED": "APP_UNINSTALLED",
    "DISK_IO_ERR": "DISK_IO_ERR",
    # 部分 NAS 日志使用 DiskError + parameter.template，与 DISK_IO_ERR 同源
    "DiskError": "DISK_IO_ERR",
    "DiskWakeup": "DiskWakeup",
    "DiskSpindown": "DiskSpindown",
    "CPU_USAGE_ALARM": "CPU_USAGE_ALARM",
    "CPU_USAGE_RESTORED": "CPU_USAGE_RESTORED",
    "MEMORY_USAGE_ALARM": "MEMORY_USAGE_ALARM",
    "MEMORY_USAGE_RESTORED": "MEMORY_USAGE_RESTORED",
    # 可选事件（默认不推送，需用户在配置中勾选）
    "ARCHIVING_SUCCESS": "ARCHIVING_SUCCESS",
    "DeleteFile": "DeleteFile",
    "MovetoTrashbin": "MovetoTrashbin",
    "SHARE_EVENTID_DEL": "SHARE_EVENTID_DEL",
    "SHARE_EVENTID_PUT": "SHARE_EVENTID_PUT",
    "WEBDAV_ENABLED": "WEBDAV_ENABLED",
    "WEBDAV_DISABLED": "WEBDAV_DISABLED",
    "SAMBA_ENABLED": "SAMBA_ENABLED",
    "SAMBA_DISABLED": "SAMBA_DISABLED",
    "DLNA_ENABLED": "DLNA_ENABLED",
    "DLNA_DISABLED": "DLNA_DISABLED",
    "FTP_ENABLED": "FTP_ENABLED",
    "FTP_DISABLED": "FTP_DISABLED",
    "NFS_ENABLED": "NFS_ENABLED",
    "NFS_DISABLED": "NFS_DISABLED",
    "FW_ENABLE": "FW_ENABLE",
    "FW_DISABLE": "FW_DISABLE",
    "SECURITY_PORTCHANGED": "SECURITY_PORTCHANGED",
    "SHUTDOWN_VM": "SHUTDOWN_VM",
    "STATUS_RUNNING_VM": "STATUS_RUNNING_VM",
    "DESTROY_VM": "DESTROY_VM",
    # 影视库 / 用户（eventId 以 NAS 实际为准，可再扩展）
    "USER_CREATE": "MEDIA_USER_CREATED",
    "CreateUser": "MEDIA_USER_CREATED",
    "AddUserSucc": "MEDIA_USER_CREATED",
    "USER_ADD_SUCCESS": "MEDIA_USER_CREATED",
    "MEDIA_USER_CREATE": "MEDIA_USER_CREATED",
}

def _logtime_to_datetime(logtime: int) -> str:
    """10 位 Unix 时间戳转 YYYY-MM-DD HH:MM:SS（DISPLAY_TIMEZONE，默认 Asia/Shanghai）。
    可设 LOGTIME_DISPLAY_OFFSET_SECONDS 修正存库偏差（如 28800=+8h）。"""
    try:
        ts = int(logtime) + get_logtime_display_offset_seconds()
    except (ValueError, TypeError, OSError):
        return now_for_display()
    return format_ts_for_display(ts)


def _parse_parameter(parameter: Optional[str], uname: Optional[str], uid: Optional[int]) -> Dict[str, Any]:
    """解析 parameter JSON，并合并 uname/uid。
    常见 eventId 的 parameter 结构示例：
    - APP_CRASH: {"data":{...}, "datetime", "eventId", "from", "level"}
    - SshdLogonout / SshdLoginAuthFail / SshdLoginSucc: {"user":"xxx", "from":"192.168.1.155"}，from 为 IP
    """
    data: Dict[str, Any] = {}
    if parameter and parameter.strip():
        try:
            data = json.loads(parameter)
        except json.JSONDecodeError:
            data = {"raw": parameter}
    if uname is not None and "user" not in data:
        data["user"] = uname
    if uid is not None and "uid" not in data:
        data["uid"] = uid
    if "IP" not in data and "ip" not in data:
        # 飞牛 SSH 事件等使用 "from" 表示来源 IP，统一映射到 IP 供通知展示
        data["IP"] = data.get("from") or data.get("FROM") or ""
    return data


def _as_int(value: Any, default: int = 0) -> int:
    """宽松整数转换：非数字、None、空串等一律回落到 default，绝不抛异常。

    库内 loglevel / uid 曾出现非数字（或文本）导致 int() 抛错，进而使整轮轮询
    中断、游标不推进，该库之后所有日志永久不再推送。此处避免该路径。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _row_to_entry(row: Dict[str, Any]) -> JournalEntry:
    """将数据库一行转为 JournalEntry（供现有 event_processor 使用）。"""
    logtime = row.get("logtime") or 0
    ts = _logtime_to_datetime(logtime)
    parameter = row.get("parameter") or "{}"
    return JournalEntry(
        cursor=str(row.get("id", "")),
        timestamp=ts,
        hostname=str(row.get("serviceId") or "db"),
        syslog_identifier=str(row.get("eventId") or "unknown"),
        message=parameter,
        priority=_as_int(row.get("loglevel")),
        pid=_as_int(row.get("uid")),
        raw_data=parameter,
        original_line=parameter,
    )


class DBLogPoller:
    """从 logger_data.db3 的 log 表轮询新记录并分发到已注册的事件处理器。"""

    BATCH_MAX_ATTEMPTS = 3
    # 单轮最多读取的行数，避免游标异常落后时整表读入内存
    FETCH_LIMIT = 2000
    # 启动时读取 MAX(id) 的尝试次数，全部失败则拒绝启动（宁可不启动，也不重推整库）
    MAX_ID_ATTEMPTS = 3

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
        media_lib_logger_enabled: bool = False,
        media_lib_service_patterns: Optional[Sequence[str]] = None,
        max_id_attempts: Optional[int] = None,
        resume_from_cursor_on_start: bool = False,
    ):
        self.db_path = db_path
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, poll_interval)
        self.monitor_events = set(monitor_events or [])
        self.media_lib_logger_enabled = bool(media_lib_logger_enabled)
        self.media_lib_service_patterns = [str(p).strip() for p in (media_lib_service_patterns or []) if str(p).strip()]
        self.event_handlers: Dict[str, Callable] = {}
        self.batch_handler: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        # 高频同类事件的聚合队列挂钩（DeleteFile/共享变更等，见 main.AGGREGATED_EVENT_TYPES）
        self.deferred_events: frozenset = frozenset()
        self.deferred_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        # 当 SSH journal 轮询可用时，跳过库内 Sshd*，避免双推
        self.skip_ssh_events = False
        # 与 SshJournalPoller 一致：忽略本机回环的 SSH 登录成功 / 断开
        self.ignore_ssh_loopback = False
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._cursor_file = self.cursor_dir / "db_poller_cursor.txt"
        self._batch_fail_count = 0
        # 启动时 MAX(id) 读取失败的重试次数（默认 MAX_ID_ATTEMPTS）
        _attempts = _as_int(max_id_attempts, 0)
        self.max_id_attempts = _attempts if _attempts > 0 else self.MAX_ID_ATTEMPTS
        # 是否在启动时沿用已保存游标（默认否，保持「只推送启动后新记录」的历史行为）
        self.resume_from_cursor_on_start = bool(resume_from_cursor_on_start)
        # schema 自检只做一次
        self._schema_probed = False
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)

    def add_handler(self, event_type: str, handler: Callable):
        """注册事件类型对应的处理函数（event_type 为项目内类型，如 LoginSucc、SSH_LOGIN_SUCCESS）。"""
        self.event_handlers[event_type] = handler
        self.logger.debug("注册事件处理器: %s", event_type)

    def clear_handlers(self) -> None:
        """清空已注册的事件处理器（热加载配置前调用）。"""
        self.event_handlers.clear()

    def set_batch_handler(self, handler: Optional[Callable[[List[Dict[str, Any]]], None]]) -> None:
        """注册按轮询批量处理函数（同一轮仅调用一次）。"""
        self.batch_handler = handler

    def set_deferred_events(
        self,
        event_types,
        enqueue: Optional[Callable[[List[Dict[str, Any]]], None]],
    ) -> None:
        """按事件类型把主日志事件转入聚合队列（其余事件仍逐条分发）。

        用于 DeleteFile/共享变更等高频同质事件的静默窗口聚合；
        仅在聚合刷盘线程运行（enqueue 非 None）时生效。
        """
        self.deferred_events = frozenset(event_types or ())
        self.deferred_enqueue = enqueue

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        db_path: Optional[str] = None,
        media_lib_logger_enabled: Optional[bool] = None,
        media_lib_service_patterns: Optional[Sequence[str]] = None,
        resume_from_cursor_on_start: Optional[bool] = None,
    ) -> None:
        """热加载时更新监控事件、轮询间隔、数据库路径。"""
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, poll_interval)
        if db_path is not None:
            self.db_path = db_path
        if media_lib_logger_enabled is not None:
            self.media_lib_logger_enabled = bool(media_lib_logger_enabled)
        if media_lib_service_patterns is not None:
            self.media_lib_service_patterns = [str(p).strip() for p in media_lib_service_patterns if str(p).strip()]
        if resume_from_cursor_on_start is not None:
            self.resume_from_cursor_on_start = bool(resume_from_cursor_on_start)
        self.logger.info("DBLogPoller 配置已更新: events=%s, interval=%s, db=%s", len(self.monitor_events), self.poll_interval, self.db_path)

    def _apply_media_library_filter(self, row: Dict[str, Any], project_type: str) -> str:
        """将全局登录/登出映射为影视库专用事件（需 serviceId/parameter 匹配模式）。"""
        if not self.media_lib_logger_enabled or not self.media_lib_service_patterns:
            return project_type
        hay = f"{row.get('serviceId') or ''}\t{row.get('category') or ''}\t{row.get('parameter') or ''}"
        hl = hay.lower()
        if not any(p.lower() in hl for p in self.media_lib_service_patterns):
            return project_type
        if project_type == "LoginSucc":
            return "MEDIA_LOGIN_SUCC"
        if project_type == "Logout":
            return "MEDIA_LOGOUT"
        return project_type

    def _read_last_id(self) -> int:
        """读取上次已处理到的 id（0 表示没有可用游标）。"""
        try:
            if self._cursor_file.exists():
                raw = self._cursor_file.read_text(encoding="utf-8").strip()
                if raw.isdigit():
                    return int(raw)
        except Exception as e:
            self.logger.warning("读取游标失败: %s", e)
        return 0

    def _write_last_id(self, last_id: int) -> None:
        try:
            self._cursor_file.write_text(str(last_id), encoding="utf-8")
        except Exception as e:
            self.logger.warning("写入游标失败: %s", e)

    def _get_max_log_id(self) -> int:
        """获取 log 表当前最大 id；启动时用此值作为游标，只处理此后新写入的记录。

        读取失败时不再静默返回 0（那会让游标从 0 开始、重推整库），改为重试若干次后
        抛错并拒绝启动轮询，避免把「查询失败」当成「库是空的」。
        """
        last_err: Optional[Exception] = None
        attempts = max(1, int(self.max_id_attempts or self.MAX_ID_ATTEMPTS))
        attempt = 0
        while attempt < attempts:
            attempt += 1
            try:
                conn = connect_readonly_with_fallback(self.db_path, timeout=5.0)
                try:
                    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM log").fetchone()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                return _as_int(row[0] if row else 0)
            except Exception as exc:
                last_err = exc
                self.logger.warning(
                    "获取 log 表最大 id 失败（第 %s/%s 次）: %s", attempt, attempts, exc
                )
                if attempt < attempts:
                    time.sleep(min(2.0 * attempt, 5.0))
        raise RuntimeError(f"无法读取 log 表最大 id，已放弃启动轮询以避免重推历史: {last_err}")

    def _fetch_new_rows(self, after_id: int) -> List[Dict[str, Any]]:
        """查询 id > after_id 的记录，按 id 升序，每轮最多 FETCH_LIMIT 条。

        加 LIMIT 是因为游标一旦异常落后（例如库被替换、MAX(id) 读取失败回退），
        原实现会把整表一次性读入内存；分批后下一轮会继续处理剩余记录。
        """
        try:
            conn = connect_readonly_with_fallback(self.db_path, timeout=5.0)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT id, serviceId, uid, uname, logtime, loglevel, eventId, parameter, category "
                    "FROM log WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after_id, self.FETCH_LIMIT),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            self.logger.error("查询数据库失败: %s", e)
            return []

    def _build_batch_item(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把一行转为待投递的批次项；该行不需要推送时返回 None。"""
        row_id = row.get("id", 0)
        db_event_id = (row.get("eventId") or "").strip()
        if not db_event_id:
            return None
        project_type = DB_EVENT_ID_TO_PROJECT.get(db_event_id, db_event_id)
        # SshdLoginAuthFail 且 uname=invalid 才是无效用户尝试，按 SSH_INVALID_USER 处理
        uname_raw = (row.get("uname") or "").strip()
        if db_event_id == "SshdLoginAuthFail" and uname_raw.lower() == "invalid":
            project_type = "SSH_INVALID_USER"
        if self.skip_ssh_events and project_type in {
            "SSH_LOGIN_SUCCESS",
            "SSH_AUTH_FAILED",
            "SSH_INVALID_USER",
            "SSH_DISCONNECTED",
        }:
            return None
        project_type = self._apply_media_library_filter(row, project_type)
        if self.monitor_events and project_type not in self.monitor_events:
            return None
        handler = self.event_handlers.get(project_type)
        if not handler:
            return None
        event_data = _parse_parameter(
            row.get("parameter"),
            row.get("uname"),
            row.get("uid"),
        )
        if self.ignore_ssh_loopback and should_ignore_loopback_ssh(project_type, event_data):
            return None
        event_data.setdefault("_source", "logger_db")
        event_data.setdefault("_source_cursor", str(row_id))
        event_data.setdefault("_source_event_id", db_event_id)
        return {
            "row_id": row_id,
            "db_event_id": db_event_id,
            "event_type": project_type,
            "event_data": event_data,
            "entry": _row_to_entry(row),
            "handler": handler,
        }

    def _poll_once(self, last_id: int) -> int:
        rows = self._fetch_new_rows(last_id)
        batch_events: List[Dict[str, Any]] = []
        # 游标推进到本轮已见的最大 id，而不是「rows[-1]」：单行解析异常时也要越过该行，
        # 否则同一脏行每轮复现、其后的日志永远不再推送。
        max_seen_id = last_id
        skipped = 0
        for row in rows:
            row_id = _as_int(row.get("id"), max_seen_id)
            max_seen_id = max(max_seen_id, row_id)
            try:
                item = self._build_batch_item(row)
            except Exception as e:
                skipped += 1
                self.logger.error(
                    "解析/构造事件失败，已跳过该行 id=%s eventId=%s: %s",
                    row_id,
                    (row.get("eventId") or "").strip(),
                    e,
                    exc_info=True,
                )
                continue
            if item is not None:
                batch_events.append(item)
        if skipped:
            self.logger.warning("本轮有 %s 行日志处理失败已跳过（游标继续推进）", skipped)

        batch_delivery_failed = False
        if batch_events:
            if self.batch_handler:
                try:
                    self.batch_handler(batch_events)
                except Exception as e:
                    self.logger.error("批量处理事件失败（count=%s）: %s", len(batch_events), e, exc_info=True)
                    batch_delivery_failed = True
            else:
                # 兼容旧逻辑：未注册批处理时按条处理
                for item in batch_events:
                    try:
                        if self.deferred_enqueue and item["event_type"] in self.deferred_events:
                            # 高频同类事件转入聚合队列（静默窗口内合并推送）
                            self.deferred_enqueue([item])
                            continue
                        item["handler"](item["event_data"], item["entry"])
                    except Exception as e:
                        self.logger.error("处理事件失败 eventId=%s: %s", item["db_event_id"], e)
        if rows:
            # 汇总投递异常时同批最多重试 BATCH_MAX_ATTEMPTS 次，之后推进游标，避免无限重发
            if self.batch_handler and batch_events and batch_delivery_failed:
                self._batch_fail_count += 1
                if self._batch_fail_count < self.BATCH_MAX_ATTEMPTS:
                    return last_id
                self.logger.warning(
                    "批量处理连续失败 %s 次，放弃本批（id %s–%s）",
                    self._batch_fail_count,
                    rows[0].get("id"),
                    max_seen_id,
                )
            self._batch_fail_count = 0
            self._write_last_id(max_seen_id)
        return max_seen_id

    def _align_cursor_on_start(self) -> Optional[int]:
        """启动时确定游标起点。

        两种模式：
        - 默认（resume_from_cursor_on_start=False，与历史行为一致）：对齐到当前库最大 id，
          不补推历史，仅处理启用之后的新增记录；
        - resume_from_cursor_on_start=True：沿用上次退出时保存的游标，把停机期间产生的记录补推一遍。

        MAX(id) 读不到时**不会**退回 0（那会把整库历史重推一遍），而是记录错误并让上层
        隔一段时间重试；期间不轮询，宁可晚一点开始，也不制造大量重复通知。
        """
        if self.resume_from_cursor_on_start:
            saved = self._read_last_id()
            if saved > 0:
                try:
                    max_id = self._get_max_log_id()
                except Exception as e:
                    self.logger.error("启动读取库水位失败，暂不轮询（将自动重试）: %s", e)
                    return None
                if saved <= max_id:
                    self.logger.info(
                        "数据库轮询启动：沿用已保存游标 id=%s（补推停机期间记录，当前库最大 id=%s）",
                        saved,
                        max_id,
                    )
                    return saved
                self.logger.warning(
                    "已保存游标 id=%s 大于当前库最大 id=%s（库可能被替换/回滚），改为对齐当前水位",
                    saved,
                    max_id,
                )
                self._write_last_id(max_id)
                return max_id
            self.logger.info("未找到可用游标，按当前水位对齐（不补推历史）")

        try:
            max_id = self._get_max_log_id()
        except Exception as e:
            self.logger.error("启动对齐游标失败，暂不轮询（将自动重试）: %s", e)
            return None
        self._write_last_id(max_id)
        self.logger.info("数据库轮询启动，仅处理 id > %s 的新记录，间隔 %s 秒", max_id, self.poll_interval)
        return max_id

    def _run_loop(self) -> None:
        last_id: Optional[int] = None
        while self.running:
            if last_id is None:
                last_id = self._align_cursor_on_start()
                if last_id is None:
                    # 等待一个轮询周期后重试对齐，避免启动瞬间库被占用导致永久停摆
                    for _ in range(self.poll_interval):
                        if not self.running:
                            return
                        time.sleep(1)
                    continue
            try:
                self._probe_schema_once()
                last_id = self._poll_once(last_id)
            except Exception as e:
                self.logger.error("轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def _probe_schema_once(self) -> None:
        """首次成功连接后做一次 schema 自检（列名不符会让查询静默返回空数据）。"""
        if self._schema_probed:
            return
        self._schema_probed = True
        try:
            conn = connect_readonly_with_fallback(self.db_path, timeout=5.0)
            try:
                probe_schema(conn, "logger", self.logger, self.db_path)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            self.logger.info("schema 自检跳过（连接失败）: %s", e)

    def start(self) -> None:
        if self.running:
            return
        if not (self.db_path or "").strip():
            self.logger.info("未配置 logger_db_path，跳过 DBLogPoller")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="DBLogPoller", daemon=False)
        self._thread.start()
        self.logger.info("DBLogPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("DBLogPoller 已停止")
