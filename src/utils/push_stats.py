"""
推送统计：总条数/成功/失败、当日条数/成功/失败，持久化到 cursor_dir/push_stats.json

与 `utils/push_history.py` 的关系（避免后来者误配）：

- **权威源是 `push_history.db`**：Web 的「推送汇总/统计」读的是它的实时聚合
  （`web/push_history_service.get_stats`），能按条查明细、按渠道看返回体。
- **本模块是轻量聚合视图**：只累加成功/失败计数，不含明细；写入时机与
  `push_history` 相同（都在 `UnifiedNotifier.send_notification` 内），
  但两者各自独立落盘，异常时可能出现微小偏差。
- 因此：新增统计需求请走 `push_history`；本文件仅为兼容既有行为与快速查看保留。
  不要把它的计数当作精确值做对账。
"""

import json
import os
import tempfile
import threading
import copy
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from utils.logtime_display import get_display_timezone, local_tzinfo

_lock = threading.Lock()
_stats_path: str = ""

_DEFAULT = {"total": {"success": 0, "fail": 0}, "daily": {}}


def init(cursor_dir: str) -> None:
    """初始化统计文件路径。cursor_dir 可为相对路径（相对当前工作目录）。同时初始化推送记录 DB。"""
    global _stats_path
    if not cursor_dir:
        cursor_dir = "./data/cursor"
    abs_dir = os.path.abspath(os.path.join(os.getcwd(), cursor_dir))
    Path(abs_dir).mkdir(parents=True, exist_ok=True)
    _stats_path = os.path.join(abs_dir, "push_stats.json")
    try:
        from utils.push_history import init as init_push_history
        init_push_history(cursor_dir)
    except Exception:
        pass


def _load() -> Dict[str, Any]:
    if not _stats_path or not os.path.isfile(_stats_path):
        return copy.deepcopy(_DEFAULT)
    try:
        with open(_stats_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "total" in data and "daily" in data:
                return data
    except Exception:
        pass
    return copy.deepcopy(_DEFAULT)


def _save(data: Dict[str, Any]) -> None:
    """原子写入：先写同目录临时文件再 replace，避免进程被杀/磁盘写满时把统计文件写坏后清零。"""
    if not _stats_path:
        return
    tmp_path = ""
    try:
        directory = os.path.dirname(_stats_path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".push_stats.", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _stats_path)
        tmp_path = ""
    except Exception:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _today_key() -> str:
    return datetime.now(get_display_timezone() or local_tzinfo()).strftime("%Y-%m-%d")


def record(success: bool) -> None:
    """记录一次推送结果。"""
    with _lock:
        data = _load()
        if success:
            data["total"]["success"] = data["total"].get("success", 0) + 1
        else:
            data["total"]["fail"] = data["total"].get("fail", 0) + 1
        key = _today_key()
        if key not in data["daily"]:
            data["daily"][key] = {"success": 0, "fail": 0}
        if success:
            data["daily"][key]["success"] = data["daily"][key].get("success", 0) + 1
        else:
            data["daily"][key]["fail"] = data["daily"][key].get("fail", 0) + 1
        _save(data)


def get_total() -> Dict[str, int]:
    """返回总统计：total, success, fail。"""
    with _lock:
        data = _load()
    t = data.get("total", {})
    s = t.get("success", 0)
    f = t.get("fail", 0)
    return {"total": s + f, "success": s, "fail": f}


def get_today() -> Dict[str, int]:
    """返回当日统计：total, success, fail。"""
    with _lock:
        data = _load()
    key = _today_key()
    d = data.get("daily", {}).get(key, {})
    s = d.get("success", 0)
    f = d.get("fail", 0)
    return {"total": s + f, "success": s, "fail": f}


def get_stats_path() -> str:
    """返回当前统计文件路径（供 Web 读取用）。"""
    return _stats_path
