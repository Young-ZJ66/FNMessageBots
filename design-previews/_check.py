#!/usr/bin/env python3
"""自动检测样稿的横向溢出（真实渲染，不靠肉眼）。

做法：把一份「测量脚本」注入每个样稿的临时副本，用 Chrome 无头模式渲染后
`--dump-dom` 取回结果。脚本会报告：
  - 文档 scrollWidth vs clientWidth（整页横向滚动条）
  - 任何右边界超出视口的元素（最多列 8 个）

为什么需要它：`grid-template-columns: repeat(n, 1fr)` 的 `1fr` 隐含 min-content
下限，内容一挤就撑破容器、元素被裁掉——这个问题在截图里不显眼，但真实存在
（开发过程中「定时巡检」小卡就这样被裁过一次）。

用法：
    python design-previews/_check.py
需要本机装有 Chrome（脚本内自动探测）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve().parent

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

WIDTHS = [1500, 1180, 900, 480]

PROBE = """
<script>
(function () {
  // 有意可横向滚动的容器（如表格兜底的 .tblwrap）不算溢出——
  // 我们要拦的是「整页出现横向滚动条」和「元素被裁掉」。
  function inScrollable(el) {
    var p = el.parentElement;
    while (p && p !== document.body) {
      var ox = getComputedStyle(p).overflowX;
      if (ox === "auto" || ox === "scroll") return true;
      p = p.parentElement;
    }
    return false;
  }
  function measure() {
    var d = document.documentElement;
    var over = [];
    document.querySelectorAll("body *").forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return;
      if (r.right > d.clientWidth + 1 && !inScrollable(el)) {
        var id = el.tagName.toLowerCase() +
          (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\\s+/).join(".") : "");
        over.push(id + "@" + Math.round(r.right));
      }
    });
    var out = document.createElement("div");
    out.id = "overflow-probe";
    out.setAttribute("data-report", JSON.stringify({
      scrollWidth: d.scrollWidth,
      clientWidth: d.clientWidth,
      overflowing: over.slice(0, 8),
      overflowCount: over.length
    }));
    document.body.appendChild(out);
  }
  if (document.readyState === "complete") measure();
  else window.addEventListener("load", measure);
})();
</script>
"""


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    print("未找到 Chrome/Edge，跳过渲染检测")
    raise SystemExit(2)


def render_report(chrome: str, html_path: Path, width: int, profile: Path) -> dict | None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / html_path.name
        tmp.write_text(
            html_path.read_text(encoding="utf-8").replace("</body>", PROBE + "</body>"),
            encoding="utf-8",
        )
        cmd = [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--user-data-dir={profile}", f"--window-size={width},1200",
            "--virtual-time-budget=4000", "--dump-dom", tmp.as_uri(),
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return None
    m = re.search(r'data-report="(.*?)"', p.stdout or "", re.S)
    if not m:
        return None
    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def parse_tokens(css: str, selector: str) -> dict:
    """取出某个选择器的自定义属性。"""
    pat = re.escape(selector) + r"\s*\{(.*?)\}"
    m = re.search(pat, css, re.S)
    if not m:
        return {}
    body = m.group(1)
    return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


# (前景令牌, 背景令牌, 用途, 最小对比度)  —— 4.5 正文/小字，3.0 大字号或非文字图形
CONTRAST_PAIRS = [
    ("--text", "--bg", "正文 / 页面底", 4.5),
    ("--text", "--surface", "正文 / 卡片", 4.5),
    ("--text", "--surface-2", "正文 / 次级面", 4.5),
    ("--text-muted", "--bg", "次要文字 / 页面底", 4.5),
    ("--text-muted", "--surface", "次要文字 / 卡片", 4.5),
    ("--text-faint", "--bg", "弱化文字 / 页面底", 4.5),
    ("--text-faint", "--surface", "弱化文字 / 卡片", 4.5),
    ("--accent", "--bg", "强调色文字 / 页面底", 4.5),
    ("--accent-fg", "--accent", "主按钮文字 / 按钮底", 4.5),
    ("--ok", "--surface", "成功状态 / 卡片", 4.5),
    ("--warn", "--surface", "警告状态 / 卡片", 4.5),
    ("--err", "--surface", "失败状态 / 卡片", 4.5),
]


def check_tokens_and_contrast() -> int:
    print()
    print("=" * 72)
    print("设计令牌卫生 + WCAG 对比度（正文/小字要求 4.5:1）")
    print("=" * 72)
    problems = 0
    for f in sorted(HERE.glob("*.html")):
        html = f.read_text(encoding="utf-8")
        style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        # ── 硬编码颜色卫生：令牌块之外不应出现 hex ──
        stripped = style
        for sel in (":root", ':root[data-theme="dark"]'):
            stripped = re.sub(re.escape(sel) + r"\s*\{.*?\}", "", stripped, flags=re.S)
        stray = re.findall(r"#[0-9a-fA-F]{3,8}\b", stripped)
        if stray:
            problems += 1
            print(f"  ! {f.name}: 令牌块外仍有硬编码颜色 {sorted(set(stray))}")
        else:
            print(f"  ok {f.name}: 令牌块外零硬编码颜色")

        # ── 对比度 ──
        dark_block = ':root[data-theme="dark"]' in style
        light = parse_tokens(style, ":root")
        dark = dict(light)
        dark.update(parse_tokens(style, ':root[data-theme="dark"]'))
        dark_only = bool(re.search(r'<html[^>]*data-theme="dark"', html)) and not dark_block
        modes = (("深色", dark),) if dark_only else (("亮色", light), ("深色", dark))
        for mode, toks in modes:
            fails = []
            for fg, bg, use, need in CONTRAST_PAIRS:
                if fg not in toks or bg not in toks:
                    continue
                a, b = toks[fg], toks[bg]
                if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", a) or not re.fullmatch(r"#[0-9a-fA-F]{3,8}", b):
                    continue
                r = contrast(a, b)
                if r < need:
                    fails.append(f"{use}: {a} on {b} = {r:.2f}:1 (需 {need})")
            if fails:
                problems += 1
                print(f"  ! {f.name} [{mode}] 对比度不足：")
                for x in fails:
                    print(f"      {x}")
    return problems


def main() -> int:
    files = sorted(p for p in HERE.glob("*.html") if p.name != "index.html")
    if not files:
        print("没有找到样稿，先运行 _build.py")
        return 1
    chrome = find_chrome()
    profile = Path(tempfile.gettempdir()) / "preview-check-profile"
    profile.mkdir(parents=True, exist_ok=True)

    failures: List[Tuple[str, int, str]] = []
    print(f"{'文件':<22}{'宽度':>6}{'scrollW':>9}{'clientW':>9}  结果")
    for f in files:
        for w in WIDTHS:
            r = render_report(chrome, f, w, profile)
            if r is None:
                print(f"{f.name:<22}{w:>6}{'—':>9}{'—':>9}  ! 无法取到测量结果")
                failures.append((f.name, w, "measure-failed"))
                continue
            horizontal_scroll = r["scrollWidth"] > r["clientWidth"] + 1
            bad = horizontal_scroll or r["overflowCount"] > 0
            detail = ""
            if r["overflowCount"]:
                detail = " 溢出元素: " + ", ".join(r["overflowing"][:3])
            print(f"{f.name:<22}{w:>6}{r['scrollWidth']:>9}{r['clientWidth']:>9}  "
                  f"{'FAIL' if bad else 'ok'}{detail}")
            if bad:
                failures.append((f.name, w, detail.strip()))

    print()
    if failures:
        print(f"发现 {len(failures)} 处横向溢出：")
        for name, w, d in failures:
            print(f"  - {name} @ {w}px {d}")
    else:
        print(f"横向溢出：全部通过（{len(files)} 个样稿 × {len(WIDTHS)} 种宽度）")

    problems = check_tokens_and_contrast()
    print()
    if failures or problems:
        print(f"结论：溢出 {len(failures)} 项，令牌/对比度 {problems} 项，需要修复")
        return 1
    print("结论：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
