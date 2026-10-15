#!/usr/bin/env python3
"""本地预览前端（真实模板 + 真实 Flask，不构建镜像）。

用途：改完前端想立刻看效果，不必构建镜像、也不必等 GitHub Actions。
它加载的就是仓库里的 src/web/templates 与 assets/，与容器内渲染的是同一套。

与 _render.py 的分工：
    _render.py    无头浏览器批量截图，用于回归对比（跑完即退出）
    本脚本        起一个常驻服务，自己在浏览器里点（可交互、可开 DevTools 手机模拟）

用法
    python design-previews/serve_preview.py                 # 127.0.0.1:18080，已关鉴权
    python design-previews/serve_preview.py --port 18081
    python design-previews/serve_preview.py --auth          # 打开鉴权（试登录流程）
    python design-previews/serve_preview.py --host 0.0.0.0  # 局域网可访问（注意安全）

说明
- 真实 Flask 从 tests/_tmp/_flask 加载（不进仓库、不进镜像）；缺失时会提示安装命令。
- docker/psutil/croniter 用 tests/_support 的桩替代，前端预览用不到它们。
- 数据目录是 .preview/（已 gitignore），初始为空：因此推送记录为空、统计为 0，
  渠道列表是内置默认值。想要真实数据，把 NAS 上的 config 与 data 拷进来即可。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_SUPPORT = _ROOT / "tests" / "_support"
_FLASK_DIR = _ROOT / "tests" / "_tmp" / "_flask"
_PREVIEW = _ROOT / ".preview"

BANNER_TABS = [
    ("概览", "/?tab=overview"),
    ("推送渠道", "/?tab=channels"),
    ("监控事件", "/?tab=events"),
    ("通知策略", "/?tab=policy"),
    ("定时巡检", "/?tab=patrol"),
    ("推送记录", "/history"),
    ("常见问题", "/faq"),
]


def _ensure_flask() -> None:
    """真 Flask 必须先进 sys.modules，stub_deps 才会跳过它。"""
    if _FLASK_DIR.is_dir():
        sys.path.insert(0, str(_FLASK_DIR))
    try:
        import flask  # noqa: F401
    except ImportError:
        print("缺少真实 Flask（预览会用桩，渲染不出真实页面）。")
        print("安装：")
        print(f'  "{sys.executable}" -m pip install --target "{_FLASK_DIR}" '
              f'--proxy http://127.0.0.1:7890 flask')
        raise SystemExit(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--auth", action="store_true",
                    help="保留访问密码校验（默认关闭，便于直接看页面）")
    ap.add_argument("--fresh", action="store_true", help="清空 .preview 数据目录后启动")
    args = ap.parse_args()

    if args.fresh and _PREVIEW.exists():
        shutil.rmtree(_PREVIEW, ignore_errors=True)

    home = _PREVIEW
    for sub in ("config", "data/logs", "data/cursor"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    cfg = home / "config" / "config.json"
    if not cfg.exists():
        cfg.write_text("{}", encoding="utf-8")
    # 前端要用的图标与字体都在 assets 下，复制一份到应用目录
    shutil.copytree(_ROOT / "assets", home / "assets", dirs_exist_ok=True)

    _ensure_flask()
    sys.path[:0] = [str(_ROOT), str(_SRC), str(_SUPPORT)]
    import stub_deps
    stub_deps.install()

    os.environ["APP_HOME"] = str(home)
    os.environ["UI_PORT"] = str(args.port)
    if not args.auth:
        # 两个变量都要给：只看一个时会认为「未确认关闭鉴权」而仍然拦
        os.environ["FNMB_DISABLE_AUTH"] = "1"
        os.environ["FNMB_DISABLE_AUTH_ACK"] = "1"

    from web import ui_app

    app = ui_app.create_app()
    base = f"http://{args.host}:{args.port}"
    print()
    print("=" * 62)
    print("  FnMessageBot 前端预览（真实模板，未构建镜像）")
    print("=" * 62)
    print(f"  数据目录 : {home}")
    print(f"  鉴权     : {'开启（首次访问需设置访问密码）' if args.auth else '关闭'}")
    print()
    for label, path in BANNER_TABS:
        print(f"    {label:<8} {base}{path}")
    print()
    print("  手机端效果：浏览器按 F12 → 设备模拟 → 选 375/430 宽度。")
    print("  注意别用窗口拖窄来代替：Windows 上 Chrome 有最小窗口宽度（约 510px），")
    print("  拖到 430 实际仍是 ~510px 布局，右侧被裁，看着像布局坏了。")
    print()
    print("  Ctrl+C 停止")
    print("=" * 62)
    print()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
