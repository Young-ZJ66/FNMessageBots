"""Web 层基线测试：在重构 UI 之前锁住现有行为。

为什么需要它：`src/web/templates/*.html` 是一个长表单 + 41KB 内联 JS，没有任何直接测试。
重构 UI 时最容易悄悄坏掉的是**看不见的东西**：路由消失、鉴权闸门失效、密钥打码泄漏、
保存时把打码值当成新密钥写回（等于把 webhook 地址清空）。这些靠肉眼 review 抓不住。

实现方式
- `tests/_support/stub_flask.py`：功能桩，让 `create_app()` 真跑起来并能派发请求。
- 本文件 `setUpClass` 里跑**一个**子进程收集全部事实（路由表、各路径状态码、
  模板上下文、/api/config 响应、打码值保存往返），之后每个用例只断言其中一片。
  这样既保持子进程隔离（`app_paths` 在导入期解析 `APP_HOME`），又不用每个用例起一个进程。
- 模板本身用静态分析（标签配平、变量是否被上下文覆盖、静态资源是否存在），
  不需要重新实现 Jinja。

运行方式（项目根目录）：
    python tests/test_web_ui_baseline.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Set

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_SUPPORT = _ROOT / "tests" / "_support"
_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"
_TEMPLATES = _SRC / "web" / "templates"

# 基线路由表。断言「实际包含这些」而非「等于」——重构可以新增路由，但删掉必须失败。
EXPECTED_ROUTES = {
    ("GET", "/"),
    ("GET", "/history"),
    ("GET", "/faq"),
    ("GET", "/robots.txt"),
    ("GET", "/favicon.ico"),
    ("GET", "/assets/<path:filename>"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/set-password"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/config"),
    ("POST", "/api/save-config"),
    ("POST", "/api/test"),
    ("POST", "/api/nas-patrol/run"),
    ("GET", "/api/push-stats"),
    ("GET", "/api/push-history"),
    ("GET", "/api/push-history/<int:record_id>"),
}

# 未登录时应被拒绝的路径 → 期望状态码
ANON_EXPECTED = {
    "/api/config": 401,
    "/api/push-stats": 401,
    "/api/push-history": 401,
    "/history": 302,
}

# 公开路径 → 期望 200
PUBLIC_EXPECTED = {
    "/": 200,
    "/api/auth/status": 200,
    "/robots.txt": 200,
}

# 用作「真实密钥」，用于验证打码与回填
REAL_WECHAT_KEY = "AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"


PROBE = r'''
import json, os, sys
from pathlib import Path
import stub_deps; stub_deps.install()
import stub_flask
from web import ui_app

GW = {
    "fnmb.via_gateway": True,
    "HTTP_X_TRIM_USERID": "1000",
    "HTTP_X_TRIM_USERNAME": "admin",
    "HTTP_X_TRIM_ISADMIN": "true",
    "REMOTE_ADDR": "127.0.0.1",
}

app = ui_app.create_app()
client = app.test_client()


def hit(path, method="GET", env=None, **kw):
    if method == "POST":
        r = client.post(path, environ=env, **kw)
    else:
        r = client.get(path, environ=env, **kw)
    return {"status": r.status_code, "location": r.headers.get("Location", ""), "text": r.text}


out = {"routes": [[m, v.rule] for v in app.views for m in v.methods]}
out["anon"] = {p: hit(p) for p in ANON_PATHS}
out["public"] = {p: hit(p) for p in PUBLIC_PATHS}
out["admin"] = {p: hit(p, env=GW) for p in ADMIN_PATHS}
out["non_json_post"] = hit("/api/save-config", method="POST", env=GW, has_json=False)

# 模板上下文
stub_flask.reset_render_log()
for p in ("/", "/faq"):
    hit(p, env=GW)
out["renders"] = [{"template": r["template"], "context": sorted(r["context"])}
                  for r in stub_flask.RENDER_LOG]
# 推送记录已并入首页概览：/history 现在是 302 跳转，不再渲染模板
out["history_redirect"] = hit("/history", env=GW)

# 打码：从 /api/config 读回 channels，确认密钥不出现
cfg = hit("/api/config", env=GW)
out["config_text"] = cfg["text"]
payload = json.loads(cfg["text"]).get("data") or {}
out["channels_from_api"] = payload.get("channels")

# 往返：把「读到的（打码）渠道」原样保存回去，密钥必须存活
save = hit("/api/save-config", method="POST", env=GW, json_body={
    "events": ["LoginSucc"],
    "channels": payload.get("channels") or [],
})
out["save"] = save
cfg_file = Path(os.environ["APP_HOME"]) / "config" / "config.json"
saved = json.loads(cfg_file.read_text(encoding="utf-8"))
out["saved_wechat"] = saved.get("wechat_webhook_url", "")

print("__PROBE__" + json.dumps(out, ensure_ascii=False))
'''


def _make_app_home() -> Path:
    home = _TMP_ROOT / f"ui_{uuid.uuid4().hex[:8]}"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (home / "data" / "cursor").mkdir(parents=True, exist_ok=True)
    cfg = {
        "wechat_webhook_url": (
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + REAL_WECHAT_KEY
        ),
        "monitor_events": ["LoginSucc"],
    }
    (home / "config" / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return home


# ─────────────────────────────────────────────────────────────────────────────
# 模板静态分析
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_TAGS = ("if", "for", "block", "macro", "with", "filter", "autoescape", "call", "raw")
_JINJA_GLOBALS = {
    "loop", "range", "dict", "lipsum", "cycler", "joiner", "namespace", "url_for",
    "get_flashed_messages", "request", "session", "g", "config", "none", "true",
    "false", "True", "False", "None", "self", "super", "caller",
}


def _strip_code_blocks(html: str) -> str:
    """去掉 <script>/<style> 内容——否则 JS 里的 `{{` 会被误当成 Jinja 表达式。"""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    return re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.S | re.I)


def _tag_balance(html: str) -> List[str]:
    """检查 {% xxx %} 标签是否配平，返回问题列表。"""
    problems: List[str] = []
    stack: List[str] = []
    for m in re.finditer(r"\{%-?\s*(\w+)\b(.*?)-?%\}", html, re.S):
        name, rest = m.group(1), m.group(2)
        if name in ("else", "elif", "endif", "endfor", "endblock", "endmacro",
                    "endwith", "endfilter", "endcall", "endraw", "endautoescape",
                    "extends", "include", "import", "from", "set", "do", "break", "continue"):
            if name.startswith("end"):
                want = name[3:]
                if not stack:
                    problems.append(f"多余的 {{% {name} %}}")
                elif stack[-1] != want:
                    problems.append(f"{{% {name} %}} 与未闭合的 {{% {stack[-1]} %}} 不匹配")
                    stack.pop()
                else:
                    stack.pop()
            continue
        if name in _BLOCK_TAGS:
            # raw / autoescape 内部不解析，简化处理：仍入栈
            stack.append(name)
    for left in stack:
        problems.append(f"{{% {left} %}} 未闭合")
    return problems


def _referenced_vars(html: str) -> Set[str]:
    """提取模板引用的顶层变量名。"""
    found: Set[str] = set()

    def add(expr: str) -> None:
        for raw in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
            found.add(raw)

    for m in re.finditer(r"\{\{-?\s*(.*?)\s*-?\}\}", html, re.S):
        expr = m.group(1)
        # 去掉字符串字面量，避免把 'zh_cn' 之类当成变量
        expr = re.sub(r"'[^']*'|\"[^\"]*\"", "", expr)
        add(expr.split("|")[0])
    for m in re.finditer(r"\{%-?\s*(?:if|elif|for|set)\b(.*?)-?%\}", html, re.S):
        body = m.group(1)
        body = re.sub(r"'[^']*'|\"[^\"]*\"", "", body)
        if m.group(0).lstrip("{%- ").startswith("for"):
            # for x in y  →  只有 y 是外部引用
            parts = re.split(r"\bin\b", body, maxsplit=1)
            if len(parts) == 2:
                add(parts[1])
            continue
        add(body)
    for m in re.finditer(r"\{%-?\s*set\s+([A-Za-z_][A-Za-z0-9_]*)", html):
        found.discard(m.group(1))
    return {v for v in found if v not in _JINJA_GLOBALS}


def _declared_vars(html: str) -> Set[str]:
    """模板内 {% set x %}、{% for x in ... %} 声明的局部名，以及宏名与宏参数。"""
    declared: Set[str] = set()
    for m in re.finditer(r"\{%-?\s*set\s+([A-Za-z_][A-Za-z0-9_]*)", html):
        declared.add(m.group(1))
    for m in re.finditer(r"\{%-?\s*for\s+(.*?)\s+in\b", html, re.S):
        for name in re.split(r",", m.group(1)):
            declared.add(name.strip())
    # 宏（如 switch 开关组件）：宏名会出现在调用表达式里，宏参数是宏内部的形参，
    # 都由调用方传入，属于模板内部名字，不是视图需要提供的变量。
    for m in re.finditer(
            r"\{%-?\s*macro\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*-?%\}", html, re.S):
        declared.add(m.group(1))
        for part in m.group(2).split(","):
            part = part.strip()
            if not part:
                continue
            declared.add(part.split("=")[0].strip())
    return declared


class TemplateStaticCase(unittest.TestCase):
    """模板静态分析——不需要渲染，也就绕开了「没有 Jinja」的限制。"""

    def templates(self) -> Dict[str, str]:
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(_TEMPLATES.glob("*.html"))}

    def test_templates_exist(self):
        names = set(self.templates())
        for expected in ("index.html", "base.html", "faq.html"):
            self.assertIn(expected, names, f"缺少模板 {expected}")
        # 推送记录已并入首页概览，不应再有独立页面模板
        self.assertNotIn("history.html", names,
                         "推送记录已并入概览页，history.html 应已删除")

    def test_jinja_tags_balanced(self):
        for name, html in self.templates().items():
            with self.subTest(template=name):
                problems = _tag_balance(_strip_code_blocks(html))
                self.assertEqual(problems, [], f"{name} 标签不配平: {problems}")

    def test_no_duplicate_element_ids(self):
        """重复 id 会让 JS 的 getElementById 取到意外元素——重构时极易引入。"""
        for name, html in self.templates().items():
            ids = re.findall(r'\bid="([^"]+)"', _strip_code_blocks(html))
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            with self.subTest(template=name):
                self.assertEqual(dupes, [], f"{name} 存在重复 id: {dupes}")

    def test_ids_referenced_by_js_exist(self):
        """JS 取用的 id，必须能由模板静态声明、或由 JS 自己用字符串生成。

        这是重构 UI 最危险的耦合点：改掉或删掉一个 id，JS 不会报错——
        它只是拿到 null 然后静默什么都不做。

        两种来源都要算：
        1) 模板里的 id="x"（绝大多数）；
        2) JS 在**字符串里**拼出来的 id="x"（history.js 的详情面板就是这样：
           先把 innerHTML 设好，再 getElementById 取用）。
        早期版本只认第 1 种、且「JS 自建」只找 `.id=` 与 setAttribute，
        于是把 history.js 的 detail-close 误判为缺失——正则范围过窄会给出错误结论。
        """
        js_refs = re.compile(
            r'getElementById\(\s*"([^"]+)"\s*\)'          # 必须是完整字面量
            r'|querySelector(?:All)?\(\s*"#([A-Za-z0-9_\-]+)"')
        id_literal = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")

        declared: Set[str] = set()
        for _name, html in self.templates().items():
            declared |= set(re.findall(r'\bid="([^"]+)"', html))
            # 开关组件由宏生成 id（{{ switch("input-xxx", "…") }}），
            # 静态扫 id="…" 看不到它，但渲染后确实存在，故按调用参数补进来。
            declared |= set(re.findall(r'\{\{-?\s*switch\(\s*"([^"]+)"', html))

        macro_calls = sum(len(re.findall(r'\{\{-?\s*switch\(', h))
                          for h in self.templates().values())
        # 实测 8 处；留余量，但明显偏少即说明提取失效
        self.assertGreaterEqual(macro_calls, 6,
                                f"只找到 {macro_calls} 处开关宏调用，提取范围可能失效")

        needed: Set[str] = set()
        for _name, html in self.templates().items():
            needed |= {m.group(1) or m.group(2) for m in js_refs.finditer(html)}
        for js in self.scripts():
            text = js.read_text(encoding="utf-8")
            needed |= {m.group(1) or m.group(2) for m in js_refs.finditer(text)}
            # JS 字符串里创建的 id 也算已声明
            declared |= set(id_literal.findall(text))

        self.assertGreater(len(needed), 20,
                           f"只提取到 {len(needed)} 个 id 引用，范围或正则可能失效")
        missing = sorted(needed - declared)
        self.assertEqual(missing, [], f"JS 取用但无处声明的 id: {missing}")

    def scripts(self) -> List[Path]:
        return sorted((_ROOT / "assets" / "js").glob("*.js"))

    def test_scripts_directory_exists(self):
        self.assertTrue((_ROOT / "assets" / "js").is_dir(), "缺少 assets/js 目录")
        self.assertTrue(self.scripts(), "assets/js 下没有脚本文件")

    def test_templates_reference_existing_local_assets(self):
        """模板引用的 <script src> 与 <link href> 必须真实存在。

        外置资源后新增的故障类型：文件改名、漏拷、路径写错时页面不报错，
        只是样式或脚本静默不生效。相对路径还要兼容统一网关前缀，不能改成绝对路径。
        """
        missing: List[str] = []
        absolute: List[str] = []
        for name, html in self.templates().items():
            refs = re.findall(r'<script[^>]+src="([^"]+)"', html)
            refs += re.findall(r'<link[^>]+href="([^"]+)"', html)
            for ref in refs:
                if ref.startswith(("http://", "https://", "//", "{{")):
                    continue
                if ref.startswith("/"):
                    absolute.append(f"{name}: {ref}")
                    continue
                if not (_ROOT / ref.split("?")[0]).is_file():
                    missing.append(f"{name}: {ref}")
        self.assertEqual(missing, [], f"模板引用了不存在的资源: {missing}")
        self.assertEqual(absolute, [],
                         f"本地资源必须用相对路径（网关前缀兼容）: {absolute}")

    def test_no_inline_style_or_script_blocks(self):
        """样式与脚本应已外置；残留内联块会让两处定义并存、难以维护。"""
        for name, html in self.templates().items():
            with self.subTest(template=name):
                self.assertEqual(re.findall(r"<style[^>]*>", html), [],
                                 f"{name} 仍有内联 <style>")
                inline = [a for a, _b in re.findall(r"<script([^>]*)>(.*?)</script>",
                                                    html, re.S)
                          if "src=" not in a]
                self.assertEqual(inline, [], f"{name} 仍有内联 <script>")

    def test_js_does_not_create_ids_it_later_looks_up(self):
        """反向确认：目前没有任何 id 是 JS 动态创建后再取用的。

        如果哪天出现了，上一条测试的「静态声明」前提就不成立，
        需要把动态创建的 id 也纳入白名单——这条会先失败提示去处理。
        """
        js_refs = re.compile(r'getElementById\(\s*"([^"]+)"')
        makes = re.compile(r'\.id\s*=\s*"([^"]+)"|setAttribute\(\s*"id"\s*,\s*"([^"]+)"')
        for name, html in self.templates().items():
            needed = set(js_refs.findall(html))
            made = {m.group(1) or m.group(2) for m in makes.finditer(html)}
            overlap = sorted(needed & made)
            with self.subTest(template=name):
                self.assertEqual(overlap, [],
                                 f"{name} 中 JS 动态创建又被取用的 id: {overlap}")

    def test_local_asset_references_resolve(self):
        """模板引用的本地静态资源必须真实存在（重构换目录时最容易漏）。"""
        missing: List[str] = []
        # 只查「看起来是文件」的引用：相对页面链接（href="faq"、href="faq#x"）不算资源
        filelike = re.compile(r"\.[A-Za-z0-9]{2,5}$")
        for name, html in self.templates().items():
            for m in re.finditer(r'(?:src|href)="((?!https?:|#|\{\{)[^"]+)"', html):
                ref = m.group(1).split("?")[0].strip()
                if not ref or ref.startswith("/") or not filelike.search(ref):
                    continue
                for base in (_SRC / "web", _ROOT, _SRC):
                    if (base / ref).exists():
                        break
                else:
                    missing.append(f"{name}: {ref}")
        self.assertEqual(missing, [], f"模板引用了不存在的资源: {missing}")


def _top_level_names(js: str) -> Set[str]:
    """挑出脚本在**顶层**声明的名字（function / const / let / var）。

    为什么要按花括号深度走一遍、而不是按行首缩进判断：
    实际踩过一次——history.js 里 `  var THEME_STORAGE_KEY`（缩进 2 空格）
    与 theme.js 的顶层 `const THEME_STORAGE_KEY` 同名。经典脚本共享全局词法环境，
    已有 const 绑定时再 var 同名会抛 SyntaxError，**整个 history.js 都不执行**，
    列表与筛选静默失效。按缩进判断会漏掉这种带缩进的声明。

    这是启发式而非解析器：字符串与注释被跳过，但模板字符串里的 ${} 会被当成花括号
    计入深度，可能造成偏差。因此调用方会先断言「提取到的名字数量在合理区间」，
    以免启发式失效时测试变成空过。
    """
    names: Set[str] = set()
    depth = 0
    i = 0
    n = len(js)
    quote: str = ""
    line_comment = False
    block_comment = False
    decl = re.compile(
        r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
        r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
    while i < n:
        if line_comment:
            if js[i] == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if js.startswith("*/", i):
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if js[i] == "\\":
                i += 2
                continue
            if js[i] == quote:
                quote = ""
            i += 1
            continue
        two = js[i:i + 2]
        if two == "//":
            line_comment = True
            i += 2
            continue
        if two == "/*":
            block_comment = True
            i += 2
            continue
        c = js[i]
        if c in "\"'`":
            quote = c
            i += 1
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            m = decl.match(js, i)
            if m:
                names.add(m.group(1) or m.group(2))
                i = m.end()
                continue
        i += 1
    return names


def _strip_js_comments(js: str) -> str:
    """去掉行注释与块注释，避免把注释里提到的关键字当成真实代码。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)//.*$", "", js)


