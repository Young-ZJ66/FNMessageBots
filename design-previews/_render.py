#!/usr/bin/env python3
"""渲染真实模板并截图——用于 UI 重构前后的视觉对比。

为什么需要它：本项目测试套件保持纯标准库（用桩替代 flask/jinja2），
所以 `tests/` 里既不能真渲染模板，也无法目视检查。但重构 UI 时
「样式有没有丢」这件事只能靠看。

做法
- 临时用 pip 把真 Flask/Jinja2 装到 tests/_tmp/_flask（不进仓库、不进镜像）。
- 先 import 真 flask，再调用 stub_deps.install()——它会跳过已在 sys.modules 的模块，
  于是 flask 是真的、requests/docker/psutil/croniter 仍是桩。
- 用隔离的 APP_HOME 与一份内容完整的 config.json 启动应用（关闭鉴权，仅本机临时用）。
- Chrome 无头模式截 /、/history、/faq 的明暗两套。

用法（项目根目录）
    python design-previews/_render.py <输出目录> [--width 1500]

依赖：tests/_tmp/_flask 下已安装 flask（见 README 或 --install 选项）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_TESTS = _ROOT / "tests"
_SUPPORT = _TESTS / "_support"
_FLASK_DIR = _TESTS / "_tmp" / "_flask"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PAGES = [
    ("/", "index"),
    ("/?tab=channels", "index-channels"),
    ("/?tab=events", "index-events"),
    ("/?tab=policy", "index-policy"),
    ("/?tab=patrol", "index-patrol"),
    ("/faq", "faq"),
    # /history 现在是 302 跳转到概览：Chrome 会跟随，截图即概览页，
    # 留一项是为了确认跳转确实落到了正确的位置
    ("/history", "history-redirect"),
]

# 文档级横向溢出探针。放在应用的 /assets/ 下加载，于是与目标页同源，
# 可以直接读 iframe 内的 documentElement——不需要往生产模板里塞测试代码。
# 一次加载即可测多个「页面 × 宽度」组合（iframe 的宽度就是该页的视口宽度），
# 比反复起浏览器+调窗口尺寸快得多，也更准。
# 精确宽度截图wrapper。
# 为什么需要它：Chrome 的 --window-size 在 Windows 上存在最小窗口宽度（约 510px），
# 传 430 会得到约 510px 的布局视口，而 PNG 只有 430 宽——右侧内容被裁掉。
# 曾因此把「~510px 被裁的图」误当成 430px 的移动端效果。
# 在指定宽度的 iframe 内渲染，才真正得到该宽度的布局。
SHOT_WRAPPER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>shot</title>
<style>html,body{margin:0;padding:0;overflow:hidden}iframe{border:0;display:block}</style>
</head><body>
<script>
(function () {
  var q = new URLSearchParams(location.search);
  var u = q.get("u") || "/";
  var w = parseInt(q.get("w") || "430", 10);
  var h = parseInt(q.get("h") || "900", 10);
  var f = document.createElement("iframe");
  f.style.width = w + "px";
  f.style.height = h + "px";
  f.src = u;
  f.onload = function () {
    if (q.get("dirty") === "1") {
      try {
        var d = f.contentDocument;
        var el = d.querySelector("#input-log-days") || d.querySelector("#input-title-prefix");
        if (el) {
          el.dispatchEvent(new f.contentWindow.Event("input", {bubbles: true}));
          el.dispatchEvent(new f.contentWindow.Event("change", {bubbles: true}));
        }
      } catch (e) {}
    }
  };
  document.body.appendChild(f);
})();
</script>
</body></html>
"""

