#!/usr/bin/env python3
"""把模板里的内联 JS 提取成 assets/js/*.js，并证明「只是搬移」。

为什么用脚本 + 行比对：内联 JS 共 50 KB / 1200 行，手抄必然出错。
而且「纯搬移」这句话必须能被机械验证——脚本会把新文件按加载顺序拼回来，
与原始脚本体做**去缩进后的行多重集比对**，任何增删改都会暴露。

为什么可以按主题切而不是整块搬：
- 三个脚本块里 **Jinja 插值都是 0**，是纯静态代码。
- 脚本没有 IIFE 包裹，顶层 `const/let/function` 处于全局词法环境；
  函数声明会提升，因此函数在文件之间可以自由换位，
  只要保证顶层**可执行语句**（几处变量初始化）仍在被使用之前执行。
- 实测顶层可执行语句只有 42–82 行的 DOM 引用与状态变量，以及末尾的 load 注册。

用法（项目根目录）
    python design-previews/_extract_js.py           # 提取 + 校验
    python design-previews/_extract_js.py --check   # 只校验
"""

from __future__ import annotations

import argparse
import collections
import re
from pathlib import Path
from typing import Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "src" / "web" / "templates"
_JS_DIR = _ROOT / "assets" / "js"

# 每个模块：(输出文件名, 说明, [(起, 止), ...])  —— 行号基于 script 体，1 起始、含两端
INDEX_MODULES: List[Tuple[str, str, List[Tuple[int, int]]]] = [
    ("theme.js", "主题：读取/解析/应用/切换，以及切换按钮的绑定", [(1, 41)]),
    ("core.js", "DOM 引用与共享状态、未保存更改跟踪、状态条/Toast/持久警告",
     [(42, 82), (173, 247)]),
    ("auth.js", "鉴权：初始化门禁、首次设置密码、登录", [(83, 172)]),
    ("channels.js", "推送渠道：各渠道表单构件的序列化与构建、渠道行增删",
     [(248, 647)]),
    ("events.js", "监控事件：按分类渲染勾选卡片与全选/折叠", [(648, 767)]),
    ("app.js", "加载配置与推送统计、初始化入口", [(768, 10 ** 6)]),
]

SIMPLE_MODULES: Dict[str, Tuple[str, str]] = {
    "history.html": ("history.js", "推送记录页：列表、筛选、加载更多、详情面板"),
    "faq.html": ("faq.js", "常见问题页：目录与折叠交互"),
}

HEADER = """/* {title}
   ----------------------------------------------------------------------------
   {desc}

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */
"""


def _script_body(name: str) -> str:
    t = (_TEMPLATES / name).read_text(encoding="utf-8")
    m = re.search(r"<script[^>]*>(.*?)</script>", t, re.S)
    return m.group(1) if m else ""


def _dedent(lines: List[str]) -> List[str]:
    """去掉整体 4 空格缩进（仅作用于有该前缀的行）。"""
    out = []
    for l in lines:
        out.append(l[4:] if l.startswith("    ") else l)
    return out


def _norm(lines: List[str]) -> List[str]:
    """用于比对的规范形式：去缩进、去首尾空白、丢空行。"""
    return [l.strip() for l in lines if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    # 本脚本是一次性迁移工具：输入是模板里的内联 <script>。
    # 脚本外置之后重跑它只能读到空内容——此时绝不能写文件，
    # 否则会把 assets/js/*.js 全部清空。
    if not any(_script_body(n).strip() for n in ("index.html", "history.html", "faq.html")):
        print("拒绝写出：模板里已无内联 <script>，没有可提取的内容。")
        print("  该脚本只适用于「内联脚本尚未外置」的历史状态。")
        return 3

    if not args.check:
        _JS_DIR.mkdir(parents=True, exist_ok=True)

    report: List[str] = []
    failures: List[str] = []

    # ── index.html ──
    body = _script_body("index.html")
    lines = body.splitlines()
    original_norm = collections.Counter(_norm(lines))

    produced_norm: List[str] = []
    index_files: List[str] = []
    for out_name, desc, ranges in INDEX_MODULES:
        chunk: List[str] = []
        for a, b in ranges:
            chunk += lines[a - 1:min(b, len(lines))]
        chunk = _dedent(chunk)
        produced_norm += _norm(chunk)
        header = HEADER.format(title=f"FnMessageBot · {out_name}",
                               desc=desc)
        content = header + "\n" + "\n".join(chunk).rstrip() + "\n"
        if not args.check:
            (_JS_DIR / out_name).write_text(content, encoding="utf-8")
        index_files.append(out_name)
        report.append(f"  {out_name:<14} {len(chunk):>5} 行  {desc[:44]}")

    missing = original_norm - collections.Counter(produced_norm)
    extra = collections.Counter(produced_norm) - original_norm
    if missing or extra:
        failures.append(
            f"index.html 搬移不完整：丢失 {sum(missing.values())} 行、多出 {sum(extra.values())} 行")
        for l, n in list(missing.items())[:5]:
            failures.append(f"      丢失 ×{n}: {l[:90]}")
        for l, n in list(extra.items())[:5]:
            failures.append(f"      多出 ×{n}: {l[:90]}")

    # ── history.html / faq.html ──
    for tpl, (out_name, desc) in SIMPLE_MODULES.items():
        b = _script_body(tpl)
        lb = b.splitlines()
        chunk = _dedent(lb)
        if not args.check:
            header = HEADER.format(title=f"FnMessageBot · {out_name}", desc=desc)
            (_JS_DIR / out_name).write_text(
                header + "\n" + "\n".join(chunk).rstrip() + "\n", encoding="utf-8")
        if collections.Counter(_norm(lb)) != collections.Counter(_norm(chunk)):
            failures.append(f"{tpl} → {out_name} 行内容不一致")
        report.append(f"  {out_name:<14} {len(chunk):>5} 行  {desc[:44]}")

    print("=== 提取结果 ===")
    print("\n".join(report))
    print()
    print(f"原 script 体 {len(lines)} 行；拼回后 {len(produced_norm)} 行（去空行去缩进后比对）")
    if failures:
        print("!! 校验失败：")
        for f in failures:
            print("  " + f)
        return 1
    print("✅ 行多重集完全一致：确认是纯搬移，无增删改")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
