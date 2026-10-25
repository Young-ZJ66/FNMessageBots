"""
备份数据库轮询器
从 basic_backup.db3 的 operations 表轮询新记录，并按成功/失败分发事件。
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import JournalEntry
from .schema_probe import probe as probe_schema
from .sqlite_uri import connect_readonly_with_fallback
from utils.dedup_store import DedupStore
from utils.logtime_display import format_ts_for_display, now_for_display


BACKUP_SUCCESS_EVENT = "BACKUP_TASK_SUCCESS"
BACKUP_FAILED_EVENT = "BACKUP_TASK_FAILED"
BACKUP_PARTIAL_EVENT = "BACKUP_TASK_PARTIAL_SUCCESS"
BACKUP_POLL_EVENTS = frozenset({BACKUP_SUCCESS_EVENT, BACKUP_FAILED_EVENT, BACKUP_PARTIAL_EVENT})
BACKUP_LOOKBACK_SECONDS = 600
DEDUP_TTL_SECONDS = 3 * 24 * 3600


def _ts_to_str(ts: Optional[int]) -> str:
    """展示用时间串；统一走 DISPLAY_TIMEZONE（原实现用容器本地时区，与其它轮询器不一致）。"""
    try:
        v = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        return now_for_display()
    if v > 10_000_000_000:
        v = int(v / 1000)
    return format_ts_for_display(v)


def _normalize_ts_to_seconds(ts: Optional[int]) -> int:
    try:
        v = int(ts or 0)
    except (TypeError, ValueError):
        return 0
    if v > 10_000_000_000:
        v = int(v / 1000)
    return max(0, v)


class BackupDBPoller:
    """从 basic_backup.db3 轮询备份执行记录。"""

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
    ):
        self.db_path = db_path
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.event_handlers: Dict[str, Callable] = {}
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._cursor_file = self.cursor_dir / "backup_db_poller_cursor.txt"
        self._dedup_file = self.cursor_dir / "backup_db_poller_dedup.json"
        self.logger = logging.getLogger(__name__)
        self._dedup_store = DedupStore(self._dedup_file, DEDUP_TTL_SECONDS, "备份", self.logger)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self.poll_batch_summary_enabled = False
        self.summary_batch_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None

    @property
    def _dedup_seen(self) -> Dict[str, int]:
        """去重缓存以 DedupStore 为唯一事实来源，这里仅做兼容访问。"""
        return self._dedup_store.seen

    @_dedup_seen.setter
    def _dedup_seen(self, value: Dict[str, int]) -> None:
        self._dedup_store.seen = value

    def add_handler(self, event_type: str, handler: Callable):
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        db_path: Optional[str] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if db_path is not None:
            self.db_path = db_path

    def set_poll_batch_summary(
        self,
        enabled: bool,
        enqueue: Optional[Callable[[List[Dict[str, Any]]], None]],
    ) -> None:
        self.poll_batch_summary_enabled = bool(enabled)
        self.summary_batch_enqueue = enqueue

    def _read_cursor(self) -> Dict[str, int]:
        default = {"last_finished_time": 0, "last_id": 0}
        try:
            if self._cursor_file.exists():
                raw = self._cursor_file.read_text(encoding="utf-8").strip()
                if not raw:
                    return default
                # 兼容旧版纯数字游标：仅保存了 last_id
                if raw.isdigit():
                    return {"last_finished_time": 0, "last_id": int(raw)}
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    return {
                        "last_finished_time": int(obj.get("last_finished_time") or 0),
                        "last_id": int(obj.get("last_id") or 0),
                    }
        except Exception as e:
            self.logger.warning("读取备份轮询游标失败: %s", e)
        return default

    def _write_cursor(self, last_finished_time: int, last_id: int) -> None:
        try:
            payload = {
                "last_finished_time": int(last_finished_time or 0),
                "last_id": int(last_id or 0),
            }
            self._cursor_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.logger.warning("写入备份轮询游标失败: %s", e)

    def _load_dedup(self) -> None:
        self._dedup_store.load()

    def _save_dedup(self) -> None:
        self._dedup_store.save()

    def _prune_dedup(self) -> None:
        self._dedup_store.prune()

    def _connect(self) -> sqlite3.Connection:
        # 仅做轮询读取，使用只读连接避免要求数据库目录可写（WAL/journal 权限）
        conn = connect_readonly_with_fallback(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_latest_watermark(self) -> Optional[Dict[str, int]]:
        """查询最新水位；失败返回 None（调用方等待重试对齐，不把失败当空库）。"""
        conn = None
        try:
            conn = self._connect()
            # schema 自检只在首次调用时实际执行（内部去重）
            probe_schema(conn, "backup", self.logger, self.db_path)
            row = conn.execute(
                """
                SELECT
                  id,
                  COALESCE(finished_time, start_time, 0) AS event_time
                FROM operations
                ORDER BY event_time DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return {"last_finished_time": 0, "last_id": 0}
            return {
                "last_finished_time": _normalize_ts_to_seconds(row["event_time"]),
                "last_id": int(row["id"] or 0),
            }
        except Exception as e:
            self.logger.warning("获取备份数据库最新水位失败: %s", e)
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _fetch_rows_with_lookback(self, from_event_time: int) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          o.id,
          o.uid,
          o.task_id,
          o.status,
          o.error_code,
          o.error_message,
          o.start_time,
          o.finished_time,
          o.files_count,
          o.total_size,
          o.completed_count,
          o.completed_size,
          o.actual_count,
          o.actual_size,
          o.actual_time,
          o.comment,
          ut.name AS task_name,
          ut.target_id,
          ut.target_path,
          ut.source_paths,
          s.name AS storage_name,
          s.address AS storage_address
        FROM operations o
        LEFT JOIN user_tasks ut ON ut.id = o.task_id AND ut.uid = o.uid
        LEFT JOIN storages s ON s.id = ut.target_id AND s.uid = ut.uid
        WHERE COALESCE(o.finished_time, o.start_time, 0) >= ?
        ORDER BY COALESCE(o.finished_time, o.start_time, 0) ASC, o.id ASC
        """
        try:
            conn = self._connect()
            rows = [dict(r) for r in conn.execute(sql, (int(from_event_time),)).fetchall()]
            conn.close()
            return rows
        except Exception as e:
            self.logger.error("查询备份数据库失败: %s", e)
            return []

    def _to_event(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status = int(row.get("status") or 0)
        error_code = int(row.get("error_code") or 0)
        if status == 3 and error_code == 0:
            event_type = BACKUP_SUCCESS_EVENT
        elif status == 4:
            actual_count = int(row.get("actual_count") or 0)
            if actual_count > 0:
                event_type = BACKUP_PARTIAL_EVENT
            else:
                event_type = BACKUP_FAILED_EVENT
        else:
            return None
        event_data = {
            "operation_id": row.get("id"),
            "uid": row.get("uid"),
            "task_id": row.get("task_id"),
            "task_name": row.get("task_name") or f"任务-{row.get('task_id')}",
            "status": status,
            "error_code": error_code,
            "error_message": (row.get("error_message") or "").strip(),
            "start_time": row.get("start_time"),
            "finished_time": row.get("finished_time"),
            "files_count": row.get("files_count"),
            "total_size": row.get("total_size"),
            "completed_count": row.get("completed_count"),
            "completed_size": row.get("completed_size"),
            "actual_count": row.get("actual_count"),
            "actual_size": row.get("actual_size"),
            "actual_time": row.get("actual_time"),
            "comment": row.get("comment") or "",
            "storage_name": row.get("storage_name") or "",
            "storage_address": row.get("storage_address") or "",
            "target_path": row.get("target_path") or "",
            "source_paths": row.get("source_paths") or "",
            "_source": "backup_db",
            "_source_cursor": str(row.get("id") or ""),
            "_source_event_id": event_type,
        }
        timestamp = _ts_to_str(row.get("finished_time") or row.get("start_time"))
        entry = JournalEntry(
            cursor=str(row.get("id") or ""),
            timestamp=timestamp,
            hostname="basic_backup.db3",
            syslog_identifier=event_type,
            message=json.dumps(event_data, ensure_ascii=False),
            priority=0,
            pid=int(row.get("uid") or 0),
            raw_data=json.dumps(row, ensure_ascii=False),
            original_line=json.dumps(row, ensure_ascii=False),
        )
        return {"event_type": event_type, "event_data": event_data, "entry": entry}

    def _row_watermark(self, row: Dict[str, Any]) -> tuple:
        event_time = _normalize_ts_to_seconds(row.get("finished_time") or row.get("start_time"))
        return (event_time, int(row.get("id") or 0))

    def _fingerprint(self, row: Dict[str, Any]) -> str:
        return "|".join(
            [
                str(row.get("id") or ""),
                str(row.get("uid") or ""),
                str(row.get("task_id") or ""),
                str(_normalize_ts_to_seconds(row.get("finished_time") or row.get("start_time"))),
                str(row.get("status") or ""),
                str(row.get("error_code") or ""),
            ]
        )

    def _poll_once(self, cursor: Dict[str, int]) -> Dict[str, int]:
        last_finished_time = int(cursor.get("last_finished_time") or 0)
        last_id = int(cursor.get("last_id") or 0)
        lower_bound = max(0, last_finished_time - BACKUP_LOOKBACK_SECONDS)
        rows = self._fetch_rows_with_lookback(lower_bound)
        max_key = (last_finished_time, last_id)

        for row in rows:
            key = self._row_watermark(row)
            if key > max_key:
                max_key = key

            # 只处理“严格晚于已确认水位”的记录；回看窗口内旧记录仅用于防漏与去重。
            if key <= (last_finished_time, last_id):
                continue
            fp = self._fingerprint(row)
            if fp in self._dedup_seen:
                continue

            ev = self._to_event(row)
            if not ev:
                continue
            et = ev["event_type"]
            if self.monitor_events and et not in self.monitor_events:
                continue
            handler = self.event_handlers.get(et)
            if not handler:
                continue
            try:
                rid = int(row.get("id") or 0) or (abs(hash(fp)) % (2**31 - 1))
                ed = ev["event_data"]
                entry = ev["entry"]
                if self.poll_batch_summary_enabled and self.summary_batch_enqueue:
                    self.summary_batch_enqueue(
                        [
                            {
                                "row_id": rid,
                                "db_event_id": et,
                                "event_type": et,
                                "event_data": ed,
                                "entry": entry,
                                "handler": handler,
                                "source": "backup_db",
                            }
                        ]
                    )
                else:
                    handler(ed, entry)
                self._dedup_seen[fp] = int(time.time())
            except Exception as e:
                self.logger.error("处理备份事件失败 %s: %s", et, e, exc_info=True)
        self._prune_dedup()
        self._save_dedup()
        next_cursor = {"last_finished_time": max_key[0], "last_id": max_key[1]}
        self._write_cursor(next_cursor["last_finished_time"], next_cursor["last_id"])
        return next_cursor

    def _run_loop(self):
        self._load_dedup()
        cursor = self._read_cursor()
        if int(cursor.get("last_finished_time") or 0) <= 0 and int(cursor.get("last_id") or 0) <= 0:
            latest = self._get_latest_watermark()
            while latest is None and self.running:
                # 查询失败 ≠ 空库：等待重试对齐，避免恢复后把存量记录当新事件
                self.logger.warning("备份数据库暂不可读，%s 秒后重试对齐（不把失败当空库）", self.poll_interval)
                for _ in range(self.poll_interval):
                    if not self.running:
                        return
                    time.sleep(1)
                latest = self._get_latest_watermark()
            if latest is None:
                return
            cursor = latest
            self._write_cursor(cursor["last_finished_time"], cursor["last_id"])
            self.logger.info(
                "备份轮询启动，仅处理水位之后的新记录: finished_time=%s id=%s",
                cursor["last_finished_time"],
                cursor["last_id"],
            )
        while self.running:
            try:
                cursor = self._poll_once(cursor)
            except Exception as e:
                self.logger.error("备份轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def _align_cursor_to_latest(self) -> None:
        """每次启用时对齐到当前最新水位，避免补发停用期间的存量记录。"""
        latest = self._get_latest_watermark()
        if latest is None:
            self.logger.warning("备份数据库暂不可读，本次未对齐（保留原游标，下轮重试）")
            return
        self._write_cursor(latest["last_finished_time"], latest["last_id"])
        self.logger.info(
            "备份轮询启用时已对齐当前水位（不补历史）: finished_time=%s id=%s",
            latest["last_finished_time"],
            latest["last_id"],
        )

    def start(self):
        if self.running:
            return
        if not (BACKUP_POLL_EVENTS & set(self.monitor_events or [])):
            self.logger.info("monitor_events 未包含备份任务事件，跳过 BackupDBPoller")
            return
        self._align_cursor_to_latest()
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="BackupDBPoller", daemon=False)
        self._thread.start()
        self.logger.info("BackupDBPoller 已启动，db=%s", self.db_path)

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("BackupDBPoller 已停止")
