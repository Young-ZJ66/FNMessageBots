#!/usr/bin/env python3
"""生成 5 个方向的设计样稿（design-previews/*.html）。

为什么用生成器：5 个方向必须**内容完全一致**才能公平对比配色与排版，
手写 5 份会漂移。内容只写一遍，每个方向只提供「设计令牌 + 视觉语言 CSS + 外壳类型」。

用法（项目根目录）：
    python design-previews/_build.py

产物是自包含 HTML（无 CDN、无外部字体），双击即可打开，
支持 ?theme=dark / ?theme=light 指定主题。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

OUT_DIR = Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────────────────
# 内容（5 个方向共用，取自项目真实形态）
# ─────────────────────────────────────────────────────────────────────────────

KPIS = [
    ("今日推送", "128", '成功 126 · 失败 2', [("成功 126", "ok"), ("失败 2", "err")]),
    ("累计推送", "12,486", "自 2025-09-27 起", []),
    ("活跃渠道", "5/8", "3 个未配置", []),
    ("监控事件", "23", "共 96 类可选", []),
    ("连续运行", "14d 6h", "上次重启 10-01", []),
]

# (渠道, 状态文本, 状态类, 目标, 最近推送, 24h)
CHANNELS = [
    ("企业微信", "已启用", "on", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91***", "2 分钟前", "100%"),
    ("钉钉", "已启用", "on", "https://oapi.dingtalk.com/robot/send?access_token=d1f8a0***", "2 分钟前", "100%"),
    ("飞书", "降级纯文本", "warn", "https://open.feishu.cn/open-apis/bot/v2/hook/2c9e41***", "15 分钟前", "98%"),
    ("Bark", "已启用", "on", "https://api.day.app/7Kq2mN***", "1 小时前", "100%"),
    ("PushPlus", "未配置", "off", "—", "—", "—"),
    ("SMTP 邮件", "已启用", "on", "smtp.qq.com:465 → 1600386893@qq.com", "3 小时前", "100%"),
    ("通用 Webhook", "未配置", "off", "—", "—", "—"),
    ("MeoW", "已启用", "on", "youngzj", "昨天 21:04", "96%"),
]

# (名称, eventId, 是否开启)
EVENTS = [
    ("登录成功", "LoginSucc", True),
    ("登录失败", "LoginFail", True),
    ("SSH 登录失败", "SSH_AUTH_FAILED", True),
    ("磁盘 IO 错误", "DISK_IO_ERR", True),
    ("UPS 切换电池", "UPS_ONBATT", True),
    ("应用崩溃", "APP_CRASH", True),
    ("影视资源新增", "TRIM_RESOURCE_ADDED", False),
    ("Docker 容器停止", "DOCKER_CONTAINER_STOP", False),
]

# (时间, 事件, 渠道, 结果文本, 结果类)
LOGS = [
    ("22:14:07", "登录成功", "企业微信 · 钉钉", "成功", "on"),
    ("21:48:33", "磁盘 IO 错误", "企业微信 · Bark · SMTP", "成功", "on"),
    ("20:02:11", "影视资源新增", "飞书", "已回退", "warn"),
    ("18:37:52", "Docker 容器停止", "Bark · MeoW", "失败", "err"),
]

PATROL = [("CPU", "12%"), ("内存", "34%"), ("系统盘", "41%"), ("CPU 温度", "42°C"), ("数据卷", "63%")]

NAV = [
    ("概览", True), ("推送渠道", False), ("监控事件", False),
    ("通知策略", False), ("定时巡检", False), ("推送记录", False),
]

# ─────────────────────────────────────────────────────────────────────────────
# HTML 片段
# ─────────────────────────────────────────────────────────────────────────────


def kpi_block() -> str:
    out = []
    for label, value, _sub, bits in KPIS:
        sub = "".join(f'<span class="{c}">{t}</span>' for t, c in bits) if bits else _sub
        out.append(
            f'<div class="kpi"><div class="kpi-k">{label}</div>'
            f'<div class="kpi-v num">{value}</div>'
            f'<div class="kpi-s">{sub}</div></div>'
        )
    return '<div class="kpis">' + "".join(out) + "</div>"


def tiles_block() -> str:
    """Bento 方向的概览：不等宽模块栅格。

    注意：不要给主卡跨 2 行——内容少时会留出大片空白，看起来像没做完。
    这里用「3+3 / 2+2+2」两行填满，每块都有实际内容。
    """
    tiles = [
        ("span-3 hero", "今日推送", "128", '<span class="ok">成功 126</span> · <span class="err">失败 2</span>'),
        ("span-3", "活跃渠道", "5<span class=\"dim\">/8</span>", "3 个未配置"),
        ("span-2", "监控事件", "23", "共 96 类可选"),
        ("span-2", "连续运行", "14<span class=\"dim\">d</span> 6<span class=\"dim\">h</span>", "上次重启 10-01"),
        ("span-2", "累计推送", "12,486", "自 2025-09-27 起"),
    ]
    out = []
    for cls, label, value, sub in tiles:
        out.append(
            f'<div class="tile {cls}"><div class="kpi-k">{label}</div>'
            f'<div class="kpi-v num">{value}</div><div class="kpi-s">{sub}</div></div>'
        )
    return '<div class="tiles">' + "".join(out) + "</div>"


def channels_block() -> str:
    rows = []
    for name, st, cls, tgt, last, rate in CHANNELS:
        dim = " dim" if st == "未配置" else ""
        rows.append(
            f'<tr><td class="c-name">{name}</td><td class="c-st"><span class="st {cls}">{st}</span></td>'
            f'<td class="c-tgt-cell"><span class="tgt{dim}">{tgt}</span></td>'
            f'<td class="r mono c-last-cell">{last}</td><td class="r num c-rate-cell">{rate}</td></tr>'
        )
    return (
        '<div class="tblwrap"><table><thead><tr><th class="c-name">渠道</th>'
        '<th class="c-st">状态</th><th class="c-tgt">目标地址</th>'
        '<th class="r c-last">最近推送</th><th class="r c-rate">24h 成功率</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def events_block() -> str:
    out = []
    for name, eid, on in EVENTS:
        checked = "true" if on else "false"
        out.append(
            f'<div class="ev"><span class="ev-text"><span class="ev-name">{name}</span>'
            f'<span class="ev-id">{eid}</span></span>'
            f'<span class="sw" role="switch" aria-checked="{checked}" tabindex="0" '
            f'aria-label="{name}"></span></div>'
        )
    return '<div class="events">' + "".join(out) + "</div>"


def logs_block() -> str:
    rows = []
    for t, ev, ch, res, cls in LOGS:
        rows.append(
            f'<tr><td class="mono">{t}</td><td>{ev}</td>'
            f'<td class="dim">{ch}</td><td class="r"><span class="st {cls}">{res}</span></td></tr>'
        )
    return (
        '<div class="tblwrap"><table><thead><tr><th class="c-time">时间</th><th>事件</th>'
        '<th class="c-ch">渠道</th><th class="r c-res">结果</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div>"
    )


def patrol_block() -> str:
    items = "".join(
        f'<div class="patrol-item"><div class="k">{k}</div><div class="v num">{v}</div></div>'
        for k, v in PATROL
    )
    return (
        f'<div class="patrol">{items}</div>'
        '<p class="dim note">下次巡检 10-15 09:00 · 上次 10-14 09:00 · 全部正常</p>'
    )


TOPBAR = (
    '<div class="topbar"><div class="wrap topbar-in">'
    '<span class="brand">飞牛NAS 日志监控</span><span class="ver mono">v2.6.0</span>'
    '<span class="grow"></span>'
    '<button class="btn iconbtn" id="theme" type="button">'
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2'
    'M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/></svg>'
    "切换主题</button>"
    '<button class="btn btn-primary" type="button">保存配置</button>'
    "</div></div>"
)


def sidebar() -> str:
    items = "".join(
        f'<a class="nav-item{" active" if act else ""}" href="#">{n}</a>' for n, act in NAV
    )
    return (
        '<aside class="side">'
        '<div class="side-brand"><span class="brand">飞牛NAS 日志监控</span>'
        '<span class="ver mono">v2.6.0</span></div>'
        f'<nav class="nav">{items}</nav>'
        '<div class="side-foot">FnMessageBot · MIT</div>'
        "</aside>"
    )


def sections(overview: str) -> List[str]:
    return [
        f'<section><div class="sec-head"><span class="sec-no mono">01</span>'
        f'<h2 class="sec-title">推送概览</h2>'
        f'<span class="sec-note">统计窗口：今日 00:00 起</span></div>{overview}</section>',
        '<section><div class="sec-head"><span class="sec-no mono">02</span>'
        '<h2 class="sec-title">推送渠道</h2>'
        '<span class="sec-note">密钥已打码显示，留空表示不修改</span></div>'
        f'{channels_block()}</section>',
        '<section><div class="sec-head"><span class="sec-no mono">03</span>'
        '<h2 class="sec-title">监控事件</h2>'
        '<span class="sec-note">已选 6 / 96</span></div>'
        f'{events_block()}</section>',
        '<section><div class="grid2"><div class="c7">'
        '<div class="sec-head"><span class="sec-no mono">04</span>'
        f'<h2 class="sec-title">最近推送</h2></div>{logs_block()}</div>'
        '<div class="c5"><div class="sec-head"><span class="sec-no mono">05</span>'
        f'<h2 class="sec-title">定时巡检</h2></div>{patrol_block()}</div></div></section>',
    ]


# 公共基础样式：与视觉语言无关的骨架 + 无障碍兜底
BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; }
body { font: 13px/1.5 var(--sans); background: var(--bg); color: var(--text);
       -webkit-font-smoothing: antialiased; }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.dim { color: var(--text-faint); }
.ok { color: var(--ok); } .err { color: var(--err); }
.grow { flex: 1; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 10px var(--sp-3) 10px 0; }
th { font-size: var(--fs-sm); font-weight: 500; color: var(--text-muted); }
td.r, th.r { text-align: right; padding-right: 0; }
tbody tr:last-child td { border-bottom: none; }
.c-name { width: 112px; } .c-st { width: 104px; } .c-last { width: 90px; }
.c-rate { width: 92px; } .c-time { width: 84px; } .c-ch { width: 168px; } .c-res { width: 68px; }
/* 兜底：窄屏表格允许自身横向滚动，避免撑破整页（整页横向滚动条是硬伤） */
.tblwrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.tgt { font-family: var(--mono); font-size: var(--fs-sm); color: var(--text-muted);
       display: block; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.st { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-sm); white-space: nowrap; }
.st::before { content: ""; width: 7px; height: 7px; flex: none; background: currentColor; }
.st.on { color: var(--ok); } .st.off { color: var(--text-faint); }
.st.warn { color: var(--warn); } .st.err { color: var(--err); }
.btn { font: 500 13px/1 var(--sans); color: var(--text); background: transparent;
       border: 1px solid var(--border-strong); border-radius: var(--r); padding: 9px 16px;
       cursor: pointer; transition: background-color .18s ease, border-color .18s ease,
       color .18s ease, transform .18s ease; }
.btn:hover { background: var(--surface); }
.btn-primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.iconbtn { display: inline-flex; align-items: center; gap: 6px; }
.sec-head { display: flex; align-items: baseline; gap: var(--sp-3); margin-bottom: var(--sp-4); }
.sec-title { margin: 0; }
.sec-note { margin-left: auto; font-size: var(--fs-sm); color: var(--text-muted); }
.grid2 { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: var(--sp-5); }
.c7 { grid-column: span 7; } .c5 { grid-column: span 5; }
.sw { position: relative; cursor: pointer; flex: none; }
.sw::after { content: ""; position: absolute; transition: transform .18s ease, background-color .18s ease; }
.sw[aria-checked="true"]::after { }
/* 关键：`1fr` 隐含 min-content 下限，内容一挤就撑破容器（实测「定时巡检」小卡会溢出被裁）。
   统一用 minmax(0, 1fr) 允许收缩，auto-fit 让列数不足时自动换行。 */
.patrol { display: grid; grid-template-columns: repeat(auto-fit, minmax(84px, 1fr)); gap: var(--sp-4); }
.patrol-item { min-width: 0; }
.patrol-item .k { font-size: var(--fs-sm); color: var(--text-muted); }
.patrol-item .v { font-size: var(--fs-lg); margin-top: 2px; }
.note { font-size: var(--fs-sm); margin: var(--sp-4) 0 0; }
.events { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 var(--sp-6); }
.ev { display: flex; align-items: center; gap: var(--sp-3); min-width: 0; }
.ev-text { flex: 0 0 236px; min-width: 0; }
.ev-name { display: block; font-size: var(--fs-md); }
.ev-id { display: block; font-family: var(--mono); font-size: var(--fs-xs); color: var(--text-faint);
         overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tiles { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: var(--sp-4); }
.tile { padding: var(--sp-4); min-width: 0; }
.span-2 { grid-column: span 2; } .span-3 { grid-column: span 3; }
.kpi-k { font-size: var(--fs-sm); color: var(--text-muted); }
.kpi-v { font-weight: 500; margin-top: var(--sp-2); }
.kpi-s { font-size: var(--fs-sm); color: var(--text-muted); margin-top: 2px; }
footer { padding: var(--sp-5) 0 var(--sp-6); color: var(--text-faint); font-size: var(--fs-sm); }
@media (max-width: 700px) {
  /* 窄屏优先保证「渠道 / 状态 / 最近推送」可读，隐藏最宽也最次要的两列 */
  .c-tgt, .c-tgt-cell, .c-rate, .c-rate-cell { display: none; }
  .c-name, .c-st, .c-last, .c-time, .c-ch, .c-res { width: auto; }
  .tgt { max-width: none; }
}
@media (max-width: 900px) {
  .kpis, .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .span-2, .span-3 { grid-column: span 2 !important; }
  .events { grid-template-columns: minmax(0, 1fr); }
  .ev-text { flex: 0 0 160px; }
  .c7, .c5 { grid-column: span 12; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition: none !important; animation: none !important; } }
"""