class ScriptScopeCase(unittest.TestCase):
    """外置脚本共享顶层作用域（无 IIFE），因此**同名声明会让后者整个不执行**。

    这是浏览器里静默失败的一种：控制台之外没有提示，页面只是少了功能。
    实测踩过一次（theme.js 的 const 与 history.js 的 var 同名），故固化为测试。
    """

    def scripts(self) -> List[Path]:
        return sorted((_ROOT / "assets" / "js").glob("*.js"))

    def loaded_per_page(self) -> Dict[str, List[Path]]:
        """按模板求出每页实际加载的脚本（含 base.html 里的 theme.js）。"""
        base = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
        base_scripts = re.findall(r'<script[^>]+src="assets/js/([^"]+)"', base)
        pages: Dict[str, List[Path]] = {}
        for p in sorted(_TEMPLATES.glob("*.html")):
            html = p.read_text(encoding="utf-8")
            names = list(base_scripts) + re.findall(
                r'<script[^>]+src="assets/js/([^"]+)"', html)
            # 去重且保序
            seen: Set[str] = set()
            files = []
            for n in names:
                if n in seen:
                    continue
                seen.add(n)
                files.append(_ROOT / "assets" / "js" / n)
            if files:
                pages[p.name] = files
        return pages

    def test_every_referenced_script_exists(self):
        missing = [f.name for files in self.loaded_per_page().values()
                   for f in files if not f.is_file()]
        self.assertEqual(sorted(set(missing)), [], f"模板引用了不存在的脚本: {missing}")

    def test_no_top_level_name_collision_within_a_page(self):
        pages = self.loaded_per_page()
        self.assertTrue(pages, "没有解析到任何页面加载脚本")
        problems: List[str] = []
        for page, files in pages.items():
            owner: Dict[str, str] = {}
            for f in files:
                if not f.is_file():
                    continue
                for name in sorted(_top_level_names(f.read_text(encoding="utf-8"))):
                    if name in owner and owner[name] != f.name:
                        problems.append(f"{page}: {name} 同时声明于 {owner[name]} 与 {f.name}")
                    else:
                        owner[name] = f.name
        self.assertEqual(problems, [],
                         "顶层同名声明会让后加载的文件整个不执行：\n  " + "\n  ".join(problems))

    def test_scope_extraction_is_not_vacuous(self):
        """启发式失效时必须失败，而不是静默通过。"""
        total = sum(len(_top_level_names(f.read_text(encoding="utf-8")))
                    for f in self.scripts())
        # 实测约 60 个；留出余量，但明显偏少即说明启发式已失效
        self.assertGreaterEqual(total, 50,
                                f"只提取到 {total} 个顶层声明，启发式可能已失效")
        theme = _top_level_names(
            (_ROOT / "assets" / "js" / "theme.js").read_text(encoding="utf-8"))
        for expect in ("THEME_STORAGE_KEY", "themeToggleBtn", "initTheme", "applyTheme"):
            with self.subTest(name=expect):
                self.assertIn(expect, theme)

    def test_theme_ownership_is_single(self):
        """主题设置只应由 theme.js 负责。

        history.js / faq.js 原先各自带一段「读 localStorage 后设 data-theme」的代码：
        既与 theme.js 重名（见上），又在 theme.js 之后执行、把主题覆盖回本地保存值，
        导致 ?theme= 与深色模式在那些页面失效。
        """
        offenders: List[str] = []
        for f in self.scripts():
            if f.name == "theme.js":
                continue
            text = f.read_text(encoding="utf-8")
            code = _strip_js_comments(text)
            if 'setAttribute("data-theme"' in code or "data-theme" in code:
                offenders.append(f.name)
        self.assertEqual(offenders, [],
                         f"主题只应由 theme.js 设置，以下文件也在改 data-theme: {offenders}")


