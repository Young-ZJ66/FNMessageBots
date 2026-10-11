"""事件处理器日志留存一致性测试。

运行方式（项目根目录）：
    python tests/test_event_processor_logging.py

验证「会推送的事件同时落原始日志」这一约定不因事件类型而漏项。
"""

from __future__ import annotations

import json
import shutil
import sys
import types
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT = Path(__file__).resolve().parent / "_support"
for _p in (str(_SUPPORT), str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stub_deps  # noqa: E402

stub_deps.install()

from monitor.event_processor import EventProcessor  # noqa: E402
from monitor.models import JournalEntry  # noqa: E402

_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def send_notification(self, event_type, event_data, raw_log, timestamp):
        self.calls.append((event_type, event_data, raw_log, timestamp))
        return SimpleNamespace(success=True)


def make_entry(payload: str = '{"data":{"THRESHOLD":90}}') -> JournalEntry:
    return JournalEntry(
        cursor="1",
        timestamp="2026-01-01 12:00:00",
        hostname="eventlogger",
        syslog_identifier="CPU_TEMPERATURE_ALARM",
        message=payload,
        priority=3,
        pid=0,
        raw_data=payload,
        original_line=payload,
    )


class EventLoggingCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _TMP_ROOT / f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        config = SimpleNamespace(
            log_dir=str(self.root / "logs"),
            log_retention_days=7,
            monitor_events=[],
        )
        self.notifier = FakeNotifier()
        self.processor = EventProcessor(self.notifier, config)
        self.stored = []
        self.processor._store_notification_log = lambda *a, **k: self.stored.append((a, k))

    def tearDown(self) -> None:
        try:
            self.processor.log_storage.stop_cleanup_thread()
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)


class TestCpuTemperatureAlarmLogging(EventLoggingCase):
    def test_alarm_pushes_and_stores_raw_log(self):
        entry = make_entry()
        event_data = {"data": {"THRESHOLD": 90}}

        self.processor._handle_cpu_temperature_alarm(event_data, entry)

        self.assertEqual(len(self.notifier.calls), 1, "仍应推送通知")
        self.assertEqual(self.notifier.calls[0][0], "CPU_TEMPERATURE_ALARM")
        self.assertEqual(len(self.stored), 1, "CPU 温度告警应同时落原始日志")
        args, kwargs = self.stored[0]
        self.assertEqual(args[0], "CPU_TEMPERATURE_ALARM")
        self.assertEqual(kwargs.get("source"), "db")

    def test_alarm_still_dispatches_via_handler_map(self):
        handler = self.processor.get_handler("CPU_TEMPERATURE_ALARM")
        self.assertIsNotNone(handler)
        handler({"data": {"THRESHOLD": 95}}, make_entry())
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(len(self.stored), 1)


class TestAllPushEventsStoreRawLog(EventLoggingCase):
    """约定：会推送的事件都必须落原始日志。

    通过 handler 映射与源码检查兜住「新增事件时忘记落盘」的回归——
    这类漏项不会报错，只会让 ./data/logs 里静默少掉一类事件，事后几乎无法发现。
    """

    # 经共享方法落盘的 handler（这些方法内部调用了 _store_notification_log）
    DELEGATING_TARGETS = {
        "_send_ssh_notification",     # SSH 事件短窗口合并后发出
        "_schedule_ssh_event",        # 合并入口，最终走 _send_ssh_notification
        "_handle_simple_notification",  # 通用事件
        "_handle_app_lifecycle",
        "_handle_trim_library_db",
        "_handle_docker_notification",
    }
    STORAGE_CALL = "_store_notification_log"

    @staticmethod
    def _source() -> str:
        import monitor.event_processor as mod

        return Path(mod.__file__).read_text(encoding="utf-8")

    def test_every_registered_handler_stores_raw_log(self):
        import re

        source = self._source()
        missing = []
        for event_type, handler in sorted(self.processor.handlers.items()):
            method_name = getattr(handler, "__name__", "")
            if method_name == "<lambda>" or not method_name.startswith("_handle_"):
                # lambda 包装：从源码里取它转发的目标方法
                target = self._lambda_target(source, event_type)
                if target is None:
                    missing.append((event_type, "无法解析转发目标"))
                    continue
                method_name = target
            block = re.search(
                r"\n    def " + re.escape(method_name) + r"\(.*?(?=\n    def |\Z)", source, re.S
            )
            if block is None:
                missing.append((event_type, f"{method_name} 未找到"))
                continue
            body = block.group(0)
            if self.STORAGE_CALL in body or any(t in body for t in self.DELEGATING_TARGETS):
                continue
            missing.append((event_type, method_name))

        self.assertEqual(
            missing,
            [],
            "以下事件会推送但不落原始日志（README 承诺推送数据写入 ./data/logs）："
            + ", ".join(f"{et}({m})" for et, m in missing),
        )

    @staticmethod
    def _lambda_target(source: str, event_type: str):
        import re

        m = re.search(
            r"'%s':\s*lambda[^:]*:\s*self\.(_handle_[A-Za-z_0-9]+)" % re.escape(event_type),
            source,
        )
        return m.group(1) if m else None