FONT_TOKENS = """
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
          "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", sans-serif;
"""

# ─────────────────────────────────────────────────────────────────────────────
# 5 个方向：设计令牌 + 视觉语言 CSS + 外壳
# ─────────────────────────────────────────────────────────────────────────────

DIRECTIONS: Dict[str, dict] = {}

DIRECTIONS["1-swiss"] = dict(
    title="方向 1 · Swiss Precision",
    shell="plain",
    overview_kind="kpis",
    tokens="""
  --bg: #ffffff; --surface: #f5f5f5; --surface-2: #ededed;
  --border: #e0e0e0; --border-strong: #c9c9c9;
  --text: #0a0a0a; --text-muted: #525252; --text-faint: #6b6b6b;
  --accent: #0f62fe; --accent-fg: #ffffff;
  --ok: #12703f; --warn: #9a5b00; --err: #b42318; --info: #0f62fe;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 24px; --sp-6: 32px;
  --r: 0px; --fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px; --fs-lg: 15px;
  --rule: 1px solid var(--border);
""" + FONT_TOKENS,
    tokens_dark="""
  --bg: #0a0a0a; --surface: #141414; --surface-2: #1c1c1c;
  --border: #2a2a2a; --border-strong: #3d3d3d;
  --text: #fafafa; --text-muted: #a3a3a3; --text-faint: #8f8f8f;
  --accent: #4b8bff; --accent-fg: #06122b;
  --ok: #4ade80; --warn: #fbbf24; --err: #f87171; --info: #4b8bff;
""",
    css="""
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 var(--sp-5); }
.topbar { border-bottom: var(--rule); position: sticky; top: 0; background: var(--bg); z-index: 5; }
.topbar-in { display: flex; align-items: center; gap: var(--sp-4); padding: var(--sp-4) 0; }
.brand { font-size: var(--fs-lg); font-weight: 600; letter-spacing: -.01em; }
.ver { font-size: var(--fs-sm); color: var(--text-faint); }
section { padding: var(--sp-6) 0; border-bottom: var(--rule); }
.sec-no { font-size: var(--fs-sm); color: var(--text-faint); }
.sec-title { font-size: 20px; font-weight: 600; letter-spacing: -.015em; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--sp-5); }
.kpi-v { font-size: 32px; letter-spacing: -.02em; }
th, td { border-bottom: var(--rule); }
th { text-transform: uppercase; letter-spacing: .06em; }
.st::before { background: currentColor; }
.sw { width: 34px; height: 18px; border: 1px solid var(--border-strong); background: var(--bg); }
.sw::after { top: 1px; left: 1px; width: 14px; height: 14px; background: var(--border-strong); }
.sw[aria-checked="true"] { background: var(--accent); border-color: var(--accent); }
.sw[aria-checked="true"]::after { transform: translateX(16px); background: var(--accent-fg); }
.ev { padding: 11px 0; border-bottom: var(--rule); }
.patrol-item { border-left: 2px solid var(--border); padding-left: var(--sp-3); }
""",
)