class WebUiBaselineCase(unittest.TestCase):
    """通过子进程真跑 create_app()，收集事实后逐项断言。"""

    probe: Dict[str, Any] = {}
    home: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.home = _make_app_home()
        env = dict(os.environ)
        env["APP_HOME"] = str(cls.home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = os.pathsep.join([str(_ROOT), str(_SRC), str(_SUPPORT)])
        env.pop("FNMB_DISABLE_AUTH", None)
        env.pop("FNMB_DISABLE_AUTH_ACK", None)
        code = textwrap.dedent(PROBE).replace(
            "ANON_PATHS", repr(sorted(ANON_EXPECTED))
        ).replace("PUBLIC_PATHS", repr(sorted(PUBLIC_EXPECTED))).replace(
            "ADMIN_PATHS", repr(sorted(ANON_EXPECTED) + ["/history", "/faq"])
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(_ROOT), timeout=120,
        )
        marker = "__PROBE__"
        if proc.returncode != 0 or marker not in (proc.stdout or ""):
            raise AssertionError(
                f"探针失败 returncode={proc.returncode}\n"
                f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-3000:]}"
            )
        cls.probe = json.loads(proc.stdout.split(marker, 1)[1].strip())
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)

    # ── 路由表 ──
    def test_routes_still_registered(self):
        actual = {(m, r) for m, r in self.probe["routes"]}
        missing = EXPECTED_ROUTES - actual
        self.assertEqual(missing, set(), f"重构后这些路由不存在了: {sorted(missing)}")

    def test_no_route_path_load_bearing_changes(self):
        """同名路由的路径不应被改动（前端 JS 与文档都依赖这些地址）。"""
        paths = {r for _m, r in self.probe["routes"]}
        for expected in ("/api/config", "/api/save-config", "/api/test", "/api/push-stats"):
            self.assertIn(expected, paths)

    # ── 鉴权闸门 ──
    def test_protected_apis_reject_anonymous(self):
        for path, want in ANON_EXPECTED.items():
            with self.subTest(path=path):
                got = self.probe["anon"][path]["status"]
                self.assertEqual(got, want, f"{path} 未登录应返回 {want}，实际 {got}")

    def test_protected_page_redirects_to_home(self):
        r = self.probe["anon"]["/history"]
        self.assertEqual(r["status"], 302)
        self.assertTrue(r["location"].endswith("/"), f"应跳回首页，实际 {r['location']!r}")

    def test_public_paths_remain_public(self):
        for path, want in PUBLIC_EXPECTED.items():
            with self.subTest(path=path):
                self.assertEqual(self.probe["public"][path]["status"], want)

    def test_admin_gateway_can_read_config(self):
        self.assertEqual(self.probe["admin"]["/api/config"]["status"], 200)

    def test_post_requires_json(self):
        """写接口只收 JSON，否则跨站表单可用 text/plain 绕过预检。"""
        self.assertEqual(self.probe["non_json_post"]["status"], 415)

    # ── 密钥打码 ──
    def test_config_api_does_not_leak_secrets(self):
        text = self.probe["config_text"]
        self.assertNotIn(REAL_WECHAT_KEY, text, "/api/config 泄漏了企业微信密钥")

    def test_masked_channels_are_returned(self):
        chans = self.probe["channels_from_api"]
        self.assertIsInstance(chans, list)
        self.assertTrue(chans, "应至少返回一个渠道")
        self.assertTrue(
            any("****" in json.dumps(c, ensure_ascii=False) for c in chans),
            "渠道里应出现打码值（******）",
        )

    def test_saving_masked_value_keeps_real_secret(self):
        """最高风险路径：把读到的打码值原样保存，绝不能把真密钥覆盖成 ******。"""
        self.assertEqual(self.probe["save"]["status"], 200,
                         f"保存失败: {self.probe['save']['text'][:300]}")
        saved = self.probe["saved_wechat"]
        self.assertIn(REAL_WECHAT_KEY, saved,
                      f"企业微信密钥被打码值覆盖了，实际落盘: {saved!r}")
        self.assertNotIn("******", saved)

    # ── 模板上下文 ──
    def test_templates_get_the_variables_they_reference(self):
        """模板引用的每个变量都必须由视图提供——重构最容易漏的就是这个。"""
        contexts = {r["template"]: set(r["context"]) for r in self.probe["renders"]}
        for name, html in ((p.name, p.read_text(encoding="utf-8"))
                           for p in sorted(_TEMPLATES.glob("*.html"))):
            if name not in contexts:
                continue
            clean = _strip_code_blocks(html)
            needed = _referenced_vars(clean) - _declared_vars(clean)
            missing = sorted(needed - contexts[name])
            with self.subTest(template=name):
                self.assertEqual(missing, [],
                                 f"{name} 引用了视图未提供的变量: {missing}")

    def test_expected_pages_render(self):
        rendered = {r["template"] for r in self.probe["renders"]}
        self.assertEqual(rendered, {"index.html", "faq.html"})

    def test_history_page_redirects_to_overview(self):
        """/history 保留为 302 跳转，旧书签不会变成 404，也不再渲染独立页面。"""
        r = self.probe["history_redirect"]
        self.assertEqual(r["status"], 302)
        self.assertIn("tab=overview", r["location"])
        self.assertNotIn("history.html", r["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
