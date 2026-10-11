"""
飞牛各服务 SQLite 库的 schema 自检。

背景：飞牛内部库（`logger_data.db3` / `trimmedia.db` / `basic_backup.db3` / `scheduler.db` …）
的表结构没有公开文档，跨版本也可能变化。代码里写的列名一旦与真机不符，通常的表现是
**静默返回空数据**（查不到行 → 不推送），而不是报错——排查成本很高。

因此这里在首次轮询成功连接后做一次自检：

- `INFO`：打出每张表实际探测到的列名（联调时一眼就能核对）
- `WARNING`：打出「代码依赖但表里不存在」的列（明确指向真正的坏点）

只读 `PRAGMA table_info`，不写入、不改动任何数据；每个库只打一次。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

# 已自检过的库（按「库路径 + 表集合」去重，热加载换库后会重新自检）
_reported: Set[str] = set()
_reported_lock = threading.Lock()

# 期望的列：表名 -> 代码实际查询/读取的列
PollerExpectations = Mapping[str, Sequence[str]]

# ── 各库的表与代码依赖列（逐条对应轮询器里的 SQL）─────────────────────────────
# 列名一旦与真机不符，表现是「查不到数据 → 静默不推送」，所以这里列的是代码真正读写的列。
DB_SCHEMAS: Mapping[str, Tuple[str, PollerExpectations]] = {
    "logger": (
        "日志库 logger_data.db3",
        {
            "log": (
                "id", "serviceId", "uid", "uname", "logtime", "loglevel",
                "eventId", "parameter", "category",
            ),
        },
    ),
    "trim_media": (
        "影视库 trimmedia.db",
        {
            "item": (
                "guid", "type", "title", "path", "parent_guid", "season_number",
                "episode_number", "tmdb_id", "fetch_status", "create_time",
                "update_time", "overview", "release_date", "first_air_date",
                "air_date", "runtime",
            ),
            "item_media": ("guid", "item_guid", "path", "size", "create_time", "update_time"),
            # media_delete 列名跨版本差异最大，交给 probe_media_delete 单独提示
        },
    ),
    "photo": (
        "相册 photo.db",
        {
            "share_link": (
                "id", "name", "share_id", "owner", "album_id", "valid_to", "create_time",
            ),
            "user": ("id", "nas_uid"),
            "device": (
                "id", "user_id", "device_id", "device_name", "device_uniq_name", "created_at",
            ),
            "face_task_log": ("id", "user_photo_id", "user_id"),
            "user_photo": ("id", "photo_id"),
            "face": ("user_photo_id", "person_id"),
            "person": ("id", "name"),
        },
    ),
    "backup": (
        "备份库 basic_backup.db3",
        {
            "operations": (
                "id", "uid", "task_id", "status", "error_code", "error_message",
                "start_time", "finished_time", "files_count", "total_size",
                "completed_count", "completed_size", "actual_count", "actual_size",
                "actual_time", "comment",
            ),
            "user_tasks": ("name", "target_id", "target_path", "source_paths"),
            "storages": ("name", "address"),
        },
    ),
    "scheduler": (
        "任务计划 scheduler.db",
        {
            "task_results": (
                "id", "task_id", "status", "trigger_reason", "started_at", "finished_at", "log",
            ),
            "tasks": ("id", "name", "account", "trigger_type", "schedule_expression"),
        },
    ),
    "trim_activity": (
        "影视库活动 trimactivity.db",
        {
            "user_token": (
                "token", "user_guid", "ip", "app_name", "status", "create_time",
                "update_time", "device",
            ),
        },
    ),
}


def probe(conn, schema_key: str, logger: logging.Logger, db_path: str = "") -> Dict[str, Set[str]]:
    """按 DB_SCHEMAS 中的登记对某个库做自检（表名与依赖列集中维护，避免逐轮询器写散）。"""
    entry = DB_SCHEMAS.get(schema_key)
    if entry is None:
        return {}
    label, expectations = entry
    result = probe_database(conn, label, expectations, logger=logger, db_path=db_path)
    if schema_key == "trim_media":
        probe_media_delete(conn, logger, db_path)
    return result


def table_columns(conn, table: str) -> Set[str]:
    """返回表的实际列名集合；表不存在或无权限时返回空集合。

    使用 table-valued pragma（pragma_table_info(?)）参数化传入表名，
    避免 SQL 拼接；要求 SQLite ≥ 3.16（2017 年），fnOS 自带版本满足。
    """
    cols: Set[str] = set()
    try:
        cur = conn.execute("SELECT cid, name FROM pragma_table_info(?)", (table,))
    except Exception:
        return cols
    try:
        for row in cur.fetchall():
            try:
                # sqlite3.Row 支持按名取；普通 tuple 走索引
                name = row["name"] if hasattr(row, "keys") else row[1]
            except Exception:
                continue
            name = str(name).strip()
            if name:
                cols.add(name)
    except Exception:
        return cols
    return cols


def table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def probe_database(
    conn,
    db_label: str,
    expectations: PollerExpectations,
    *,
    logger: Optional[logging.Logger] = None,
    db_path: str = "",
    required_tables: Iterable[str] = (),
) -> Dict[str, Set[str]]:
    """对单个库做一次 schema 自检并写日志，返回 {表名: 实际列集合}。

    db_label 用于日志辨识（如「影视库 trimmedia.db」）；
    required_tables 中的表缺失时按 WARNING 报（表存在但缺列也在 WARNING 里逐条列出）。
    """
    log = logger or logging.getLogger(__name__)
    key = f"{db_path or db_label}::{','.join(sorted(expectations))}"
    with _reported_lock:
        if key in _reported:
            return {}
        _reported.add(key)

    result: Dict[str, Set[str]] = {}
    missing_tables: list[str] = []
    missing_columns: list[Tuple[str, str]] = []

    for table, expected in expectations.items():
        cols = table_columns(conn, table)
        if not cols and not table_exists(conn, table):
            missing_tables.append(table)
            continue
        result[table] = cols
        for col in expected:
            if col not in cols:
                missing_columns.append((table, col))

    if result:
        log.info(
            "[schema 自检] %s%s 实际列：%s",
            db_label,
            f"（{db_path}）" if db_path else "",
            "; ".join(f"{t}=({', '.join(sorted(c))})" for t, c in sorted(result.items())),
        )
    for table in missing_tables:
        log.warning(
            "[schema 自检] %s 缺少表 `%s`（该表对应的事件类型将不会有任何推送）；"
            "若飞牛版本改名，请提交 issue 告知实际表名",
            db_label,
            table,
        )
    for table, col in missing_columns:
        log.warning(
            "[schema 自检] %s 表 `%s` 缺少列 `%s`：依赖该列的查询会查不到数据而静默不推送，"
            "请对照实际列名修正",
            db_label,
            table,
            col,
        )
    for table in sorted(set(required_tables) - set(result) - set(missing_tables)):
        log.warning("[schema 自检] %s 必需表 `%s` 不可读", db_label, table)

    return result


def reset_schema_probe_cache() -> None:
    """清除自检去重记录（测试用）。"""
    with _reported_lock:
        _reported.clear()


def probe_media_delete(conn, logger: logging.Logger, db_path: str = "") -> Set[str]:
    """影视库 media_delete 表自检：该表结构跨版本差异最大，单独提示。"""
    cols = table_columns(conn, "media_delete")
    if not cols:
        logger.info(
            "[schema 自检] 影视库 media_delete 表不存在，跳过影视资源删除事件（不影响其它影视事件）"
        )
        return cols
    logger.info("[schema 自检] media_delete 实际列：%s", ", ".join(sorted(cols)))
    if not {"update_time", "delete_time", "create_time", "time"} & cols:
        logger.warning(
            "[schema 自检] media_delete 没有可用的时间列（update_time/delete_time/create_time/time），"
            "影视资源删除事件将被跳过"
        )
    return cols