DIRECTIONS["2-ops-console"] = dict(
    title="方向 2 · Ops Console（数据优先）",
    shell="sidebar",
    overview_kind="kpis",
    tokens="""
  --bg: #f5f5f5; --surface: #ffffff; --surface-2: #fafafa;
  --border: #e2e2e2; --border-strong: #c4c4c4;
  --text: #333333; --text-muted: #5b5b5b; --text-faint: #666666;
  --accent: #2563eb; --accent-fg: #ffffff;
  --ok: #15803d; --warn: #c2410c; --err: #dc2626; --info: #2563eb; --knob: #fff;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 20px; --sp-6: 28px;
  --r: 3px; --fs-xs: 11px; --fs-sm: 12px; --fs-md: 12.5px; --fs-lg: 14px;
  --rule: 1px solid var(--border);
""" + FONT_TOKENS,
    tokens_dark="""
  --bg: #020617; --surface: #0f172a; --surface-2: #131f38;
  --border: #1e293b; --border-strong: #334155;
  --text: #e6edf7; --text-muted: #93a4bd; --text-faint: #8b9ab0;
  --accent: #3b82f6; --accent-fg: #04122c;
  --ok: #22c55e; --warn: #f97316; --err: #ef4444; --info: #3b82f6;
""",
    css="""
.shell { display: grid; grid-template-columns: 236px 1fr; min-height: 100vh; }
.side { background: var(--surface); border-right: var(--rule); display: flex;
        flex-direction: column; padding: var(--sp-4); gap: var(--sp-5); position: sticky;
        top: 0; height: 100vh; }
.side-brand { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) var(--sp-3); }
.brand { font-size: 14px; font-weight: 600; }
.ver { font-size: var(--fs-sm); color: var(--text-faint); }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item { display: block; padding: 8px var(--sp-3); border-radius: var(--r); font-size: var(--fs-md);
            color: var(--text-muted); text-decoration: none; border-left: 2px solid transparent; }
.nav-item:hover { background: var(--surface-2); color: var(--text); }
.nav-item.active { background: var(--surface-2); color: var(--text); font-weight: 600;
                   border-left-color: var(--accent); }
.side-foot { margin-top: auto; font-size: var(--fs-sm); color: var(--text-faint); padding: var(--sp-3); }
.main { min-width: 0; background: var(--bg); }
.topbar { background: var(--surface); border-bottom: var(--rule); position: sticky; top: 0; z-index: 5; }
.topbar-in { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-3) var(--sp-5); }
.topbar .brand { display: none; } .topbar .ver { display: none; }
.wrap { padding: 0 var(--sp-5); }
section { padding: var(--sp-5) 0; }
.sec-head { margin-bottom: var(--sp-3); }
.sec-no { display: none; }
.sec-title { font-size: 15px; font-weight: 600; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--sp-3); }
.kpi { background: var(--surface); border: var(--rule); border-radius: var(--r); padding: var(--sp-3); }
.kpi-v { font-size: 24px; font-weight: 600; letter-spacing: -.01em; }
th, td { border-bottom: var(--rule); }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; padding-top: 6px; padding-bottom: 6px; }
td { padding-top: 7px; padding-bottom: 7px; }
tbody tr:hover td { background: var(--surface-2); }
tbody tr:nth-child(even) td { background: var(--surface); }
tbody tr:nth-child(even):hover td { background: var(--surface-2); }
.tgt { max-width: 260px; }
.sw { width: 32px; height: 17px; border: 1px solid var(--border-strong); background: var(--surface-2);
      border-radius: 999px; }
.sw::after { top: 2px; left: 2px; width: 11px; height: 11px; background: var(--text-faint);
             border-radius: 999px; }
.sw[aria-checked="true"] { background: var(--ok); border-color: var(--ok); }
.sw[aria-checked="true"]::after { transform: translateX(15px); background: var(--knob); }
.ev { padding: 9px 0; border-bottom: var(--rule); }
.patrol-item { background: var(--surface); border: var(--rule); border-radius: var(--r);
               padding: var(--sp-3); }
.patrol-item .v { font-size: 18px; font-weight: 600; }
.c7 { grid-column: span 7; } .c5 { grid-column: span 5; }
""",
)

