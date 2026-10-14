#!/usr/bin/env python3
"""把三个模板里的内联 CSS 提取成 assets/css/app.css，并把颜色接到设计令牌上。

为什么用脚本而不是手抄：现有内联 CSS 合计 34.6 KB / 约 310 条选择器，
手抄必然漏项和打错字；而脚本可以做**选择器齐平校验**——逐条确认
「原来有的选择器，输出里都有对应规则」，这是「没丢样式」唯一可机械验证的证据。

映射表是显式声明的（见 COLOR_MAP / RGBA_MAP），不是脚本猜的。其中需要判断的地方：
- 现有 6 级灰字（#111827/#1f2937/#374151/#4b5563/#6b7280/#9ca3af）收敛到 3 级
  （text / muted / faint）。这是**可见的层级收紧**，不是等价变换。
- faq 页原本用淡紫底 #eef3ff，与 index 的灰底不一致；一并归到同一套令牌。
- 深色覆盖块整体删除：颜色全部变成令牌后，深色由令牌层负责，无需逐选择器覆盖。

跨文件的同名选择器：声明相同的合并为一条；不同的按 [data-page="..."] 限定，
因此需在三个模板的 <html> 上各加一个 data-page 属性（不改 class/id，不影响 JS）。

用法（项目根目录）
    python design-previews/_extract_css.py            # 生成
    python design-previews/_extract_css.py --check    # 只校验选择器齐平
"""

from __future__ import annotations

import argparse
import collections
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "src" / "web" / "templates"
_OUT = _ROOT / "assets" / "css" / "app.css"

PAGES = {"index.html": "index", "history.html": "history", "faq.html": "faq"}

# ── 颜色映射：现有 46 种去重颜色 → 令牌 ─────────────────────────────────────
COLOR_MAP: Dict[str, str] = {
    # 正文：原本 4 级灰收敛为 1 级。层级改由字号/字重承担
    "#111827": "var(--text)",
    "#1f2937": "var(--text)",
    "#1f2933": "var(--text)",
    "#0f172a": "var(--text)",
    "#374151": "var(--text)",
    # 次要文字
    "#4b5563": "var(--text-muted)",
    "#6b7280": "var(--text-muted)",
    # 弱化文字：原本 #9ca3af 在浅底上只有约 2.5:1，映射后对比度显著提升
    "#9ca3af": "var(--text-faint)",
    # 面
    "#fff": "var(--surface)",
    "#ffffff": "var(--surface)",
    "#f9fafb": "var(--bg)",
    "#fafafa": "var(--bg)",
    "#eef3ff": "var(--bg)",
    "#f3f4f6": "var(--surface-2)",
    # 边框
    "#e5e7eb": "var(--border)",
    "#d1d5db": "var(--border-strong)",
    # 主色（原蓝 → Calm Cyan 青）
    "#2563eb": "var(--accent)",
    "#1d4ed8": "var(--accent-hover)",
    "#1e40af": "var(--accent-hover)",
    "#0369a1": "var(--accent-text)",
    "#eff6ff": "var(--accent-soft)",
    "#e0e7ff": "var(--accent-soft)",
    "#e0f2fe": "var(--accent-soft)",
    "#bae6fd": "var(--accent-soft-border)",
    "#93c5fd": "var(--accent-soft-border)",
    "#bfdbfe": "var(--accent-soft-border)",
    # 成功
    "#059669": "var(--ok)",
    "#22c55e": "var(--ok)",
    "#166534": "var(--ok)",
    "#dcfce7": "var(--ok-soft)",
    # 警告
    "#b45309": "var(--warn)",
    "#fde68a": "var(--warn-soft)",
    "#fef3c7": "var(--warn-soft)",
    # 失败
    "#dc2626": "var(--err)",
    "#b91c1c": "var(--err)",
    "#991b1b": "var(--err)",
    "#f87171": "var(--err)",
    "#fecaca": "var(--err-soft)",
    "#fee2e2": "var(--err-soft)",
    "#fef2f2": "var(--err-soft)",
    # 其他
    "#1e3a5f": "var(--text)",
}

