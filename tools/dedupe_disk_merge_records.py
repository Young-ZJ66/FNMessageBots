#!/usr/bin/env python3
"""清理「磁盘合并推送」的重复补记记录，并回退由此虚高的推送统计。

背景
----
修复 9e351af 之前，一次磁盘合并推送在 send_notification 路径上会写两条推送记录：

  ① 补记   摘要「磁盘事件合并推送 DiskWakeup × N」，detail 里 kind=merged_disk_event
  ② 内容   摘要就是实际推送文案，detail 里 event_data.merged_disks 带逐盘明细

同时 push_stats 被记两次（total 与 daily 各 +1），于是汇总里一次推送算成两次。

⚠ 只清理**能配对**的补记记录。走合并窗口定时线程、关停时 flush 的推送本来就只写
①（没有②），那些记录是唯一留痕，删掉就丢数据了，必须保留。

用法
----
    # 1) 先看报告，不改任何东西（默认就是 dry-run）
    python tools/dedupe_disk_merge_records.py

    # 2) 确认数字无误后执行（会先把库与统计文件备份成 *.bak.<时间戳>）
    python tools/dedupe_disk_merge_records.py --apply

数据目录默认 ./data/cursor（与 config.cursor_dir 的默认值一致）。
在容器里跑通常是：
    docker exec -it <容器名> python tools/dedupe_disk_merge_records.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 补记记录的摘要前缀，与 unified_notifier._record_merged_event_send 里写的保持一致
MERGED_SUMMARY_PREFIX = "磁盘事件合并推送"
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def parse_ts(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime((text or "").strip(), TIME_FMT)
    except Exception:
        return None


def load_backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, target)
    return target


def collect(data_dir: Path, window_sec: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """返回 (可配对删除的补记, 需保留的补记, 内容记录索引)。"""
    db = data_dir / "push_history.db"
    if not db.is_file():
        raise SystemExit(f"找不到推送记录库：{db}")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, created_at, event_type, success, summary FROM push_history"
        )]
    finally:
        conn.close()

    merged = [r for r in rows if (r.get("summary") or "").startswith(MERGED_SUMMARY_PREFIX)]
    contents = [r for r in rows if not (r.get("summary") or "").startswith(MERGED_SUMMARY_PREFIX)]

    # 按 event_type 分组内容记录，避免 O(n²) 全表比较
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for r in contents:
        by_type.setdefault(str(r.get("event_type") or ""), []).append(r)

    paired: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    for m in merged:
        mt = parse_ts(m.get("created_at"))
        partners = by_type.get(str(m.get("event_type") or ""), [])
        hit = None
        if mt is not None:
            for c in partners:
                ct = parse_ts(c.get("created_at"))
                if ct is None:
                    continue
                if abs((ct - mt).total_seconds()) <= window_sec:
                    hit = c
                    break
        if hit is None:
            kept.append(m)            # 定时线程/flush 路径：唯一留痕，保留
        else:
            m = dict(m)
            m["partner_id"] = hit.get("id")
            m["partner_summary"] = (hit.get("summary") or "")[:60]
            paired.append(m)
    return paired, kept, contents


def plan_stats(paired: List[Dict[str, Any]], stats: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """算出统计回退后的结果，返回 (新统计, 变更说明)。"""
    new_stats = json.loads(json.dumps(stats))       # 深拷贝
    total = new_stats.setdefault("total", {"success": 0, "fail": 0})
    daily = new_stats.setdefault("daily", {})
    notes: List[str] = []

    per_day: Dict[Tuple[str, str], int] = {}
    per_field = {"success": 0, "fail": 0}
    for m in paired:
        field = "success" if int(m.get("success") or 0) else "fail"
        per_field[field] += 1
        day = str(m.get("created_at") or "")[:10]
        per_day[(day, field)] = per_day.get((day, field), 0) + 1

    for field, n in per_field.items():
        if not n:
            continue
        before = int(total.get(field, 0) or 0)
        after = max(0, before - n)
        total[field] = after
        notes.append(f"total.{field}: {before} → {after}（-{n}）")

    for (day, field), n in sorted(per_day.items()):
        bucket = daily.get(day)
        if not isinstance(bucket, dict):
            notes.append(f"daily[{day}] 不存在，跳过（-{n} {field} 未回退）")
            continue
        before = int(bucket.get(field, 0) or 0)
        after = max(0, before - n)
        bucket[field] = after
        notes.append(f"daily[{day}].{field}: {before} → {after}（-{n}）")

    return new_stats, notes


def write_stats(path: Path, stats: Dict[str, Any]) -> None:
    directory = str(path.parent) or "."
    fd, tmp = tempfile.mkstemp(prefix=".push_stats.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="清理磁盘合并推送的重复补记记录并回退推送统计（默认 dry-run）",
    )
    ap.add_argument("--data-dir", default="./data/cursor",
                    help="数据目录（含 push_history.db 与 push_stats.json），默认 ./data/cursor")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行删除与统计回退（会先备份两个文件）；不加则只报告")
    ap.add_argument("--window", type=int, default=2,
                    help="配对时间窗（秒），默认 2")
    ap.add_argument("--list", type=int, default=10,
                    help="最多列出多少条样例，默认 10")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser().resolve()
    db = data_dir / "push_history.db"
    stats_path = data_dir / "push_stats.json"
    print(f"数据目录：{data_dir}")
    print(f"记录库  ：{db}")
    print(f"统计文件：{stats_path}{'' if stats_path.is_file() else '（不存在，将跳过统计回退）'}")
    print()

    paired, kept, contents = collect(data_dir, args.window)
    total_merged = len(paired) + len(kept)
    print(f"补记记录（摘要以「{MERGED_SUMMARY_PREFIX}」开头）：{total_merged} 条")
    print(f"  · 可配对、将被删除：{len(paired)} 条")
    print(f"  · 无配对、必须保留：{len(kept)} 条（定时线程/flush 路径的唯一留痕）")
    if not total_merged:
        print("\n没有需要处理的记录。")
        return 0

    if paired and args.list:
        print(f"\n待删除样例（最多 {args.list} 条）：")
        for m in paired[: args.list]:
            print(f"  #{m['id']:<6} {m.get('created_at')}  {m.get('event_type')}  "
                  f"→ 配对 #{m['partner_id']}  {(m.get('partner_summary') or '')}")
    if kept and args.list:
        print(f"\n保留样例（最多 {args.list} 条）：")
        for m in kept[: args.list]:
            print(f"  #{m['id']:<6} {m.get('created_at')}  {m.get('event_type')}  "
                  f"{(m.get('summary') or '')[:48]}")

    stats: Dict[str, Any] = {}
    if stats_path.is_file() and paired:
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except Exception as e:                       # noqa: BLE001
            raise SystemExit(f"读取统计文件失败：{e}")
        _, notes = plan_stats(paired, stats)
        print("\n统计回退预览：")
        for n in notes:
            print(f"  · {n}")

    if not args.apply:
        print("\n这是 dry-run，未修改任何文件。确认无误后加 --apply 执行。")
        return 0

    if not paired:
        print("\n没有可删除的重复记录，统计也无需回退。")
        return 0

    print("\n开始执行……")
    db_backup = load_backup(db)
    print(f"  已备份记录库 → {db_backup.name}")
    if stats_path.is_file():
        st_backup = load_backup(stats_path)
        print(f"  已备份统计   → {st_backup.name}")

    conn = sqlite3.connect(str(db))
    try:
        with conn:
            conn.executemany("DELETE FROM push_history WHERE id = ?",
                             [(m["id"],) for m in paired])
    finally:
        conn.close()
    print(f"  已删除 {len(paired)} 条重复补记记录")

    if stats_path.is_file() and stats:
        new_stats, notes = plan_stats(paired, stats)
        write_stats(stats_path, new_stats)
        print("  统计已回退：")
        for n in notes:
            print(f"    · {n}")

    print("\n完成。刷新页面后：推送记录少掉重复项，汇总统计与真实推送次数一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