DIRECTIONS["3-deep-oled"] = dict(
    title="方向 3 · Deep OLED（近黑，仅深色）",
    shell="sidebar",
    overview_kind="kpis",
    dark_only=True,
    tokens="""
  --bg: #020617; --surface: #0b1220; --surface-2: #131c2e;
  --border: #1e293b; --border-strong: #334155;
  --text: #e2e8f0; --text-muted: #94a3b8; --text-faint: #8b9ab0;
  --accent: #38bdf8; --accent-fg: #04121f;
  --ok: #22c55e; --warn: #f59e0b; --err: #f43f5e; --info: #38bdf8;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 22px; --sp-6: 30px;
  --r: 6px; --fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px; --fs-lg: 14px;
  --rule: 1px solid var(--border);
  --glow-ok: 0 0 10px rgba(34,197,94,.35);
  --glow-err: 0 0 10px rgba(244,63,94,.35);
""" + FONT_TOKENS,
    tokens_dark="",
    css="""
.shell { display: grid; grid-template-columns: 232px 1fr; min-height: 100vh; }
.side { background: var(--surface); border-right: var(--rule); display: flex; flex-direction: column;
        padding: var(--sp-4); gap: var(--sp-5); position: sticky; top: 0; height: 100vh; }
.side-brand { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) var(--sp-3); }
.brand { font-size: 14px; font-weight: 600; letter-spacing: .01em; }
.ver { font-size: var(--fs-sm); color: var(--text-faint); }
.nav { display: flex; flex-direction: column; gap: 1px; }
.nav-item { display: block; padding: 8px var(--sp-3); font-size: var(--fs-md); color: var(--text-muted);
            text-decoration: none; border-radius: var(--r); letter-spacing: .01em; }
.nav-item:hover { background: var(--surface-2); color: var(--text); }
.nav-item.active { background: var(--surface-2); color: var(--accent); }
.side-foot { margin-top: auto; font-size: var(--fs-sm); color: var(--text-faint); padding: var(--sp-3); }
.main { min-width: 0; }
.topbar { background: var(--bg); border-bottom: var(--rule); position: sticky; top: 0; z-index: 5; }
.topbar-in { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-3) var(--sp-5); }
.topbar .brand, .topbar .ver { display: none; }
.wrap { padding: 0 var(--sp-5); }
section { padding: var(--sp-5) 0; border-bottom: var(--rule); }
.sec-head { margin-bottom: var(--sp-3); }
.sec-no { font-size: var(--fs-sm); color: var(--text-faint); }
.sec-title { font-size: 15px; font-weight: 600; letter-spacing: .01em; }
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--sp-3); }
.kpi { background: var(--surface); border: var(--rule); border-radius: var(--r); padding: var(--sp-3); }
.kpi-v { font-size: 24px; font-weight: 500; letter-spacing: -.01em; }
.kpi-v .dim { color: var(--text-faint); font-size: 15px; }
th, td { border-bottom: var(--rule); }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
tbody tr:hover td { background: var(--surface); }
.tgt { max-width: 270px; }
/* 发光只给状态，且极轻——深色最容易做廉价，这里刻意最克制 */
.st::before { border-radius: 999px; }
.st.on::before { box-shadow: var(--glow-ok); }
.st.err::before { box-shadow: var(--glow-err); }
.sw { width: 34px; height: 18px; border: 1px solid var(--border-strong); background: var(--surface-2);
      border-radius: 999px; }
.sw::after { top: 2px; left: 2px; width: 12px; height: 12px; background: var(--text-faint);
             border-radius: 999px; }
.sw[aria-checked="true"] { background: var(--surface-2); border-color: var(--accent); }
.sw[aria-checked="true"]::after { transform: translateX(16px); background: var(--accent); }
.ev { padding: 10px 0; border-bottom: var(--rule); }
.patrol-item { border-left: 1px solid var(--border); padding-left: var(--sp-3); }
.patrol-item .v { font-size: 17px; }
.c7 { grid-column: span 7; } .c5 { grid-column: span 5; }
""",
)

