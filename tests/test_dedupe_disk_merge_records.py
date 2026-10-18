"""tools/dedupe_disk_merge_records.py 的回归测试。

重点是那条**绝不能做错**的事：只能删「能配对」的重复补记记录。
走合并窗口定时线程、关停 flush 的推送本来就只写一条补记（没有内容记录），
删掉就丢数据了。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

COMPLEMENT_MARK = "磁盘事件合并推送"


def _load_tool():
    path = _ROOT / "tools" / "dedupe_disk_merge_records.py"
    spec = importlib.util.spec_from_file_location("dedupe_disk_merge_records", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DedupeCase(unittest.TestCase):
    def setUp(self) -> None:
        import utils.push_history as push_history
        import utils.push_stats as push_stats

        self.root = _ROOT / "tests" / "_tmp" / f"dedupe_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        push_history.init(str(self.root))
        push_stats.init(str(self.root))
        self.push_history = push_history
        self.push_stats = push_stats
        self.push_history.clear_all()
        self.tool = _load_tool()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _add_merged(self, event_type: str, count: int) -> None:
        self.push_history.add_record(
            success=True,
            event_type=event_type,
            summary=f"{COMPLEMENT_MARK} {event_type} × {count}",
            detail={"kind": "merged_disk_event", "event_type": event_type, "merged_count": count},
        )

    def _add_content(self, event_type: str) -> None:
        self.push_history.add_record(
            success=True,
            event_type=event_type,
            summary="磁盘已进入休眠，属正常节能行为。",
            detail={
                "event_type": event_type,
                "event_data": {"merged_disks": [{"disk": "/dev/sda", "model": "WD40"}], "count": 1},
                "channel_results": [{"channel": "企业微信", "success": True}],
            },
        )

    def _run(self, *extra: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.tool.main(["--data-dir", str(self.root), *extra])
        return buf.getvalue()

    def test_dry_run_changes_nothing(self):
        self._add_content("DiskSpindown")
        self._add_merged("DiskSpindown", 1)
        before = self.push_history.get_total_counts()["total"]

        out = self._run()

        self.assertIn("dry-run", out)
        self.assertEqual(self.push_history.get_total_counts()["total"], before, "dry-run 不得删记录")
        self.assertEqual(list(self.root.glob("*.bak.*")), [], "dry-run 不得产生备份文件")

    def test_only_paired_merged_record_is_deleted(self):
        # 一对：内容记录 + 同一秒的补记
        self._add_content("DiskSpindown")
        self._add_merged("DiskSpindown", 1)
        # 孤独补记：定时线程路径，没有内容记录
        self._add_merged("DiskWakeup", 2)
        self.assertEqual(self.push_history.get_total_counts()["total"], 3)

        self._run("--apply")

        records = self.push_history.get_records(limit=10)
        self.assertEqual(len(records), 2, "只应删掉可配对的那条补记")
        event_types = sorted(r["event_type"] for r in records)
        self.assertEqual(event_types, ["DiskSpindown", "DiskWakeup"])
        # 留下的是内容记录（摘要不是「…× N」），不是补记
        spindown = [r for r in records if r["event_type"] == "DiskSpindown"][0]
        self.assertNotIn(COMPLEMENT_MARK, spindown["summary"], "留下的应是内容记录")
        self.assertIn("merged_disks", json.loads(spindown["detail"])["event_data"],
                      "逐盘明细必须还在，否则前端表格就空了")
        # 孤独的 DiskWakeup 补记必须保留
        wakeup = [r for r in records if r["event_type"] == "DiskWakeup"][0]
        self.assertIn(COMPLEMENT_MARK, wakeup["summary"])

    def test_stats_are_rolled_back(self):
        # 模拟旧 bug：一次推送被记两次统计
        self._add_content("DiskSpindown")
        self._add_merged("DiskSpindown", 1)
        self.push_stats.record(True)
        self.push_stats.record(True)
        before = self.push_stats.get_total()

        self._run("--apply")

        after = self.push_stats.get_total()
        self.assertEqual(after["success"], before["success"] - 1, "统计应回退 1")
        self.assertEqual(after["total"], before["total"] - 1)

    def test_backups_are_created_before_writing(self):
        self._add_content("DiskSpindown")
        self._add_merged("DiskSpindown", 1)
        self.push_stats.record(True)

        self._run("--apply")

        backups = sorted(p.name for p in self.root.glob("*.bak.*"))
        self.assertTrue(any("push_history.db.bak." in n for n in backups), backups)
        self.assertTrue(any("push_stats.json.bak." in n for n in backups), backups)


if __name__ == "__main__":
    unittest.main()
