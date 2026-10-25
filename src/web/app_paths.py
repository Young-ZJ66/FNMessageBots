"""
Web 应用路径与资源常量。
"""

from __future__ import annotations

import os
from pathlib import Path


def get_base_dir() -> Path:
    """可变数据根目录（config/、data/）：Docker 下用 /app，本地调试用项目
    根目录（含 config 的目录）。FPK native 安装时指向 var 数据目录。"""
    app_home = os.getenv("APP_HOME")
    if app_home:
        return Path(app_home)
    candidate = Path(__file__).resolve().parent.parent.parent
    if (candidate / "config").exists():
        return candidate
    return Path("/app")


BASE_DIR = get_base_dir()

# assets 随代码分发，固定取代码树根（src/web/ 的上两级），不随 APP_HOME：
# FPK native 安装布局为 cmd/fnmessagebots/{src,assets}，而 APP_HOME 指向
# var 数据目录——若从 APP_HOME 找 assets，配置页所有静态文件都会 404。
CODE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BASE_DIR / "config" / "config.json"
ICON_FILE = CODE_DIR / "assets" / "icons" / "app-icon.png"
GITHUB_ICON_FILE = CODE_DIR / "assets" / "icons" / "github.svg"

# 项目地址（页脚与文档链接用）。换仓库只需改这一处，或用环境变量 FNMB_REPO 覆盖。
# 格式：owner/repo
PROJECT_REPO = os.getenv("FNMB_REPO", "Young-ZJ66/FNMessageBots").strip().strip("/")
PROJECT_REPO_URL = f"https://github.com/{PROJECT_REPO}"
PROJECT_DOCS_URL = f"{PROJECT_REPO_URL}/blob/main/docs/notification-channels.md"
# 页脚文案：上游署名的版权声明保留在 LICENSE（MIT 要求保留，见该文件）
FOOTER_TEXT = "FnMessageBot · MIT License"

