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
    ("/history", "history"),
    ("/faq", "faq"),
]

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
    ap.add_argument("outdir")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=2400)
    # 真实模板目前只用 localStorage 记主题、不认 ?theme=，所以默认只截亮色。
    # 重构中会给模板补上 ?theme= 支持（便于分享链接，也便于这个工具截图），
    # 之后即可用 --themes light,dark 同时验证深色。
    ap.add_argument("--themes", default="light")
    args = ap.parse_args()
    themes = [s.strip() for s in args.themes.split(",") if s.strip()]

    if not (_FLASK_DIR / "flask").is_dir():
        print(f"缺少真 Flask：请先执行\n"
              f"  python -m pip install --target {_FLASK_DIR} "
              f'--proxy http://127.0.0.1:7890 flask')
        return 2

    outdir = Path(args.outdir).resolve()
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
    shots = []
    for path, name in PAGES:
        for theme in themes:
            png = outdir / f"{name}-{theme}.png"
            url = f"http://127.0.0.1:{port}{path}"
            # 主题通过 URL 参数驱动（模板已支持 ?theme=dark 时生效；否则用 localStorage 预置）
            if theme == "dark":
                url += ("&theme=dark" if "?" in url else "?theme=dark")
            subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                 "--hide-scrollbars", f"--user-data-dir={profile}",
                 f"--window-size={args.width},{args.height}",
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