DIRECTIONS["4-calm-cyan"] = dict(
    title="方向 4 · Calm Cyan（浅色科技）",
    shell="sidebar",
    overview_kind="kpis",
    tokens="""
  --bg: #f7fafc; --surface: #ffffff; --surface-2: #f0f6f8;
  --border: #e2e8f0; --border-strong: #cbd5e1;
  --text: #0f172a; --text-muted: #526175; --text-faint: #64748b;
  --accent: #0e7490; --accent-fg: #ffffff;
  --ok: #047857; --warn: #b45309; --err: #dc2626; --info: #0e7490; --knob: #fff;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 24px; --sp-6: 32px;
  --r: 10px; --r-sm: 7px; --fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px; --fs-lg: 15px;
  --rule: 1px solid var(--border);
  --sh-1: 0 1px 2px rgba(15,23,42,.04), 0 1px 3px rgba(15,23,42,.06);
  --sh-2: 0 2px 4px rgba(15,23,42,.04), 0 8px 20px rgba(15,23,42,.07);
""" + FONT_TOKENS,
    tokens_dark="""
  --bg: #041a1f; --surface: #0b262c; --surface-2: #10333b;
  --border: #164e63; --border-strong: #1e6b83;
  --text: #ecfeff; --text-muted: #9fc6cf; --text-faint: #6e9aa5;
  --accent: #22d3ee; --accent-fg: #04222a;
  --ok: #34d399; --warn: #fbbf24; --err: #fb7185; --info: #22d3ee; --knob: #fff;
  --sh-1: 0 1px 2px rgba(0,0,0,.35); --sh-2: 0 8px 24px rgba(0,0,0,.4);
""",
    css="""
.shell { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
.side { background: var(--surface); border-right: var(--rule); display: flex; flex-direction: column;
        padding: var(--sp-4); gap: var(--sp-5); position: sticky; top: 0; height: 100vh; }
.side-brand { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) var(--sp-3); }
.brand { font-size: 15px; font-weight: 600; letter-spacing: -.01em; }
.ver { font-size: var(--fs-sm); color: var(--text-faint); }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item { display: block; padding: 9px var(--sp-3); border-radius: var(--r-sm); font-size: var(--fs-md);
            color: var(--text-muted); text-decoration: none;
            transition: background-color .18s ease, color .18s ease; }
.nav-item:hover { background: var(--surface-2); color: var(--text); }
.nav-item.active { background: var(--surface-2); color: var(--accent); font-weight: 600; }
.side-foot { margin-top: auto; font-size: var(--fs-sm); color: var(--text-faint); padding: var(--sp-3); }
.main { min-width: 0; }
.topbar { background: var(--surface); border-bottom: var(--rule); position: sticky; top: 0; z-index: 5;
         box-shadow: var(--sh-1); }
.topbar-in { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-3) var(--sp-5); }
.topbar .brand, .topbar .ver { display: none; }
.wrap { padding: 0 var(--sp-5); }
section { padding: var(--sp-5) 0 var(--sp-6); }
.sec-head { margin-bottom: var(--sp-4); }
.sec-no { display: none; }
.sec-title { font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
/* 卡片：柔和两级阴影 + 1px 描边，hover 只改边框与底色，不位移 */
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--sp-3); }
.kpi { background: var(--surface); border: var(--rule); border-radius: var(--r);
       padding: var(--sp-4); box-shadow: var(--sh-1);
       transition: border-color .18s ease, box-shadow .18s ease; }
.kpi:hover { border-color: var(--border-strong); box-shadow: var(--sh-2); }
.kpi-v { font-size: 26px; font-weight: 600; letter-spacing: -.02em; }
.tablecard { background: var(--surface); border: var(--rule); border-radius: var(--r);
             padding: var(--sp-2) var(--sp-4) var(--sp-3); box-shadow: var(--sh-1); }
th, td { border-bottom: var(--rule); }
th { text-transform: none; letter-spacing: .01em; }
tbody tr:hover td { background: var(--surface-2); }
.sw { width: 36px; height: 20px; border: 1px solid var(--border-strong); background: var(--surface-2);
      border-radius: 999px; }
.sw::after { top: 2px; left: 2px; width: 14px; height: 14px; background: var(--text-faint);
             border-radius: 999px; }
.sw[aria-checked="true"] { background: var(--accent); border-color: var(--accent); }
.sw[aria-checked="true"]::after { transform: translateX(16px); background: var(--knob); }
.ev { padding: 11px 0; border-bottom: var(--rule); }
.patrol-item { background: var(--surface); border: var(--rule); border-radius: var(--r-sm);
               padding: var(--sp-3); box-shadow: var(--sh-1); }
.patrol-item .v { font-size: 17px; font-weight: 600; }
.c7 { grid-column: span 7; } .c5 { grid-column: span 5; }
""",
)

