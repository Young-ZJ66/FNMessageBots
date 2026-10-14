#!/usr/bin/env python3
"""把 history.html / faq.html 改为 extends base.html，并把脚本移入 {% block scripts %}。

为什么用脚本改而不是重打一遍：这两个页面里有大段静态内容（faq 的问答正文），
重打必然引入差异；脚本改完可以做「原有 id / class 是否全部保留」的机械校验。

同时给两个页面的返回栏补上主题切换按钮——它们原先没有主题按钮、也不加载 theme.js，
所以 data-theme 从未被设置过，深色模式在这两个页面等于不存在。

用法（项目根目录）
    python design-previews/_to_base.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = _ROOT / "src" / "web" / "templates"

# 主题按钮必须带 id="theme-toggle-btn"，theme.js 靠它绑定
THEME_BTN = (
    '<button type="button" class="theme-btn" id="theme-toggle-btn" '
    'title="切换到深色模式" aria-label="切换到深色模式">深色</button>'
)

# (模板, 返回栏选择器, 返回栏整段原文)  —— 用于把主题按钮插进返回栏
ANCHORS: Dict[str, tuple] = {
    "history.html": (
        '<div class="top-bar">\n        <a class="btn btn-ghost" href="./">返回配置页</a>\n      </div>',
        '<div class="top-bar">\n        <a class="btn btn-ghost" href="./">返回配置页</a>\n'
        f'        {THEME_BTN}\n      </div>',
    ),
    "faq.html": (
        '<div class="faq-back-bar">\n        <a class="btn btn-ghost" href="./">返回配置页</a>\n      </div>',
        '<div class="faq-back-bar">\n        <a class="btn btn-ghost" href="./">返回配置页</a>\n'
        f'        {THEME_BTN}\n      </div>',
    ),
}


def ids_and_classes(html: str) -> Set[str]:
    out: Set[str] = set()
    out |= set(re.findall(r'\bid="([^"]+)"', html))
    for m in re.findall(r'class="([^"{]+)"', html):
        out |= set(m.split())
    return out


def transform(name: str) -> str:
    src = (_TEMPLATES / name).read_text(encoding="utf-8")
    old = ids_and_classes(src)

    page_id = re.search(r'<html[^>]*data-page="([^"]+)"', src).group(1)
    title = re.search(r"<title>(.*?)</title>", src, re.S).group(1).strip()

    body = re.search(r"<body>(.*?)</body>", src, re.S).group(1)

    # 把 <script src> 抽出来单独放进 scripts 块
    scripts = re.findall(r"<script[^>]+src=\"[^\"]+\"[^>]*></script>", body)
    body_wo_scripts = re.sub(r"[ \t]*<script[^>]+src=\"[^\"]+\"[^>]*></script>\n?", "", body)
    body_wo_scripts = re.sub(r"[ \t]*<!-- 脚本已拆到.*?-->\n?", "", body_wo_scripts, flags=re.S)

    # 主题按钮插入返回栏
    anchor = ANCHORS.get(name)
    if anchor and anchor[0] in body_wo_scripts:
        body_wo_scripts = body_wo_scripts.replace(anchor[0], anchor[1], 1)
    else:
        print(f"  ! {name}: 未找到返回栏锚点，主题按钮未插入")

    body_wo_scripts = body_wo_scripts.strip("\n")
    script_lines = "\n".join(("  " + s) for s in scripts)
    new = (
        '{% extends "base.html" %}\n'
        f'{{% set page_id = "{page_id}" %}}\n'
        f'{{% set page_title = "{title}" %}}\n'
        "\n"
        "{% block body %}\n"
        f"{body_wo_scripts}\n"
        "{% endblock %}\n"
        "\n"
        "{% block scripts %}\n"
        f"{script_lines}\n"
        "{% endblock %}\n"
    )
    return new, old


def main() -> int:
    for name in ANCHORS:
        new, old = transform(name)
        p = _TEMPLATES / name
        p.write_text(new, encoding="utf-8")

        after = ids_and_classes(new)
        lost = sorted(old - after)
        print(f"  {name}: id/class 原有 {len(old)} 个，丢失 {len(lost)} 个 {lost if lost else ''}")
        if lost:
            print("    !! 有丢失，需检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