class TestFixedHandlersStoreLog(EventLoggingCase):
    """本次修复的具体事件：既推送也落盘。"""

    def _assert_stores(self, invoke, event_type, event_data):
        invoke(event_data, make_entry(json.dumps(event_data, ensure_ascii=False)))
        self.assertEqual(len(self.notifier.calls), 1, f"{event_type} 应推送")
        self.assertEqual(self.notifier.calls[0][0], event_type)
        self.assertEqual(len(self.stored), 1, f"{event_type} 应落原始日志")
        self.assertEqual(self.stored[0][0][0], event_type)

    def test_login_events(self):
        self._assert_stores(
            self.processor._handle_login_2fa, "LoginSucc2FA1", {"user": "bob", "IP": "10.0.0.1"}
        )
        self.stored.clear()
        self.notifier.calls.clear()
        self._assert_stores(
            self.processor._handle_login_fail, "LoginFail", {"user": "bob", "IP": "10.0.0.1"}
        )
        self.stored.clear()
        self.notifier.calls.clear()
        self._assert_stores(
            self.processor._handle_logout, "Logout", {"user": "bob", "IP": "10.0.0.1"}
        )

    def test_found_disk(self):
        self._assert_stores(
            self.processor._handle_found_disk, "FoundDisk", {"name": "sda", "model": "X"}
        )

    def test_app_failures(self):
        for handler, event_type in (
            (self.processor._handle_app_crash, "APP_CRASH"),
            (self.processor._handle_app_update_failed, "APP_UPDATE_FAILED"),
            (
                self.processor._handle_app_start_failed_local,
                "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
            ),
            (
                self.processor._handle_app_auto_start_failed_docker,
                "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
            ),
        ):
            with self.subTest(event_type=event_type):
                self.stored.clear()
                self.notifier.calls.clear()
                self._assert_stores(
                    handler, event_type, {"data": {"DISPLAY_NAME": "某应用"}}
                )

    def test_ups_events(self):
        for handler, event_type in (
            (self.processor._handle_ups_onbatt, "UPS_ONBATT"),
            (self.processor._handle_ups_onbatt_lowbatt, "UPS_ONBATT_LOWBATT"),
            (self.processor._handle_ups_online, "UPS_ONLINE"),
            (self.processor._handle_ups_enable, "UPS_ENABLE"),
            (self.processor._handle_ups_disable, "UPS_DISABLE"),
        ):
            with self.subTest(event_type=event_type):
                self.stored.clear()
                self.notifier.calls.clear()
                self._assert_stores(handler, event_type, {"message": "UPS 状态变化"})


class TestNoRegressionOnCommonEvents(EventLoggingCase):
    def test_simple_notification_still_stores(self):
        self.processor._handle_simple_notification(
            "DeleteFile", {"message": "x"}, make_entry()
        )
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(len(self.stored), 1)

    def test_unknown_event_has_no_handler(self):
        self.assertIsNone(self.processor.get_handler("DefinitelyNotAnEvent"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
