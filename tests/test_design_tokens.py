"""设计令牌守门测试：把「零硬编码颜色」与「对比度达标」变成可执行的约束。

为什么值得单独守：深色模式最常见的退化方式，就是某个组件里写了一处硬编码颜色，
当时看不出问题，直到用户在深色下发现一处看不清。这类问题靠人工 review 极难拦住，
但用机器检查是确定性的——所以写成测试，而不是每次临时跑一遍脚本。

检查内容
1. 每个 CSS 文件里的颜色，要么在令牌定义块内，要么来自 var(--token)；
   组件规则里出现硬编码颜色即失败。
2. 所有 var(--x) 引用的令牌都已定义（拼错令牌名会静默失效）。
3. 深色块只覆盖令牌，不新增选择器规则。
4. WCAG 2.1 对比度：正文/小字 ≥ 4.5:1，明暗两套都查。
5. 自托管字体文件与其 OFL 授权文本存在（分发字体必须带授权）。

纯标准库实现，不需要浏览器。

运行方式（项目根目录）：
    python tests/test_design_tokens.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
_TOKENS = _ROOT / "assets" / "css" / "tokens.css"
_FONTS_DIR = _ROOT / "assets" / "fonts"

_DARK_SELECTOR = ':root[data-theme="dark"]'


# ─────────────────────────────────────────────────────────────────────────────
# 解析与颜色计算
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _extract_block(css: str, selector: str) -> str:
    """取出某个选择器的声明块正文（要求选择器位于行首，避免匹配到子串）。"""
    m = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{(.*?)^\s*\}}", css, re.S)
    return m.group(1) if m else ""


def _parse_decls(body: str) -> Dict[str, str]:
    return {
        k.strip(): v.strip()
        for k, v in re.findall(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+);", body)
    }


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


_HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _oklab(color: str) -> Tuple[float, float, float]:
    """sRGB → OKLab。用于感知色差，不能用亮度对比度代替。"""
    def _f(x: float) -> float:
        return x ** (1 / 3) if x > 0 else -((-x) ** (1 / 3))

    r, g, b = (_srgb_to_linear(c) for c in _rgb01(color))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _f(l), _f(m), _f(s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _delta_e_oklab(a: str, b: str) -> float:
    """OKLab 欧氏距离 ×100。经验参考：≳10 肉眼可辨，≳20 明显不同。"""
    import math

    ka, kb = _oklab(a), _oklab(b)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(ka, kb))) * 100


def _rgb01(color: str) -> Tuple[float, float, float]:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _load_tokens() -> Tuple[Dict[str, str], Dict[str, str]]:
    """返回（亮色令牌，深色令牌）。深色 = 亮色 + 深色块覆盖。

    注意 `_extract_block(css, ":root")` 只会命中裸 `:root {`；
    `:root[data-theme="dark"] {` 因 `:root` 后紧跟 `[` 而不会被误匹配。
    """
    css = _strip_comments(_read(_TOKENS))
    light = _parse_decls(_extract_block(css, ":root"))
    dark = dict(light)
    dark.update(_parse_decls(_extract_block(css, _DARK_SELECTOR)))
    return light, dark


# (前景, 背景, 用途, 最低对比度)
CONTRAST_PAIRS: List[Tuple[str, str, str, float]] = [
    ("--text", "--bg", "正文 / 页面底", 4.5),
    ("--text", "--surface", "正文 / 卡片", 4.5),
    ("--text", "--surface-2", "正文 / 次级面", 4.5),
    ("--text", "--surface-3", "正文 / 三级面", 4.5),
    ("--text-muted", "--bg", "次要文字 / 页面底", 4.5),
    ("--text-muted", "--surface", "次要文字 / 卡片", 4.5),
    ("--text-muted", "--surface-2", "次要文字 / 次级面", 4.5),
    ("--text-faint", "--bg", "弱化文字 / 页面底", 4.5),
    ("--text-faint", "--surface", "弱化文字 / 卡片", 4.5),
    ("--text-faint", "--surface-2", "弱化文字 / 次级面", 4.5),
    ("--accent-text", "--bg", "主色文字 / 页面底", 4.5),
    ("--accent-text", "--surface", "主色文字 / 卡片", 4.5),
    ("--accent-text", "--accent-soft", "主色文字 / 主色浅底", 4.5),
    ("--accent-fg", "--accent", "主按钮文字 / 按钮底", 4.5),
    ("--ok", "--surface", "成功 / 卡片", 4.5),
    ("--ok", "--ok-soft", "成功 / 成功浅底", 4.5),
    ("--warn", "--surface", "警告 / 卡片", 4.5),
    ("--warn", "--warn-soft", "警告 / 警告浅底", 4.5),
    ("--err", "--surface", "失败 / 卡片", 4.5),
    ("--err", "--err-soft", "失败 / 失败浅底", 4.5),
    ("--info", "--surface", "信息 / 卡片", 4.5),
    ("--info", "--info-soft", "信息 / 信息浅底", 4.5),
]


class TokenFileCase(unittest.TestCase):
    def test_tokens_file_exists(self):
        self.assertTrue(_TOKENS.is_file(), f"缺少 {_TOKENS.relative_to(_ROOT)}")

    def test_both_theme_blocks_present(self):
        css = _strip_comments(_read(_TOKENS))
        self.assertIn(":root {", css)
        self.assertIn(_DARK_SELECTOR, css, "缺少深色令牌块")

    def test_no_token_left_undefined(self):
        """var(--x) 引用但未定义会静默失效（样式直接丢掉），必须拦住。

        注意要按**整个 CSS 集合**校验，而不是单文件：
        令牌集中定义在 tokens.css，组件在 app.css 里引用，跨文件引用是正常的。
        曾经按单文件校验，于是 app.css 里每个令牌都被误报为「未定义」。
        """
        css_files = sorted((_ROOT / "assets" / "css").glob("*.css"))
        self.assertTrue(css_files, "assets/css 下没有 CSS 文件")
        defined: set = set()
        for f in css_files:
            defined |= set(re.findall(r"(--[A-Za-z0-9-]+)\s*:", _strip_comments(_read(f))))

        failures = []
        for f in css_files:
            used = set(re.findall(r"var\(\s*(--[A-Za-z0-9-]+)", _strip_comments(_read(f))))
            missing = sorted(used - defined)
            if missing:
                failures.append(f"{f.name} 引用了未定义的令牌: {missing}")
        self.assertEqual(failures, [], "\n  ".join(failures))

    def test_tokens_are_only_defined_in_tokens_css(self):
        """令牌定义应集中在 tokens.css，避免同一令牌在别处被悄悄改值。"""
        token_names = set(re.findall(r"(--[A-Za-z0-9-]+)\s*:",
                                     _strip_comments(_read(_TOKENS))))
        self.assertTrue(token_names, "tokens.css 里没有解析到任何令牌")
        for f in sorted((_ROOT / "assets" / "css").glob("*.css")):
            if f == _TOKENS:
                continue
            redefined = sorted(token_names &
                               set(re.findall(r"(--[A-Za-z0-9-]+)\s*:",
                                              _strip_comments(_read(f)))))
            with self.subTest(file=f.name):
                self.assertEqual(redefined, [],
                                 f"{f.name} 重复定义了 tokens.css 的令牌: {redefined}")

    def test_no_hardcoded_colors_outside_token_blocks(self):
        """组件规则里出现硬编码颜色 → 深色模式必然漏掉那一处。"""
        for f in sorted((_ROOT / "assets" / "css").glob("*.css")):
            css = _strip_comments(_read(f))
            rest = css
            for selector in (":root", _DARK_SELECTOR, "@font-face"):
                rest = re.sub(rf"(?m)^\s*{re.escape(selector)}\s*\{{.*?^\s*\}}", "", rest,
                              flags=re.S)
            stray = sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", rest)))
            with self.subTest(file=f.name):
                self.assertEqual(stray, [],
                                 f"{f.name} 令牌块外仍有硬编码颜色: {stray}")

    def test_dark_block_only_overrides_tokens(self):
        """深色块里不应出现组件规则——一旦出现，深色就会退化成逐选择器覆盖。

        `color-scheme` 是例外：它必须随主题切换，好让浏览器把滚动条与表单控件
        也渲染成深色，否则输入框会是刺眼的白底。
        """
        css = _strip_comments(_read(_TOKENS))
        body = _extract_block(css, _DARK_SELECTOR)
        self.assertTrue(body, "深色令牌块为空")
        allowed = ("--", "color-scheme")
        non_token = [ln.strip() for ln in body.splitlines()
                     if ln.strip() and not ln.strip().startswith(allowed)]
        self.assertEqual(non_token, [],
                         f"深色块里出现了非令牌声明: {non_token[:5]}")

    def test_contrast_meets_wcag(self):
        light, dark = _load_tokens()
        failures: List[str] = []
        for mode, tokens in (("亮色", light), ("深色", dark)):
            for fg, bg, use, need in CONTRAST_PAIRS:
                a, b = tokens.get(fg), tokens.get(bg)
                if not a or not b or not _HEX.match(a) or not _HEX.match(b):
                    continue
                ratio = _contrast(a, b)
                if ratio < need:
                    failures.append(
                        f"[{mode}] {use}: {a} on {b} = {ratio:.2f}:1 (需 {need}:1)")
        self.assertEqual(failures, [], "对比度不足：\n  " + "\n  ".join(failures))

    def test_status_colors_are_perceptually_separated(self):
        """成功 / 警告 / 失败三者应有足够感知色差。

        **不能用亮度对比度来判**：红与绿可以亮度几乎相同却一眼可分，
        所以这里用 OKLab 欧氏距离（ΔE_OK，×100）。也没有 WCAG 数值可用——
        阈值是选定的下限，不是标准条款。

        目标函数是**最大化最小距离**，而不是让某两对都很远：
        ok↔err 的色差约 31，弧上的中间色到两端各约一半，所以要求
        「warn 与两者都 ≥ 20」在几何上不可能（曾这样设过阈值，实测无一候选满足）。
        --info 与 --accent 有意同色，不参与比较。

        注意：这只是冗余提示。真正的无障碍保证是**状态同时有文字标签**
        （WCAG 1.4.1 要求颜色不得作为唯一信息载体），那条在模板层面验证。
        """
        MIN_DELTA_E = 12.0
        light, dark = _load_tokens()
        for mode, tokens in (("亮色", light), ("深色", dark)):
            names = ("--ok", "--warn", "--err")
            missing = [n for n in names if n not in tokens]
            with self.subTest(mode=mode):
                self.assertEqual(missing, [], f"{mode} 缺少状态令牌 {missing}")
                dists = {
                    (a, b): _delta_e_oklab(tokens[a], tokens[b])
                    for i, a in enumerate(names) for b in names[i + 1:]
                }
                worst = min(dists.values())
                self.assertGreaterEqual(
                    worst, MIN_DELTA_E,
                    f"[{mode}] 状态色最小感知色差 {worst:.1f} < {MIN_DELTA_E}："
                    + ", ".join(f"{a}/{b}={d:.1f}" for (a, b), d in dists.items()))

    def test_warn_sits_between_ok_and_err_not_beside_err(self):
        """回归守卫：亮色警告色曾经是 #b45309，与 --err 只差 ΔE 9.9 而距 --ok 有 21.9。

        这条断言「warn 到 err 的距离不得显著小于到 ok 的距离」，防止以后
        又把它调回偏红的琥珀。
        """
        light, _dark = _load_tokens()
        warn, ok, err = light["--warn"], light["--ok"], light["--err"]
        d_err = _delta_e_oklab(warn, err)
        d_ok = _delta_e_oklab(warn, ok)
        self.assertGreaterEqual(
            d_err, d_ok * 0.75,
            f"--warn 距 --err({d_err:.1f}) 明显小于距 --ok({d_ok:.1f})，"
            "警告容易被误读为失败")


class FontAssetCase(unittest.TestCase):
    def test_font_files_present(self):
        for name in ("Inter-latin.woff2", "JetBrainsMono-latin.woff2"):
            p = _FONTS_DIR / name
            with self.subTest(font=name):
                self.assertTrue(p.is_file(), f"缺少字体文件 assets/fonts/{name}")
                self.assertGreater(p.stat().st_size, 8 * 1024, f"{name} 体积异常，可能下载不完整")

    def test_font_files_are_woff2(self):
        for p in sorted(_FONTS_DIR.glob("*.woff2")):
            with self.subTest(font=p.name):
                self.assertEqual(p.read_bytes()[:4], b"wOF2", f"{p.name} 不是合法 woff2")

    def test_ofl_license_shipped_with_fonts(self):
        """SIL OFL 1.1 要求随字体分发授权文本——分发字体不带授权是不合规的。"""
        for name in ("Inter-OFL.txt", "JetBrainsMono-OFL.txt"):
            p = _FONTS_DIR / name
            with self.subTest(license=name):
                self.assertTrue(p.is_file(), f"缺少授权文本 assets/fonts/{name}")
                self.assertIn("SIL OPEN FONT LICENSE", _read(p).upper(),
                              f"{name} 不是 SIL OFL 文本")

    def test_font_face_rules_keep_unicode_range(self):
        """丢掉 unicode-range 会让 latin 子集被用于渲染中文 → 豆腐块。"""
        css = _strip_comments(_read(_TOKENS))
        blocks = re.findall(r"@font-face\s*\{(.*?)\}", css, re.S)
        self.assertEqual(len(blocks), 2, f"应有 2 个 @font-face，实际 {len(blocks)}")
        for b in blocks:
            self.assertIn("unicode-range", b, "@font-face 缺少 unicode-range")
            self.assertIn("font-weight", b)
            self.assertRegex(b, r"font-weight:\s*\d+\s+\d+",
                             "可变字体应声明字重区间（如 100 900）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