# rgba() 按语义映射：阴影走 --sh-color-*，焦点环走 --ring，半透明面单独给令牌。
# 保留形状、只抽颜色，是为了不擅自改变每条规则的阴影几何。
RGBA_MAP: Dict[str, str] = {
    # 焦点环
    "rgba(37,99,235,0.35)": "var(--ring)",
    "rgba(37, 99, 235, 0.35)": "var(--ring)",
    "rgba(37,99,235,0.28)": "var(--ring-weak)",
    "rgba(37, 99, 235, 0.28)": "var(--ring-weak)",
    "rgba(37,99,235,0.3)": "var(--ring-weak)",
    "rgba(37, 99, 235, 0.3)": "var(--ring-weak)",
    "rgba(37,99,235,0.25)": "var(--ring-weak)",
    # 阴影颜色
    "rgba(15,23,42,0.18)": "var(--sh-color-3)",
    "rgba(15, 23, 42, 0.18)": "var(--sh-color-3)",
    "rgba(15, 23, 42, 0.08)": "var(--sh-color-2)",
    "rgba(15, 23, 42, 0.06)": "var(--sh-color-1)",
    "rgba(0,0,0,0.18)": "var(--sh-color-hard)",
    "rgba(0, 0, 0, 0.18)": "var(--sh-color-hard)",
    "rgba(0, 0, 0, 0.2)": "var(--sh-color-hard)",
    "rgba(0, 0, 0, 0.08)": "var(--sh-color-1)",
    "rgba(34, 197, 94, 0.35)": "var(--sh-color-ok)",
    # 半透明面与边框
    "rgba(255,255,255,0.95)": "var(--surface-translucent)",
    "rgba(255, 255, 255, 0.95)": "var(--surface-translucent)",
    "rgba(255, 255, 255, 0.97)": "var(--surface-translucent-strong)",
    "rgba(255, 255, 255, 0.9)": "var(--surface-translucent)",
    "rgba(255,255,255,0.9)": "var(--surface-translucent)",
    "rgba(148,163,184,0.18)": "var(--border-translucent)",
    "rgba(148, 163, 184, 0.18)": "var(--border-translucent)",
    "rgba(148,163,184,0.32)": "var(--border-translucent)",
    "rgba(148, 163, 184, 0.32)": "var(--border-translucent)",
    "rgba(148, 163, 184, 0.35)": "var(--border-translucent)",
    "rgba(148, 163, 184, 0.45)": "var(--border-translucent)",
    "rgba(75, 85, 99, 0.55)": "var(--border-translucent)",
    "rgba(17, 24, 39, 0.92)": "var(--surface-translucent)",
    "rgba(37, 99, 235, 0.18)": "var(--accent-soft-border)",
    "rgba(37, 99, 235, 0.12)": "var(--accent-soft)",
    # 完全透明用于渐变收尾，与主题无关
    "rgba(255, 255, 255, 0)": "rgba(255, 255, 255, 0)",
    "rgba(255,255,255,0)": "rgba(255, 255, 255, 0)",
}

# faq 页原本自建了一套 --faq-* 变量（底色还是淡紫 #eef3ff，与其余页面不一致）。
# 直接映射到共享令牌，并删掉别名定义本身——否则 app.css 里会留一个多余的 :root 块。
ALIAS_MAP: Dict[str, str] = {
    "--faq-bg": "var(--bg)",
    "--faq-bg-accent": "var(--surface-2)",
    "--faq-card": "var(--surface)",
    "--faq-panel": "var(--surface)",
    "--faq-panel-border": "var(--border)",
    "--faq-text": "var(--text)",
    "--faq-muted": "var(--text-muted)",
    "--faq-faint": "var(--text-faint)",
    "--faq-accent": "var(--accent)",
    "--faq-border": "var(--border-translucent)",
    # 这两个原本是整个 box-shadow 值，映射到整套阴影令牌
    "--faq-shadow": "var(--sh-2)",
    "--faq-shadow-card": "var(--sh-1)",
}

# 只定义别名的规则整条删除（:root / html[data-theme="dark"] 里的 --faq-* 定义）
ALIAS_ONLY_SELECTORS = {":root", 'html[data-theme="dark"]'}