OVERFLOW_PROBE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>overflow probe</title>
<style>iframe{border:0;display:block;margin:0 0 4px}</style></head>
<body>
<script>
function inScrollable(el) {
  var p = el.parentElement;
  while (p && p !== el.ownerDocument.body) {
    var ox = p.ownerDocument.defaultView.getComputedStyle(p).overflowX;
    if (ox === "auto" || ox === "scroll") return true;
    p = p.parentElement;
  }
  return false;
}
function measure(path, width) {
  return new Promise(function (resolve) {
    var f = document.createElement("iframe");
    f.style.width = width + "px";
    f.style.height = "1000px";
    f.src = path;
    f.onload = function () {
      setTimeout(function () {
        try {
          var d = f.contentDocument, de = d.documentElement;
          // 触发一次「已修改」：否则顶栏不会显示未保存提示，
          // 而提示出现后顶栏可能装不下、把保存按钮挤出屏幕。
          // 这是真实的用户路径（在手机上改一个字段），探针必须覆盖。
          var trigger = d.querySelector("#input-log-days") ||
                        d.querySelector("#input-title-prefix");
          if (trigger) {
            trigger.dispatchEvent(new f.contentWindow.Event("input", {bubbles: true}));
            trigger.dispatchEvent(new f.contentWindow.Event("change", {bubbles: true}));
          }
          var over = [];
          var rawMax = 0, rawSel = "";
          d.querySelectorAll("body *").forEach(function (el) {
            var r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return;
            // 不加过滤的最大右边界：用于发现「被 inScrollable 跳过」的盲区。
            // 曾经只统计过滤后的元素，于是 .table-wrap（overflow-x:auto）内部的
            // 溢出完全看不见，探针报 ok 而截图明显被裁。
            if (r.right > rawMax) {
              rawMax = r.right;
              var c0 = "";
              if (el.className && typeof el.className === "string") {
                c0 = "." + el.className.trim().split(/\\s+/).join(".");
              }
              rawSel = el.tagName.toLowerCase() + c0;
            }
            if (r.right > de.clientWidth + 1 && !inScrollable(el)) {
              var cls = "";
              if (el.className && typeof el.className === "string") {
                cls = "." + el.className.trim().split(/\\s+/).join(".");
              }
              over.push({sel: el.tagName.toLowerCase() + cls,
                         w: Math.round(r.width),
                         right: Math.round(r.right),
                         text: (el.textContent || "").trim().slice(0, 48)});
            }
          });
          var tbl = d.querySelector(".channels-table");
          var tinfo = null, tableFits = null;
          if (tbl) {
            var cs = f.contentWindow.getComputedStyle(tbl);
            var tr0 = tbl.getBoundingClientRect();
            tinfo = {display: cs.display, width: Math.round(tr0.width),
                     right: Math.round(tr0.right)};
            // 本任务的验收点：表格放得进它自己的容器，就不必横向滚动去点开关/删除。
            // 只靠「是否在可滚动容器内」判断会漏掉这种情况——
            // 表格宽出容器时它正是该容器的滚动内容，看起来"合法"。
            var w0 = tbl.parentElement;
            if (w0) tableFits = tr0.width <= w0.getBoundingClientRect().width + 1;
          }
          var wrap = d.querySelector(".table-wrap");
          var winfo = null;
          if (wrap) {
            var wr = wrap.getBoundingClientRect();
            winfo = {w: Math.round(wr.width), right: Math.round(wr.right),
                     ox: f.contentWindow.getComputedStyle(wrap).overflowX};
          }
          var tb = d.querySelector(".topbar");
          var tbr = tb ? tb.getBoundingClientRect() : null;
          // 逐个报出顶栏与卡片关键元素的矩形，用于定位被裁的具体元素
          var probes = {};
          [".topbar", ".save-bar", "#save-btn", "#theme-toggle-btn", ".main-body",
           ".channel-ops-td", ".channel-ops", ".channel-type-td",
           ".channel-url-td", ".channels-table", ".table-wrap",
           ".save-dirty-hint", ".save-bar.is-dirty"].forEach(function (sel) {
            var el = d.querySelector(sel);
            if (!el) return;
            var rr = el.getBoundingClientRect();
            probes[sel] = [Math.round(rr.left), Math.round(rr.right),
                           Math.round(rr.width)];
          });
          resolve({path: path, width: width, scrollWidth: de.scrollWidth,
                   clientWidth: de.clientWidth,
                   innerWidth: f.contentWindow.innerWidth,
                   mq768: f.contentWindow.matchMedia("(max-width: 768px)").matches,
                   table: tinfo, tableWrap: winfo, tableFits: tableFits,
                   topbar: tbr ? {w: Math.round(tbr.width), right: Math.round(tbr.right)} : null,
                   probes: probes,
                   computed: (function () {
                     var out = {};
                     ["#theme-toggle-btn", ".save-dirty-hint", ".save-bar",
                      ".topbar-title"].forEach(function (sel) {
                       var el = d.querySelector(sel);
                       if (!el) { out[sel] = null; return; }
                       var cs = f.contentWindow.getComputedStyle(el);
                       out[sel] = {display: cs.display, width: cs.width,
                                   minWidth: cs.minWidth, height: cs.height,
                                   whiteSpace: cs.whiteSpace, fontSize: cs.fontSize,
                                   padding: cs.padding, fontWeight: cs.fontWeight};
                     });
                     return out;
                   })(),
                   count: over.length,
                   rawMax: Math.round(rawMax), rawSel: rawSel,
                   sample: over.slice(0, 4)});
        } catch (e) {
          resolve({path: path, width: width, error: String(e)});
        }
      }, 350);
    };
    document.body.appendChild(f);
  });
}
(function () {
  var q = new URLSearchParams(location.search);
  var paths = (q.get("paths") || "/").split(",");
  var widths = (q.get("widths") || "430").split(",").map(Number);
  var out = [];
  (function next(i) {
    if (i >= paths.length * widths.length) {
      var d = document.createElement("div");
      d.id = "overflow-report";
      d.setAttribute("data-report", JSON.stringify(out));
      document.body.appendChild(d);
      return;
    }
    var p = paths[Math.floor(i / widths.length)];
    var w = widths[i % widths.length];
    measure(p, w).then(function (r) { out.push(r); next(i + 1); });
  })(0);
})();
</script>
</body></html>
"""

# 内容尽量覆盖各组件：多种状态的渠道行、开关的开/关、各分类事件
DEMO_CONFIG = {
    "title_prefix": "飞牛NAS",
    "wechat_webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91c2d4e5f6a7b8c9d0e1f2a3b4c5",
    "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=d1f8a0b2c3d4e5f6a7b8c9d0e1f2a3b4c5",
    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/2c9e41d5e6f7a8b9c0d1e2f3a4b5c6d7",
    "bark_url": "https://api.day.app/7Kq2mNp9Rt",
    "smtp_params": json.dumps({
        "server": "smtp.qq.com", "port": 465, "username": "1600386893@qq.com",
        "password": "demo-password", "to": "1600386893@qq.com",
    }),
    "meow_params": json.dumps({"nickname": "youngzj"}),
    "monitor_events": ["LoginSucc", "LoginFail", "SSH_AUTH_FAILED", "DISK_IO_ERR",
                       "UPS_ONBATT", "APP_CRASH"],
    "log_level": "INFO",
    "log_dir": "./data/logs",
    "cursor_dir": "./data/cursor",
    "dnd_enabled": False,
    "dnd_start_time": "22:00",
    "dnd_end_time": "07:00",
    "nas_patrol_enabled": True,
    "nas_patrol_cron": "0 9 * * *",
    "poll_batch_summary_enabled": True,
    "media_batch_summary_enabled": True,
    "push_markdown_enabled": True,
    "push_emoji_enabled": False,
    "ssh_ignore_loopback": False,
    "minimal_push_enabled": False,
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_app_home() -> Path:
    home = _TESTS / "_tmp" / f"render_{uuid.uuid4().hex[:8]}"
    for d in ("config", "data/logs", "data/cursor"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / "config" / "config.json").write_text(
        json.dumps(DEMO_CONFIG, ensure_ascii=False), encoding="utf-8")
    # app_paths 用 APP_HOME 作为 BASE_DIR，静态资源取自 BASE_DIR/assets。
    # 不把真实 assets 放进去的话图标与字体全会 404——截图就不可信了。
    shutil.copytree(_ROOT / "assets", home / "assets", dirs_exist_ok=True)
    return home


def _find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    raise SystemExit("未找到 Chrome/Edge")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir", nargs="?")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=2400)
    # 真实模板目前只用 localStorage 记主题、不认 ?theme=，所以默认只截亮色。
    # 重构中会给模板补上 ?theme= 支持（便于分享链接，也便于这个工具截图），
    # 之后即可用 --themes light,dark 同时验证深色。
    ap.add_argument("--themes", default="light")
    ap.add_argument("--exact-width", type=int, default=0,
                    help="在指定宽度的 iframe 内渲染再截图（绕开 Chrome 的最小窗口宽度）")
    ap.add_argument("--dirty", action="store_true",
                    help="截图前触发一次输入事件，模拟「有未保存更改」状态")
    ap.add_argument("--verbose", action="store_true",
                    help="--overflow 模式下同时打印每个组合的关键元素矩形")
    ap.add_argument("--overflow", action="store_true",
                    help="只测文档级横向溢出（不截图）")
    ap.add_argument("--widths", default="430,600,768,1024",
                    help="--overflow 模式下要测的视口宽度")
    args = ap.parse_args()
    themes = [s.strip() for s in args.themes.split(",") if s.strip()]

    if not (_FLASK_DIR / "flask").is_dir():
        print(f"缺少真 Flask：请先执行\n"
              f"  python -m pip install --target {_FLASK_DIR} "
              f'--proxy http://127.0.0.1:7890 flask')
        return 2

    # --overflow 模式不截图，因此不要求 outdir
    outdir = Path(args.outdir).resolve() if args.outdir else None
    if not args.overflow and outdir is None:
        print("需要输出目录：python design-previews/_render.py <输出目录> [--themes ...]")
        return 2
    if outdir is not None:
        outdir.mkdir(parents=True, exist_ok=True)
    chrome = _find_chrome()
    home = _make_app_home()

    # 顺序关键：真 flask 必须先进 sys.modules，stub_deps 才会跳过它
    sys.path.insert(0, str(_FLASK_DIR))
    import flask  # noqa: F401
    sys.path[:0] = [str(_ROOT), str(_SRC), str(_SUPPORT)]
    import stub_deps
    stub_deps.install()

    os.environ["APP_HOME"] = str(home)
    os.environ["FNMB_DISABLE_AUTH"] = "1"
    os.environ["FNMB_DISABLE_AUTH_ACK"] = "1"
    os.environ["FNMB_REPO"] = os.environ.get("FNMB_REPO", "Young-ZJ66/FNMessageBots")

    from web import ui_app

    app = ui_app.create_app()
    port = _free_port()
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True, debug=False),
        daemon=True)
    t.start()

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                break
        except OSError:
            time.sleep(0.2)
    else:
        print("服务未在 15s 内就绪")
        return 3

    profile = _TESTS / "_tmp" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)

    if args.overflow:
        # 探针放在应用自己的 /assets/ 下，于是与目标页同源，可直接读 iframe 内部
        probe = home / "assets" / "_overflow_probe.html"
        probe.write_text(OVERFLOW_PROBE_HTML, encoding="utf-8")
        widths = [w.strip() for w in args.widths.split(",") if w.strip()]
        paths = [p for p in ("/", "/?tab=channels", "/?tab=events", "/?tab=policy",
                             "/?tab=patrol", "/faq")]
        import json as _json
        url = (f"http://127.0.0.1:{port}/assets/_overflow_probe.html"
               f"?paths={','.join(paths)}&widths={','.join(widths)}")
        proc = subprocess.run(
            # --hide-scrollbars 必须与截图一致：否则 iframe 里多出一条竖直滚动条，
            # clientWidth 会比截图条件小 15px，两边的判定就对不上了。
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--hide-scrollbars", f"--user-data-dir={profile}",
             "--window-size=1200,900",
             "--virtual-time-budget=60000", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        m = re.search(r'data-report="(.*?)"', proc.stdout or "", re.S)
        if not m:
            print("未取到溢出报告；探针页面可能未加载完成")
            print((proc.stdout or "")[-500:])
            shutil.rmtree(home, ignore_errors=True)
            return 4
        rows = _json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
        header = f"{'页面':<16}" + "".join(f"{w + 'px':>10}" for w in widths)
        print(header)
        print("-" * len(header))
        bad = 0
        for p in paths:
            cells = []
            for w in widths:
                r = next((x for x in rows if x.get("path") == p and str(x.get("width")) == w), None)
                if not r or r.get("error"):
                    cells.append(f"{'?':>10}")
                elif (r["scrollWidth"] > r["clientWidth"] + 1 or r["count"]
                      or r.get("tableFits") is False):
                    cells.append(f"{'FAIL':>10}")
                    bad += 1
                    why = []
                    if r["scrollWidth"] > r["clientWidth"] + 1:
                        why.append(f"文档横向溢出 scroll={r['scrollWidth']}"
                                   f">client={r['clientWidth']}")
                    if r["count"]:
                        why.append(f"内容被裁 {r['count']} 处 {r.get('sample')}")
                    if r.get("tableFits") is False:
                        why.append(f"渠道表格放不进容器 "
                                   f"table={r['table']['width']} "
                                   f"wrap={r['tableWrap']['w']}")
                    print(f"    ! {r['path']} @ {r['width']}px " + "；".join(why))
                else:
                    cells.append(f"{'ok':>10}")
            print(f"{p:<16}" + "".join(cells))
        if args.verbose:
            print()
            for x in rows:
                if x.get("probes"):
                    print(f"  {x['path']} @ {x['width']}px innerWidth={x.get('innerWidth')} "
                          f"client={x['clientWidth']} scroll={x['scrollWidth']}")
                    for k, v in sorted(x["probes"].items()):
                        flag = "  <== 越界" if v[1] > x["clientWidth"] else ""
                        print(f"      {k:<22} left={v[0]:>5} right={v[1]:>5} w={v[2]:>5}{flag}")

        detail = [x for x in rows
                  if x.get("count") or x.get("tableFits") is False
                  or (x.get("scrollWidth", 0) > x.get("clientWidth", 0) + 1)]
        if detail:
            print()
            print("溢出明细：")
            for x in detail[:12]:
                print(f"  {x['path']} @ {x['width']}px  scroll={x['scrollWidth']} "
                      f"client={x['clientWidth']}")
                print(f"      innerWidth={x.get('innerWidth')} "
                      f"mq(max-width:768px)={x.get('mq768')}")
                print(f"      topbar={x.get('topbar')} table={x.get('table')} "
                      f"tableWrap={x.get('tableWrap')} 最右={x.get('rawSel')}@{x.get('rawMax')}")
                for k, v in sorted((x.get("computed") or {}).items()):
                    print(f"        computed {k}: {v}")
                for k, v in sorted((x.get("probes") or {}).items()):
                    print(f"        {k:<22} left={v[0]:>5} right={v[1]:>5} w={v[2]:>5}")
                for o in (x.get("sample") or [])[:4]:
                    if isinstance(o, dict):
                        print(f"      {o['sel']}  宽 {o['w']}  右边界 {o['right']}"
                              f"  文本 {o['text']!r}")
                    else:
                        print(f"      {o}")
        shutil.rmtree(home, ignore_errors=True)
        print()
        print("结论：" + ("全部无横向溢出" if bad == 0 else f"{bad} 项存在横向溢出"))
        return 0 if bad == 0 else 1

    if args.exact_width:
        (home / "assets" / "_shot.html").write_text(SHOT_WRAPPER_HTML, encoding="utf-8")

    shots = []
    for path, name in PAGES:
        for theme in themes:
            png = outdir / f"{name}-{theme}.png"
            if args.exact_width:
                # 在 iframe 内渲染：PNG 尺寸 = window 尺寸 = iframe 尺寸
                w = args.exact_width
                h = args.height
                url = (f"http://127.0.0.1:{port}/assets/_shot.html"
                       f"?u={path}&w={w}&h={h}"
                       + ("&dirty=1" if args.dirty else ""))
                win = f"--window-size={w},{h}"
            else:
                url = f"http://127.0.0.1:{port}{path}"
                win = f"--window-size={args.width},{args.height}"
            # 主题通过 URL 参数驱动（模板已支持 ?theme=dark 时生效；否则用 localStorage 预置）
            if theme == "dark":
                url += ("&theme=dark" if "?" in url else "?theme=dark")
            subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                 "--hide-scrollbars", f"--user-data-dir={profile}",
                 win,
                 "--virtual-time-budget=5000", f"--screenshot={png}", url],
                capture_output=True, timeout=120)
            ok = png.exists()
            shots.append((png.name, png.stat().st_size if ok else 0))
            print(f"  {'ok ' if ok else '!! '}{png.name}"
                  f"{f'  {png.stat().st_size // 1024} KB' if ok else ''}")

    shutil.rmtree(home, ignore_errors=True)
    print(f"\n输出目录: {outdir}")
    missing = [n for n, s in shots if not s]
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
