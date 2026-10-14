#!/usr/bin/env python3
"""一次性修复 assets/css/app.css 的两个提取缺陷（现已转为手工维护，此脚本留作记录）。

背景：app.css 由 _extract_css.py 从三个模板的内联 <style> 一次性提取。
提取器有两处做得不对，是在做移动端适配时才暴露出来的：

缺陷 1｜页面专属规则没有加作用域，元素选择器泄漏到全站
  原 history.html 里有 `table { min-width: 760px }`、`h1`、`th, td`、`tbody td`、`pre`，
  faq.html 里有 `a`、`code`、`body`。它们在各自模板的 <style> 里只作用于本页，
  合进共享文件后就变成了全局元素选择器。
  后果（实测）：配置页的渠道表格被 `table{min-width:760px}` 撑到 760px，
  窄屏下必须横向滚动才能点到开关与删除——正是本次要修的移动端问题。
  修法：段内每条规则的选择器加 [data-page="<页>"] 前缀。

缺陷 2｜@keyframes 被拆散
  解析器只认 @media，遇到 `@keyframes toast-in { from {...} to {...} }` 时
  把外壳丢掉、把 from/to 当成普通规则输出。于是动画定义消失，
  而 .toast 仍写着 `animation: toast-in ...`——提示浮层不再有入场动画。
  修法：删掉那两个孤立规则，按原文补回完整的 @keyframes。

用法（项目根目录）
    python design-previews/_scope_app_css.py [--check]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP_CSS = _ROOT / "assets" / "css" / "app.css"

KEYFRAMES = """@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(-12px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
"""

# 段标题：/* ── 页面：index ── ... */
_SECTION_RE = re.compile(r"/\*\s*[^\n]*页面：(\w+)[^\n]*\*/")
_AT_RULES_TO_KEEP = ("@keyframes", "@font-face", "@supports", "@media", "@charset", "@import")


def _scope_selector(sel: str, page: str) -> str:
    """给选择器列表里的每一项加 [data-page] 前缀（已加过则跳过）。"""
    parts = [p.strip() for p in sel.split(",") if p.strip()]
    out = []
    for p in parts:
        if p.startswith("[data-page=") or p.startswith(":root"):
            out.append(p)
        else:
            out.append(f'[data-page="{page}"] {p}')
    return ", ".join(out)


def _split_leading_comment(text: str) -> tuple[str, str]:
    """把开头的注释与空白剥出来（保留原样，只对规则主体做处理）。"""
    m = re.match(r"^(\s*(?:/\*.*?\*/\s*)*)(.*)$", text, re.S)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def transform(css: str) -> tuple[str, dict]:
    stats = {"scoped": 0, "kept": 0, "dropped_keyframe_parts": 0}
    out: list[str] = []
    i = 0
    n = len(css)
    page = ""
    in_comment = False

    while i < n:
        # 规则之间会有空白与换行；注释检测必须先跳过空白，
        # 否则段标题注释识别不到，整段规则都不会被加作用域。
        probe = i
        while probe < n and css[probe] in " \t\r\n":
            probe += 1
        if css.startswith("/*", probe):
            end = css.find("*/", probe + 2)
            end = n if end == -1 else end + 2
            chunk = css[i:end]
            m = _SECTION_RE.search(chunk)
            if m:
                page = m.group(1)
            out.append(chunk)
            i = end
            continue

        # 找下一个 '{' 之前的选择器文本
        brace = css.find("{", i)
        if brace == -1:
            out.append(css[i:])
            break
        selector_raw = css[i:brace]
        lead, sel = _split_leading_comment(selector_raw)
        sel_stripped = " ".join(sel.split())

        # 找到匹配的 '}'
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
        body = css[brace:j + 1]

        if not sel_stripped:
            out.append(lead + body)
            i = j + 1
            continue

        at = next((a for a in _AT_RULES_TO_KEEP if sel_stripped.startswith(a)), None)

        if at == "@media":
            # 媒体查询：递归处理内部规则
            inner = body[body.find("{") + 1:body.rfind("}")]
            inner_out, sub = transform_inner(inner, page)
            stats["scoped"] += sub["scoped"]
            stats["dropped_keyframe_parts"] += sub["dropped_keyframe_parts"]
            out.append(f"{lead}{sel_stripped} {{ {inner_out.strip()} }}\n")
            i = j + 1
            continue

        if at:
            stats["kept"] += 1
            # 必须带上选择器/前导（@keyframes foo、@font-face 等），
            # body 只是花括号部分——早期版本这里写成 lead + body，把前导整段丢了。
            out.append(f"{lead}{sel_stripped} {body}\n")
            i = j + 1
            continue

        # 缺陷 2：孤立的 from / to（@keyframes 被拆散的残留）
        if sel_stripped in ("from", "to"):
            stats["dropped_keyframe_parts"] += 1
            i = j + 1
            continue

        if page:
            out.append(f"{lead}{_scope_selector(sel_stripped, page)} {body}\n")
            stats["scoped"] += 1
        else:
            # 通用段：不加作用域，但选择器**必须保留**
            out.append(f"{lead}{sel_stripped} {body}\n")
        i = j + 1

    return "".join(out), stats


def transform_inner(inner: str, page: str) -> tuple[str, dict]:
    """处理 @media 内部：把每条规则的选择器加上作用域（若需要）。"""
    stats = {"scoped": 0, "dropped_keyframe_parts": 0}
    if not page:
        return inner, stats
    pieces = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", inner):
        sel = " ".join(m.group(1).split())
        if not sel or sel in ("from", "to"):
            stats["dropped_keyframe_parts"] += 1
            continue
        if sel.startswith(("@keyframes", "@media", "@supports")):
            pieces.append(f"{sel} {{{m.group(2)}}}")
            continue
        pieces.append(f"{_scope_selector(sel, page)} {{{m.group(2)}}}")
        stats["scoped"] += 1
    return "\n  ".join(pieces), stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    css = _APP_CSS.read_text(encoding="utf-8")

    if "@keyframes" in css:
        print("已包含 @keyframes，疑似已修过；请人工确认后再运行。")
        return 2

    new_css, stats = transform(css)

    # 缺陷 2 修法：补回完整 keyframes（放在通用段之后、页面段之前）
    anchor = new_css.find("/* ── 页面：index")
    if anchor == -1:
        print("未找到页面分段锚点，拒绝写入")
        return 3
    new_css = new_css[:anchor] + KEYFRAMES + "\n" + new_css[anchor:]

    print(f"加作用域的规则: {stats['scoped']}")
    print(f"删除的孤立关键帧规则: {stats['dropped_keyframe_parts']}")
    print(f"保留的 at-rule: {stats['kept']}")
    # 泄漏检查：纯元素选择器（无 class/id/属性）会不分页面地命中。
    # 必须排除 at-rule 内部——@keyframes 的 from/to/百分比本来就该是裸的。
    # `*` 与 `:root` 位于通用段，本来就是刻意全局的，予以放行。
    INTENTIONALLY_GLOBAL = {"*", ":root"}
    outside_at = re.sub(r"@[\w-]+[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", new_css, flags=re.S)
    leaks = [s.strip() for s in re.findall(r"(?m)^([^\n{}]+)\{", outside_at)
             if not re.search(r"[.#\[]", s) and not s.strip().startswith("@")
             and s.strip() not in INTENTIONALLY_GLOBAL]
    print(f"修复后仍存在的纯元素选择器: {leaks}")

    if args.check:
        return 0
    if leaks:
        print("仍有泄漏，拒绝写入")
        return 4
    _APP_CSS.write_text(new_css, encoding="utf-8")
    print(f"已写回 {_APP_CSS.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
