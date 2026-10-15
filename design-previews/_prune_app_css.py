#!/usr/bin/env python3
"""一次性清理 app.css：删除已失效的规则（records 合并入概览、勾选框改开关之后）。

为什么可以安全删除：
1) 所有 [data-page="history"] 规则 —— history.html 已删除，全站再没有任何页面
   带 data-page="history"，这些选择器**不可能再匹配**，属于可证明的死代码。
   其中仍需要的部分（详情面板、成功/失败文字色）已按令牌重写进 layout.css。
2) 被新结构替换掉的 index 规则 —— 开关改为通用 .switch 组件、事件改为
   一类型一卡片一行一项、通知策略改为 setting-row，对应的旧类名不再出现。

用法（项目根目录）
    python design-previews/_prune_app_css.py [--check]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP_CSS = _ROOT / "assets" / "css" / "app.css"

# 被新结构取代、已无模板/脚本引用的 index 规则（精确按选择器匹配，不用前缀猜测）
DEAD_INDEX_SELECTORS = {
    '[data-page="index"] .channel-switch',
    '[data-page="index"] .channel-switch input',
    '[data-page="index"] .channel-switch-slider',
    '[data-page="index"] .channel-switch-slider::before',
    '[data-page="index"] .channel-switch input:checked + .channel-switch-slider',
    '[data-page="index"] .channel-switch input:checked + .channel-switch-slider::before',
    '[data-page="index"] .channel-switch input:focus-visible + .channel-switch-slider',
    '[data-page="index"] .events-grid',
    '[data-page="index"] .event-category.collapsed .events-grid',
    '[data-page="index"] .event-category-title',
    '[data-page="index"] .event-cat-collapse',
    '[data-page="index"] .event-item',
    '[data-page="index"] .event-item:hover',
    '[data-page="index"] .event-item input',
    '[data-page="index"] .event-item span',
    '[data-page="index"] .event-item .field-helper',
    '[data-page="index"] .test-section',
    '[data-page="index"] .system-grid',
}

HISTORY_MARK = '[data-page="history"]'


def _split_rules(css: str):
    """把顶层内容切成 (前导注释/空白, 选择器, 完整块) 序列；@media 原样保留其文本。"""
    out = []
    i = 0
    n = len(css)
    while i < n:
        probe = i
        while probe < n and css[probe] in " \t\r\n":
            probe += 1
        if css.startswith("/*", probe):
            end = css.find("*/", probe + 2)
            end = n if end == -1 else end + 2
            out.append(("comment", css[i:end]))
            i = end
            continue
        brace = css.find("{", i)
        if brace == -1:
            out.append(("tail", css[i:]))
            break
        sel = css[i:brace]
        depth = 0
        j = brace
        while j < n:
            if css.startswith("/*", j):
                e = css.find("*/", j + 2)
                j = n if e == -1 else e + 2
                continue
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(("rule", sel, css[brace:j + 1]))
        i = j + 1
    return out


def prune(css: str) -> tuple[str, dict]:
    stats = {"history": 0, "dead_index": 0, "media_trimmed": 0, "media_dropped": 0}
    kept = []
    for item in _split_rules(css):
        if item[0] != "rule":
            kept.append("".join(item[1:]))
            continue
        _, sel_raw, body = item
        sel = " ".join(sel_raw.split())

        if sel.startswith("@media"):
            inner = body[body.find("{") + 1:body.rfind("}")]
            pieces = []
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", inner):
                s = " ".join(m.group(1).split())
                if HISTORY_MARK in s:
                    stats["media_trimmed"] += 1
                    continue
                pieces.append(f"{s} {{{m.group(2)}}}")
            if not pieces:
                stats["media_dropped"] += 1
                continue
            # sel_raw 已经包含 "@media (…)" 这段前导（它就是从上一个 '}' 到 '{' 之间的文本），
            # 早期版本又拼了一次 head，生成 "@media (…) @media (…) { … }" 这种无效结构，
            # 浏览器会整块丢弃——所以这里只补 " {" 与 "}"。
            kept.append(f"{sel_raw.rstrip()} {{ " + "\n  ".join(pieces) + " }\n")
            continue

        if HISTORY_MARK in sel:
            stats["history"] += 1
            continue
        if sel in DEAD_INDEX_SELECTORS:
            stats["dead_index"] += 1
            continue
        kept.append(f"{sel_raw}{body}\n")

    text = "".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    css = _APP_CSS.read_text(encoding="utf-8")
    new, stats = prune(css)

    print(f"删除 [data-page=\"history\"] 规则: {stats['history']}")
    print(f"删除已失效的 index 规则: {stats['dead_index']}")
    print(f"媒体查询内删除: {stats['media_trimmed']}，整块移除: {stats['media_dropped']}")
    print(f"字节 {len(css)} → {len(new)}  规则 {css.count('{')} → {new.count('{')}")
    print(f"花括号配平: {new.count('{') == new.count('}')}")
    left = new.count(HISTORY_MARK)
    print(f"残留 history 选择器: {left}")
    # 防回归：媒体查询头部被重复拼接会生成 "@media (…) @media (…) { … }"，
    # 语法无效、浏览器整块丢弃，样式静默失效，必须拦住。
    dup_media = re.findall(r"@media[^{@]*@media", new)
    print(f"重复的 @media 头部: {len(dup_media)}")

    if args.check:
        return 0
    if new.count("{") != new.count("}") or left or dup_media:
        print("校验未通过，拒绝写入")
        return 3
    _APP_CSS.write_text(new, encoding="utf-8")
    print(f"已写回 {_APP_CSS.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
