"""推送消息预览：本地渲染各类事件实际发给企业微信的 markdown 内容（不联网）。

用法（项目根目录）：
    python tools/preview_push.py [事件类型 ...]

不带参数时输出一组典型事件的完整预览（标题、纯文本正文、企业微信 markdown payload）。
使用 tests/_support/stub_deps.py 的依赖桩，不联网、不需要第三方依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tests" / "_support"))
sys.path.insert(0, str(_ROOT / "src"))

import stub_deps  # noqa: E402

stub_deps.install()

from notifier.message_format import (  # noqa: E402
    MultiPlatformMessage,
    _markdown_body_from_plain,
)
from notifier.multi_platform_notifier import (  # noqa: E402
    MultiPlatformNotifier,
    reset_rich_text_rejected_channels,
)


def _make() -> MultiPlatformNotifier:
    reset_rich_text_rejected_channels()
    n = MultiPlatformNotifier(
        wechat_webhook_url="https://example.test/webhook",
        title_prefix="飞牛NAS",
    )
    n.merge_window = 3600
    return n


def _print_payload(name: str, msg: MultiPlatformMessage) -> None:
    payload = msg.to_wechat_markdown_format()["markdown"]["content"]
    print("=" * 64)
    print(f"【{name}】 msgtype=markdown（企业微信群实际收到）")
    print("-" * 64)
    print(payload)
    print()


def _show(notifier: MultiPlatformNotifier, name: str, event_type: str, event_data: dict, ts: str) -> None:
    msg = notifier._build_message(event_type, event_data, ts, "{}")
    _print_payload(name, msg)


def _show_system(notifier: MultiPlatformNotifier, name: str, event_type: str, message: str, ts: str) -> None:
    """按 send_system_notification 的真实构建路径渲染系统通知（含用户消息正文）。"""
    title = notifier._with_title_prefix(
        notifier.EVENT_TITLES.get(event_type, notifier._fallback_event_title(event_type))
    )
    event_data = {"message": message, "timestamp": ts, "hostname": "fnos", "version": "2.7.0"}
    content = notifier._build_system_content(event_type, event_data, message)
    msg = MultiPlatformMessage(title=notifier._clean_text(title), content=content)
    msg.markdown = _markdown_body_from_plain(notifier._clean_text(content))
    _print_payload(name, msg)


def _show_batch(notifier: MultiPlatformNotifier) -> None:
    """影视批量入库聚合消息（media_batch_summary_enabled 开启后的形态）。"""
    items = [
        {
            "timestamp": f"2026-10-11 12:0{i:02d}:00",
            "event_data": {
                "title": f"影片标题{i}",
                "item_type": "Movie" if i % 2 else "TV",
                "season_number": None if i % 2 else 1,
                "episode_number": None if i % 2 else i,
                "path": f"/vol1/media/电影/影片标题{i}/影片标题{i}.mkv",
                "_source": "trim_media_db",
            },
            "raw_log": "{}",
        }
        for i in range(1, 13)
    ]
    grouped = {"TRIM_RESOURCE_ADDED": items}
    event_data = {
        "count": len(items),
        "by_type": {"TRIM_RESOURCE_ADDED": len(items)},
        "items": [{"event_type": "TRIM_RESOURCE_ADDED", "timestamp": items[0]["timestamp"], "brief": "影片标题1"}],
        "grouped_events": grouped,
    }
    _show(notifier, f"影视批量入库聚合（{len(items)} 条）", "POLL_BATCH_SUMMARY", event_data, "2026-10-11 12:05:00")


def main() -> None:
    n = _make()
    try:
        requested = sys.argv[1:]
        samples = [
            ("登录成功", "LoginSucc", {"user": "admin", "IP": "192.168.1.10", "via": "密码"}, "2026-10-11 12:00:01"),
            ("SSH 登录失败告警", "SSH_AUTH_FAILED", {"user": "root", "IP": "203.0.113.7", "port": "22", "reason": "密码错误"}, "2026-10-11 12:03:00"),
            ("应用崩溃", "APP_CRASH", {"data": {"DISPLAY_NAME": "Jellyfin", "APP_ID": "app-123"}, "from": "fnos"}, "2026-10-11 12:04:00"),
            ("影视资源入库（单条）", "TRIM_RESOURCE_ADDED", {"title": "沙丘 2", "item_type": "Movie", "path": "/vol1/media/电影/沙丘2/沙丘2.mkv"}, "2026-10-11 12:00:00"),
            ("UPS 切换电池（备注转引用块）", "UPS_ONBATT", {}, "2026-10-11 12:10:00"),
            ("监控启动（系统通知）", "APP_START", {"message": "飞牛NAS日志监控系统已启动，开始监控系统事件", "hostname": "fnos", "version": "2.7.0"}, "2026-10-11 12:00:00"),
            ("测试推送", "TEST_PUSH", {"message": "这是一条测试消息", "hostname": "fnos", "version": "2.7.0"}, "2026-10-11 12:00:00"),
        ]
        system_samples = [
            ("监控启动（系统通知）", "APP_START", "飞牛NAS日志监控系统已启动，开始监控系统事件", "2026-10-11 12:00:00"),
            ("测试推送", "TEST_PUSH", "这是一条测试消息", "2026-10-11 12:00:00"),
        ]
        for name, et, ed, ts in samples:
            if requested and et not in requested:
                continue
            if et in ("APP_START", "TEST_PUSH"):
                continue
            _show(n, name, et, ed, ts)
        for name, et, message, ts in system_samples:
            if requested and et not in requested:
                continue
            _show_system(n, name, et, message, ts)
        if not requested or "POLL_BATCH_SUMMARY" in requested:
            _show_batch(n)
    finally:
        n.close()


if __name__ == "__main__":
    main()