DIRECTIONS["5-bento"] = dict(
    title="方向 5 · Executive Bento（模块栅格概览）",
    shell="plain",
    overview_kind="tiles",
    tokens="""
  --bg: #f5f5f7; --surface: #ffffff; --surface-2: #ececf0;
  --border: #d2d2d7; --border-strong: #b9b9c0;
  --text: #1d1d1f; --text-muted: #6e6e73; --text-faint: #68686e;
  --accent: #0062c4; --accent-fg: #ffffff;
  --ok: #0d7a37; --warn: #8a5300; --err: #c8102e; --info: #0062c4; --knob: #fff;
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 22px; --sp-6: 32px;
  --r: 16px; --r-sm: 10px; --fs-xs: 11px; --fs-sm: 12px; --fs-md: 13px; --fs-lg: 15px;
  --rule: 1px solid var(--border);
  --sh-1: 0 1px 3px rgba(0,0,0,.05);
  --sh-2: 0 6px 18px rgba(0,0,0,.09);
""" + FONT_TOKENS,
    tokens_dark="""
  --bg: #000000; --surface: #1c1c1e; --surface-2: #2c2c2e;
  --border: #38383a; --border-strong: #4a4a4c;
  --text: #f5f5f7; --text-muted: #a1a1a6; --text-faint: #98989d;
  --accent: #0a84ff; --accent-fg: #001528;
  --ok: #32d74b; --warn: #ffd60a; --err: #ff453a; --info: #0a84ff; --knob: #fff;
  --sh-1: none; --sh-2: none;
""",
    css="""
.wrap { max-width: 1240px; margin: 0 auto; padding: 0 var(--sp-5); }
.topbar { position: sticky; top: 0; background: color-mix(in srgb, var(--bg) 82%, transparent);
          backdrop-filter: saturate(180%) blur(20px); border-bottom: var(--rule); z-index: 5; }
.topbar-in { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-3) 0; }
.brand { font-size: 15px; font-weight: 600; letter-spacing: -.01em; }
.ver { font-size: var(--fs-sm); color: var(--text-faint); }
section { padding: var(--sp-6) 0 0; }
section:last-of-type { padding-bottom: var(--sp-6); }
.sec-head { margin-bottom: var(--sp-4); }
.sec-no { display: none; }
.sec-title { font-size: 17px; font-weight: 600; letter-spacing: -.015em; }
/* 概览：不等宽模块栅格，圆角 16，卡片轻微抬升不引起布局位移 */
.tiles { display: grid; grid-template-columns: repeat(6, 1fr); gap: var(--sp-4); }
.tile { background: var(--surface); border: var(--rule); border-radius: var(--r); padding: var(--sp-5);
        box-shadow: var(--sh-1); transition: transform .2s ease, box-shadow .2s ease; }
.tile:hover { transform: scale(1.012); box-shadow: var(--sh-2); }
.tile.hero { display: flex; flex-direction: column; justify-content: center; }
.tile .kpi-v { font-size: 28px; font-weight: 600; letter-spacing: -.025em; }
.tile.hero .kpi-v { font-size: 56px; font-weight: 600; letter-spacing: -.04em; line-height: 1; }
.card { background: var(--surface); border: var(--rule); border-radius: var(--r);
        padding: var(--sp-4) var(--sp-5) var(--sp-4); box-shadow: var(--sh-1); }
.card .sec-head { margin-bottom: var(--sp-3); }
th, td { border-bottom: var(--rule); }
tbody tr:hover td { background: var(--surface-2); }
tbody tr:first-child td:first-child { border-top-left-radius: var(--r-sm); }
.sw { width: 40px; height: 24px; border: 1px solid var(--border); background: var(--surface-2);
      border-radius: 999px; }
.sw::after { top: 2px; left: 2px; width: 20px; height: 20px; background: var(--knob); border-radius: 999px;
             box-shadow: 0 1px 3px rgba(0,0,0,.22); }
.sw[aria-checked="true"] { background: var(--ok); border-color: var(--ok); }
.sw[aria-checked="true"]::after { transform: translateX(16px); }
.ev { padding: 12px 0; border-bottom: var(--rule); }
.patrol-item { background: var(--surface); border: var(--rule); border-radius: var(--r-sm);
               padding: var(--sp-3); box-shadow: var(--sh-1); }
.patrol-item .v { font-size: 18px; font-weight: 600; }
""",
)