def _map_aliases(text: str) -> str:
    for k, v in ALIAS_MAP.items():
        text = text.replace(f"var({k})", v)
    return text


def _is_alias_only_def(sel: str, body: str) -> bool:
    """该规则是否只是 --faq-* 别名的定义（除别名外无其他声明）。"""
    if sel not in ALIAS_ONLY_SELECTORS:
        return False
    decls = re.findall(r"(--[A-Za-z0-9-]+)\s*:", body)
    if not decls:
        return False
    non_alias = [d for d in decls if d not in ALIAS_MAP]
    return not non_alias and any(d in ALIAS_MAP for d in decls)


# 需要按页面限定的选择器（同名但定义确实不同，不能强行合并）
PER_PAGE_CONFLICTS = {".page", ".card", ".license-footer"}

# 深色覆盖块整块删除：颜色进令牌后由令牌层负责
DROP_SELECTOR_PREFIXES = ('html[data-theme="dark"]', '[data-theme="dark"]')


def _style_blocks(html: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))


def _parse(css: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str, str]]]:
    """返回 (普通规则[(选择器, 声明块)], 媒体规则[(媒体条件, 选择器, 声明块)])。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    media: List[Tuple[str, str, str]] = []
    plain: List[Tuple[str, str]] = []

    def take_media(m: re.Match) -> str:
        cond = " ".join(m.group(1).replace("@media", "").split())
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", m.group(2)):
            media.append((cond, " ".join(sel.split()), body.strip()))
        return ""

    rest = re.sub(r"(@media[^{]*)\{((?:[^{}]|\{[^{}]*\})*)\}", take_media, css)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", rest):
        s = " ".join(sel.split())
        if s:
            plain.append((s, body.strip()))
    return plain, media


def _norm_decls(body: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in re.findall(r"([a-zA-Z-]+)\s*:\s*([^;]+)", body):
        out[k.strip().lower()] = " ".join(v.split())
    return out


def _map_colors(text: str, unresolved: collections.Counter) -> str:
    def sub_rgba(m: re.Match) -> str:
        raw = " ".join(m.group(0).split())
        if raw in RGBA_MAP:
            return RGBA_MAP[raw]
        unresolved[f"rgba:{raw}"] += 1
        return raw

    text = re.sub(r"rgba?\([^)]*\)", sub_rgba, text)

    def sub_hex(m: re.Match) -> str:
        raw = m.group(0).lower()
        if raw in COLOR_MAP:
            return COLOR_MAP[raw]
        unresolved[f"hex:{raw}"] += 1
        return raw

    return re.sub(r"#[0-9a-fA-F]{3,8}\b", sub_hex, text)


def _is_dark(sel: str) -> bool:
    return any(sel.startswith(p) or f'html[data-theme="dark"]' in sel
               for p in DROP_SELECTOR_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验选择器齐平，不写文件")
    args = ap.parse_args()

    per_page: Dict[str, List[Tuple[str, str]]] = {}
    per_media: Dict[str, List[Tuple[str, str, str]]] = {}
    original_sels: Dict[str, set] = {}
    dropped_dark: List[str] = []
    dropped_alias: List[str] = []

    for fname, page in PAGES.items():
        css = _style_blocks((_TEMPLATES / fname).read_text(encoding="utf-8"))
        plain, media = _parse(css)
        keep = [(s, b) for s, b in plain if not _is_dark(s)]
        dropped_dark += [s for s, _ in plain if _is_dark(s)]
        # 原地去掉「只定义别名」的规则
        alias_only = [s for s, b in keep if _is_alias_only_def(s, b)]
        keep = [(s, b) for s, b in keep if not _is_alias_only_def(s, b)]
        per_page[page] = [(_map_aliases(s), _map_aliases(b)) for s, b in keep]
        per_media[page] = [(_map_aliases(c), _map_aliases(s), _map_aliases(b))
                           for c, s, b in media if not _is_dark(s)]
        original_sels[page] = {s for s, _ in keep}
        if alias_only:
            dropped_alias.extend(f"{page}:{s}" for s in alias_only)

    # 选择器齐平：原选择器必须出现在输出中（含按页限定后的形式）
    emitted: Dict[str, set] = collections.defaultdict(set)
    for page, rules in per_page.items():
        for sel, _b in rules:
            emitted[page].add(sel)

    missing = {p: sorted(original_sels[p] - emitted[p]) for p in PAGES.values()}
    unresolved: collections.Counter = collections.Counter()

    # 组织输出：出现于多个页面且声明一致的 → 通用；否则按页
    sel_pages: Dict[str, List[str]] = collections.defaultdict(list)
    sel_body: Dict[Tuple[str, str], str] = {}
    for page, rules in per_page.items():
        for sel, body in rules:
            sel_pages[sel].append(page)
            sel_body[(page, sel)] = body

    common: List[Tuple[str, str]] = []
    for sel, pages in sel_pages.items():
        if len(pages) < 2:
            continue
        # 声明块要先转成可比较的规范形式（dict 不可哈希，用排序后的元组）
        shapes = {tuple(sorted(_norm_decls(sel_body[(p, sel)]).items())) for p in pages}
        if len(shapes) == 1 and sel not in PER_PAGE_CONFLICTS:
            common.append((sel, sel_body[(pages[0], sel)]))
    common_set = {s for s, _ in common}

    out: List[str] = []
    out.append("/* ============================================================================")
    out.append("   FnMessageBot · 组件样式")
    out.append("   ----------------------------------------------------------------------------")
    out.append("   由 design-previews/_extract_css.py 从三个模板的内联 <style> 提取生成。")
    out.append("   颜色一律走 assets/css/tokens.css 的令牌，本文件内不得出现硬编码颜色——")
    out.append("   硬编码一处，深色模式就会漏掉那一处。")
    out.append("")
    out.append("   原内联样式的配色是蓝(#2563eb)+灰，已按 Calm Cyan 方向映射到令牌；")
    out.append("   原有 6 级灰字收敛为 3 级（text/muted/faint），层级改由字号与字重承担。")
    out.append("   ========================================================================== */")
    out.append("")

    # ── 通用层 ──
    out.append("/* ── 通用：多个页面共用的规则 ───────────────────────────────────────────── */")
    for sel, body in common:
        out.append(f"{sel} {{ {_map_colors(body, unresolved)} }}")
    out.append("")

    # ── 页面层 ──
    for page in PAGES.values():
        out.append(f"/* ── 页面：{page} ────────────────────────────────────────────────────────── */")
        seen = set()
        for sel, body in per_page[page]:
            if sel in common_set and sel not in PER_PAGE_CONFLICTS:
                continue
            if (sel, page) in seen:
                continue
            seen.add((sel, page))
            prefix = f'[data-page="{page}"] ' if sel in PER_PAGE_CONFLICTS else ""
            out.append(f"{prefix}{sel} {{ {_map_colors(body, unresolved)} }}")
        mq = per_media[page]
        if mq:
            out.append("")
            for cond, sel, body in mq:
                prefix = f'[data-page="{page}"] ' if sel in PER_PAGE_CONFLICTS else ""
                out.append(f"@media {cond} {{ {prefix}{sel} {{ {_map_colors(body, unresolved)} }} }}")
        out.append("")

    if args.check:
        print("=== 选择器齐平 ===")
        for p, miss in missing.items():
            print(f"  {p}: 原 {len(original_sels[p])} 条，缺失 {len(miss)} 条 {miss[:5]}")
        print(f"  删除的深色覆盖规则: {len(dropped_dark)} 条")
        return 1 if any(missing.values()) else 0

    _OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"已写出 {_OUT.relative_to(_ROOT)}")
    print(f"  通用规则 {len(common)} 条")
    for page in PAGES.values():
        print(f"  {page:<8} 规则 {len(per_page[page])} 条，媒体查询 {len(per_media[page])} 条")
    print(f"  删除深色覆盖 {len(dropped_dark)} 条")
    if unresolved:
        print()
        print("!! 未映射的颜色（需补 COLOR_MAP / RGBA_MAP 或新增令牌）:")
        for k, n in unresolved.most_common(20):
            print(f"     {k}  ×{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
