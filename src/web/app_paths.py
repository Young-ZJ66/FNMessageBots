"""
Web 应用路径与资源常量。
"""

from __future__ import annotations

import os
from pathlib import Path


def get_base_dir() -> Path:
    """Docker 下用 /app，本地调试用项目根目录（含 config 的目录）。"""
    app_home = os.getenv("APP_HOME")
    if app_home:
        return Path(app_home)
    candidate = Path(__file__).resolve().parent.parent.parent
    if (candidate / "config").exists():
        return candidate
    return Path("/app")


BASE_DIR = get_base_dir()
CONFIG_FILE = BASE_DIR / "config" / "config.json"
ICON_FILE = BASE_DIR / "assets" / "icons" / "app-icon.png"
GITHUB_ICON_FILE = BASE_DIR / "assets" / "icons" / "github.svg"

# 项目地址（页脚与文档链接用）。换仓库只需改这一处，或用环境变量 FNMB_REPO 覆盖。
# 格式：owner/repo
PROJECT_REPO = os.getenv("FNMB_REPO", "Young-ZJ66/FNMessageBots").strip().strip("/")
PROJECT_REPO_URL = f"https://github.com/{PROJECT_REPO}"
PROJECT_DOCS_URL = f"{PROJECT_REPO_URL}/blob/main/docs/notification-channels.md"
# 页脚文案：上游署名的版权声明保留在 LICENSE（MIT 要求保留，见该文件）
FOOTER_TEXT = "FnMessageBot · MIT License"