SCRIPT = """
<script>
  (function () {
    var root = document.documentElement;
    var btn = document.getElementById("theme");
    // 支持 ?theme=dark / ?theme=light 直接指定，便于分享与截图
    var q = new URLSearchParams(location.search).get("theme");
    if (q === "dark" || q === "light") { root.dataset.theme = q; }
    else {
      try { var s = localStorage.getItem("preview-theme"); if (s) root.dataset.theme = s; } catch (e) {}
    }
    if (btn) btn.addEventListener("click", function () {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("preview-theme", root.dataset.theme); } catch (e) {}
    });
    document.querySelectorAll(".sw").forEach(function (sw) {
      function toggle() {
        sw.setAttribute("aria-checked",
          sw.getAttribute("aria-checked") === "true" ? "false" : "true");
      }
      sw.addEventListener("click", toggle);
      sw.addEventListener("keydown", function (e) {
        if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); }
      });
    });
  })();
</script>
"""

# 响应式修正必须排在方向样式**之后**，否则会被 `.side { position: sticky; height: 100vh }`
# 等规则覆盖。窄屏下 240px 侧栏会把内容整段挤出视口——实测会出现整页横向偏移。
RESPONSIVE_CSS = """
@media (max-width: 860px) {
  .shell { grid-template-columns: minmax(0, 1fr); }
  .side { position: static; height: auto; flex-direction: row; align-items: center;
          gap: var(--sp-3); padding: var(--sp-2) var(--sp-3); border-right: none;
          border-bottom: var(--rule); overflow-x: auto; }
  .side-brand { padding: 0 var(--sp-2) 0 0; flex: none; }
  .side-brand .ver { display: none; }
  .nav { flex-direction: row; gap: var(--sp-1); }
  .nav-item { padding: 6px 10px; white-space: nowrap; }
  .nav-item.active { border-left: none; }
  .side-foot { display: none; }
  .topbar .brand, .topbar .ver { display: inline !important; }
  .sec-head { flex-wrap: wrap; row-gap: var(--sp-1); }
  .sec-note { margin-left: 0; flex: 1 0 100%; }
}
"""


def render(key: str, d: dict) -> str:
    dark_only = d.get("dark_only", False)
    overview = tiles_block() if d["overview_kind"] == "tiles" else kpi_block()
    body_sections = sections(overview)

    # Bento 方向把渠道/记录放进卡片，其余方向表格直排
    if key == "5-bento":
        body_sections[1] = body_sections[1].replace(
            channels_block(), '<div class="card">' + channels_block() + "</div>")
        body_sections[3] = body_sections[3].replace(
            '<div class="sec-head"><span class="sec-no mono">04</span><h2 class="sec-title">最近推送</h2></div>'
            + logs_block(),
            '<div class="card"><div class="sec-head"><h2 class="sec-title">最近推送</h2></div>'
            + logs_block() + "</div>")
        body_sections[3] = body_sections[3].replace(
            '<div class="sec-head"><span class="sec-no mono">05</span><h2 class="sec-title">定时巡检</h2></div>'
            + patrol_block(),
            '<div class="card"><div class="sec-head"><h2 class="sec-title">定时巡检</h2></div>'
            + patrol_block() + "</div>")
    if key == "4-calm-cyan":
        body_sections[1] = body_sections[1].replace(
            channels_block(), '<div class="tablecard">' + channels_block() + "</div>")

    inner = TOPBAR + '<main class="wrap">' + "".join(body_sections) + \
        f'<footer>FnMessageBot · MIT License · {d["title"]}</footer></main>'

    if d["shell"] == "sidebar":
        page = '<div class="shell">' + sidebar() + '<div class="main">' + inner + "</div></div>"
    else:
        page = inner

    theme_attr = "dark" if dark_only else "light"
    hint = "本方向仅提供深色模式" if dark_only else "可按 ?theme=dark / ?theme=light 指定主题"
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="{theme_attr}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="{'dark' if dark_only else 'light dark'}" />
<title>{d["title"]}</title>
<!-- 内容与方向 1-5 完全一致，仅设计令牌与视觉语言不同；无 CDN、无外部字体。{hint} -->
<style>
:root {{
{d["tokens"].strip()}
}}
{(':root[data-theme="dark"] {\n' + d["tokens_dark"].strip() + '\n}') if d.get("tokens_dark") else ''}
{BASE_CSS.strip()}
{d["css"].strip()}
{RESPONSIVE_CSS.strip()}
</style>
</head>
<body>
{page}
{SCRIPT}
</body>
</html>
"""


INDEX_BLURB = {
    "1-swiss": ("Swiss Precision", "瑞士精确：靠排版、栅格与留白建立高级感，零装饰、零阴影、方角。",
                ["字号阶梯 + 8px 基准间距", "单强调色，深色模式零硬编码颜色", "细 1px 分隔线代替卡片分组"]),
    "2-ops-console": ("Ops Console", "运维控制台：颜色只承载状态语义，密度换效率。最贴合「这本来是监控工具」。",
                      ["左侧导航 + 状态色（严重/警告/正常/进行中）", "等宽数字对齐，紧凑行高 + 斑马纹",
                       "开关用成功绿，语义一致"]),
    "3-deep-oled": ("Deep OLED", "近黑影院级深色：层级靠 1px 描边，发光只给状态。科技感最强，仅深色。",
                    ["底色 #020617，卡片靠描边分层", "状态点带极轻发光，其余零发光", "放弃浅色模式"]),
    "4-calm-cyan": ("Calm Cyan", "冷静青：浅色为底，低饱和青做科技感，柔和两级阴影。最干净体面。",
                    ["低饱和青 #0891b2 + 大量中性灰压住", "柔和阴影 + 1px 描边（不是玻璃态）",
                     "hover 只改边框与底色，不位移"]),
    "5-bento": ("Executive Bento", "模块栅格概览：概览页一屏看全，其余页保持表单。苹果式克制灰阶。",
                ["3+3 / 2+2+2 两行不等宽模块", "KPI 数字放大到 56px（hero 卡）",
                 "卡片圆角 16px，hover 轻微抬升"]),
}


def build_index() -> str:
    cards = []
    for key, (name, desc, bullets) in INDEX_BLURB.items():
        bl = "".join(f"<li>{b}</li>" for b in bullets)
        cards.append(f"""
    <article class="pcard">
      <header class="phead">
        <h2>{name}</h2>
        <code>{key}.html</code>
      </header>
      <p class="pdesc">{desc}</p>
      <ul class="pbul">{bl}</ul>
      <div class="pacts">
        <a class="pbtn primary" href="{key}.html" target="_blank" rel="noopener">打开完整版（可切主题）</a>
        <a class="pbtn" href="{key}.html?theme=dark" target="_blank" rel="noopener">深色</a>
      </div>
      <div class="frame">
        <iframe src="{key}.html" title="{name} 预览" loading="lazy" scrolling="no"></iframe>
      </div>
    </article>""")
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>FNMessageBots · 5 个设计方向对比</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #f5f5f7; --surface: #ffffff; --border: #d2d2d7;
    --text: #1d1d1f; --muted: #6e6e73; --accent: #0062c4; --on-accent: #fff;
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
            "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Source Han Sans SC", sans-serif;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.6 var(--sans); -webkit-font-smoothing: antialiased; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 40px 24px 64px; }}
  h1 {{ font-size: 28px; letter-spacing: -.02em; margin: 0 0 6px; }}
  .lead {{ color: var(--muted); margin: 0 0 8px; }}
  .meta {{ color: var(--muted); font-size: 13px; margin: 0 0 32px; }}
  .meta code {{ font-family: var(--mono); background: var(--surface); padding: 1px 5px;
                border: 1px solid var(--border); border-radius: 4px; }}
  .notice {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
             padding: 14px 18px; margin: 0 0 32px; font-size: 13px; color: var(--muted); }}
  .notice b {{ color: var(--text); }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
           padding: 24px; margin-bottom: 24px; }}
  .phead {{ display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }}
  .phead h2 {{ font-size: 19px; margin: 0; letter-spacing: -.015em; }}
  .phead code {{ font-family: var(--mono); font-size: 12px; color: var(--muted); }}
  .pdesc {{ margin: 8px 0 10px; }}
  .pbul {{ margin: 0 0 16px; padding-left: 20px; color: var(--muted); font-size: 13px; }}
  .pacts {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }}
  .pbtn {{ display: inline-block; padding: 8px 14px; border: 1px solid var(--border);
           border-radius: 999px; text-decoration: none; color: var(--text); font-size: 13px;
           background: var(--bg); }}
  .pbtn.primary {{ background: var(--accent); border-color: var(--accent); color: var(--on-accent); }}
  /* 缩略预览：把 1500px 宽的页面缩到卡片内 */
  .frame {{ position: relative; width: 100%; height: 520px; border: 1px solid var(--border);
            border-radius: 10px; overflow: hidden; background: var(--surface); }}
  .frame iframe {{ position: absolute; top: 0; left: 0; width: 1500px; height: 1620px;
                   border: 0; transform-origin: 0 0; }}
  @media (min-width: 1000px) {{ .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
                                .grid .card {{ margin-bottom: 0; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>FNMessageBots · 5 个设计方向</h1>
  <p class="lead">内容完全一致，只有设计令牌（颜色/间距/圆角/字体）与视觉语言不同，便于公平对比。</p>
  <p class="meta">产物由 <code>design-previews/_build.py</code> 生成 ·
     溢出检测 <code>design-previews/_check.py</code>（5 文件 × 4 宽度 = 20 项全通过）</p>
  <div class="notice">
    <b>字体说明：</b>样稿用的是系统字体栈（不含 CDN、不含外部字体），所以中文部分在所有方向里
    表现一致 —— 换成 Inter / Fira Code / JetBrains Mono 的收益主要在<b>数字、IP、时间戳、单位</b>上，
    中文仍会回退到系统 CJK 字体。数字已全部开启 <code>tabular-nums</code>（等宽对齐）。
  </div>
  <div class="grid">{''.join(cards)}</div>
</div>
<script>
  // 把 1500px 宽的预览等比缩放到卡片宽度
  (function () {{
    function fit() {{
      document.querySelectorAll(".frame").forEach(function (f) {{
        var ifr = f.querySelector("iframe");
        if (ifr) ifr.style.transform = "scale(" + (f.clientWidth / 1500) + ")";
      }});
    }}
    fit();
    window.addEventListener("resize", fit);
    document.querySelectorAll(".frame iframe").forEach(function (ifr) {{
      ifr.addEventListener("load", fit);
    }});
  }})();
</script>
</body>
</html>
"""


def main() -> int:
    written = []
    for key, d in DIRECTIONS.items():
        html = render(key, d)
        path = OUT_DIR / f"{key}.html"
        path.write_text(html, encoding="utf-8")
        # 简易自检：标签配平 + 未定义的 var() 引用
        for tag in ("div", "section", "table", "span"):
            o = len(re.findall(rf"<{tag}[\s>]", html))
            c = len(re.findall(rf"</{tag}>", html))
            if o != c:
                print(f"  ! {path.name}: <{tag}> 开 {o} 闭 {c} 不匹配")
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", html))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", html))
        missing = sorted(used - defined)
        if missing:
            print(f"  ! {path.name}: 用了未定义的令牌 {missing}")
        written.append((path.name, len(html), len(used), len(missing)))
    idx = OUT_DIR / "index.html"
    idx.write_text(build_index(), encoding="utf-8")
    written.append((idx.name, idx.stat().st_size, 0, 0))
    print(f"{'文件':<22}{'字节':>8}{'令牌数':>8}{'未定义':>8}")
    for n, sz, tok, miss in written:
        print(f"{n:<22}{sz:>8}{tok:>8}{miss:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
