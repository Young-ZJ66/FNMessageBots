from __future__ import annotations

import functools
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import sys
import threading
from pathlib import Path
from utils.value_parser import as_bool
from utils.value_parser import as_int as _as_int
from version import APP_VERSION

# 直接运行本文件时（python src/web/ui_app.py），把 src 加入 path 以便导入 notifier 等
if __name__ == "__main__":
    _repo = Path(__file__).resolve().parent.parent.parent
    _src = _repo / "src"
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from flask import Flask, g, jsonify, request, render_template, send_from_directory, abort, redirect

from notifier.channel_params import validate_channel_json
from notifier.multi_platform_notifier import MultiPlatformNotifier
from web.access_guard import GatewayPrefixMiddleware
from web.access_guard import LoginRateLimiter
from web.access_guard import SetupCodeStore
from web.access_guard import StripGatewayHeadersMiddleware
from web.access_guard import auth_disabled_by_env as _auth_disabled_by_env
from web.access_guard import client_address
from web.access_guard import gateway_identity as _gateway_identity_from_environ
from web.access_guard import is_loopback_request as _is_loopback_request_environ
from web.auth_service import PASSWORD_MIN_LENGTH
from web.auth_service import apply_new_password as _apply_new_password
from web.auth_service import get_password_config as _get_password_config
from web.auth_service import has_password_set as _has_password_set_fn
from web.auth_service import verify_password as _verify_password
from web.api_helpers import build_notifier_from_raw as _build_notifier_from_raw
from web.api_helpers import parse_success_filter as _parse_success_filter
from web.app_paths import CODE_DIR
from web.app_paths import CONFIG_FILE
from web.app_paths import FOOTER_TEXT
from web.app_paths import GITHUB_ICON_FILE
from web.app_paths import ICON_FILE
from web.app_paths import PROJECT_DOCS_URL
from web.app_paths import PROJECT_REPO_URL
from web.config_store import channels_from_raw as _channels_from_raw
from web.config_store import config_load_error as _config_load_error
from web.config_store import load_raw_config as _load_raw_config_from_file
from web.config_store import mask_channels as _mask_channels
from web.config_store import restore_masked_channels as _restore_masked_channels

# 运行中的主监控应用，供「立即巡检」等接口使用
_runtime_monitor_app = None


def _smart_auth_state():
    """SMART 授权数据包（结构化字段，命令文本由前端拼装）；巡检模块不可用时静默降级。"""
    try:
        from monitor.patrol.smart_auth import get_state

        return get_state()
    except Exception:
        return {"needed": False, "issues": [], "devices": [], "mode": "native", "checked_at": ""}

# 内置测试消息：Web 页「发送测试消息」一键发送，无需用户输入内容
BUILT_IN_TEST_MESSAGE = "FnMessageBot 测试推送。收到本条即说明渠道配置与网络可用；实际推送样式以真实事件为准。"


def resolve_test_push_content(payload: dict | None) -> str:
    """测试推送内容：优先用户显式传入；缺省使用内置测试消息。"""
    payload = payload or {}
    return (payload.get("content") or "").strip() or BUILT_IN_TEST_MESSAGE


def set_runtime_monitor_app(app) -> None:
    global _runtime_monitor_app
    _runtime_monitor_app = app


def get_runtime_monitor_app():
    return _runtime_monitor_app
from web.config_store import normalize_push_channels as _normalize_push_channels
from web.config_store import save_raw_config as _save_raw_config_to_file
from web.config_store import sync_channel_flat_keys as _sync_channel_flat_keys
from web.config_store import title_prefix_from_dict as _title_prefix_from_dict
from web.event_catalog import APP_LIFECYCLE_EVENTS
from web.event_catalog import DEFAULT_SELECTED_EVENTS
from web.event_catalog import EVENT_IDS_HIDDEN_IN_UI
from web.event_catalog import OLD_DEFAULT_SELECTED_EVENTS_WITH_EXTRA
from web.event_catalog import build_events_for_ui
from config_db_paths import apply_discovered_db_paths
from monitor.docker_events_poller import DOCKER_POLL_EVENTS
from monitor.docker_socket_access import check_docker_socket_access
from monitor.ssh_journal_poller import SSH_JOURNAL_EVENTS
from web.push_history_service import get_record as get_push_history_record
from web.push_history_service import get_stats as get_push_history_stats
from web.push_history_service import list_records as list_push_history_records
from web.session_service import create_session as _create_session
from web.session_service import delete_session as _delete_session
from web.session_service import session_remaining_seconds as _session_remaining_seconds
from web.session_service import touch_session as _touch_session
from monitor.sqlite_uri import connect_readonly_with_fallback

# 配置页密码：会话空闲超时（秒）。只在真实交互（POST）时续期，
# 统计、配置读取和身份轮询不延长登录时长——页面开着 15 分钟无操作即需重登
SESSION_IDLE_SECONDS = 15 * 60
AUTH_COOKIE_NAME = "fnmb_session"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_config_lock = threading.RLock()


def _config_transaction(fn):
    """配置「读-改-写」与设密的进程内互斥：并发保存/设密不能互相覆盖丢键。"""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _config_lock:
            return fn(*args, **kwargs)
    return wrapped


def _auth_cookie_path() -> str:
    """cookie 作用域随网关前缀：/app/<appname>/ 下与根路径部署互不串扰。"""
    return (request.script_root or "").rstrip("/") + "/"


def _session_cookie_secure() -> bool:
    """HTTPS 反代场景：设置 SESSION_COOKIE_SECURE=1/true 或为 Cookie 加 Secure。"""
    v = (os.environ.get("SESSION_COOKIE_SECURE") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return (os.environ.get("HTTPS") or "").strip() == "1"


def _auth_cookie_kwargs() -> dict:
    return {
        "max_age": SESSION_IDLE_SECONDS,
        "httponly": True,
        "samesite": "Lax",
        "path": _auth_cookie_path(),
        "secure": _session_cookie_secure(),
    }


def _has_password_set() -> bool:
    return _has_password_set_fn(_load_raw_config)


def _get_session_id_from_cookie() -> str:
    # 浏览器可能同时持有旧根路径和网关路径的同名 Cookie；按服务端作用域选有效会话。
    for sid in request.cookies.getlist(AUTH_COOKIE_NAME):
        if _session_remaining_seconds(sid, SESSION_IDLE_SECONDS, scope=_auth_cookie_path()) > 0:
            return sid
    return ""


_setup_codes = SetupCodeStore(CONFIG_FILE.parent / ".setup_code")
_login_limiter = LoginRateLimiter()


def _gateway_identity():
    return _gateway_identity_from_environ(request.environ)


def _is_authenticated() -> bool:
    """统一网关的 NAS 管理员、显式关闭鉴权的部署，或持有有效会话。

    会话检查无副作用（后台读取不续期）；续期只发生在写请求（见 _require_auth）。
    """
    # 先判断「显式关闭鉴权」：该判断无副作用（可能打印一次提示），避免每个请求都先解析网关身份
    if _auth_disabled_by_env():
        return True
    gw = _gateway_identity()
    if gw is not None:
        return bool(gw["is_admin"])
    return _session_remaining_seconds(_get_session_id_from_cookie(), SESSION_IDLE_SECONDS, scope=_auth_cookie_path()) > 0


def _session_auth_active() -> bool:
    """当前鉴权是否来自应用密码会话（只有这种才续期/滚动刷新 cookie；
    网关管理员凭 NAS 登录态、显式关闭鉴权凭环境变量，均无会话可续）。"""
    if _auth_disabled_by_env():
        return False
    return _gateway_identity() is None


def _setup_requires_code() -> bool:
    """网关管理员与本机回环请求可直接设密，其余来源需要初始化码。"""
    gw = _gateway_identity()
    if gw is not None and gw["is_admin"]:
        return False
    return not _is_loopback_request_environ(request.environ)


def _client_key() -> str:
    gw = _gateway_identity()
    return f"gateway:{gw['uid']}" if gw is not None else client_address(request.environ)


@_config_transaction
def _apply_initial_password_from_env() -> None:
    """FNMB_WEB_PASSWORD：未设密码时用作初始密码（手装 Docker 可在 compose 中预置）。"""
    pw = (os.getenv("FNMB_WEB_PASSWORD") or "").strip()
    if not pw or _has_password_set():
        return
    if len(pw) < PASSWORD_MIN_LENGTH:
        print(f"FNMB_WEB_PASSWORD 长度不足 {PASSWORD_MIN_LENGTH} 位，已忽略")
        return
    try:
        _save_raw_config(_apply_new_password(_load_raw_config(), pw))
        print("已根据 FNMB_WEB_PASSWORD 设置 Web 访问密码")
    except Exception as e:
        print(f"根据 FNMB_WEB_PASSWORD 设置密码失败：{e}")


def _load_raw_config() -> dict:
    raw = _load_raw_config_from_file(CONFIG_FILE)
    merged, _ = apply_discovered_db_paths(raw)
    return merged


def _save_raw_config(data: dict) -> None:
    _save_raw_config_to_file(CONFIG_FILE, data)


def _current_config_load_error() -> str:
    return _config_load_error(CONFIG_FILE)


def _config_load_error_payload(err: str) -> dict:
    return {
        "ok": False,
        "config_error": True,
        "message": "配置文件读取失败，请修复 config.json 后重试。",
        "detail": err,
    }


def _events_catalog_bundle():
    """重建配置页事件分类（含 VM 事件发现）；每次 API 请求调用，避免 Flask 启动时缓存旧列表。"""
    raw = _load_raw_config()
    return build_events_for_ui(
        logger_db_path=(raw.get("logger_db_path") or "").strip(),
        backup_db_path=(raw.get("backup_db_path") or "").strip(),
        trim_media_db_path=(raw.get("trim_media_db_path") or "").strip(),
        trim_activity_db_path=(raw.get("trim_activity_db_path") or "").strip(),
        photo_db_path=(raw.get("photo_db_path") or "").strip(),
        scheduler_db_path=(raw.get("scheduler_db_path") or "").strip(),
        titles=MultiPlatformNotifier.EVENT_TITLES,
        notes=MultiPlatformNotifier.EVENT_NOTES,
    )


def _mode_str(mode: int) -> str:
    return stat.filemode(mode)


def _docker_socket_access_warning(sock_path: str) -> str | None:
    """勾选 Docker 容器事件时检测 socket；详见 /faq#faq-docker-sock。"""
    return check_docker_socket_access(sock_path)


def _check_db_access_issue(db_path: str, probe_sql: str = "SELECT 1") -> str:
    """检查数据库可读性并返回可读提示；无问题时返回空字符串。

    打开已知文件只需父目录 x 权限，不要求每层目录都能列出内容；
    WAL 模式下的 -wal/-shm 附属文件也要可读，否则只读连接会失败。
    """
    path = (db_path or "").strip()
    if not path:
        return ""

    p = Path(path)
    chain = [Path("/")]
    for item in p.parts[1:-1]:
        chain.append(chain[-1] / item)

    for d in chain:
        try:
            st = d.stat()
            if not os.access(d, os.X_OK):
                return (
                    f"{path}: 目录 `{d}` 不可进入（{_mode_str(st.st_mode)}）。"
                    f" 建议按实际路径给应用账号授 ACL（见常见问题页第 12 条），或 `chown/chmod 750` 给服务用户。"
                )
        except FileNotFoundError:
            return f"{path}: 目录不存在 `{d}`。"
        except PermissionError:
            return f"{path}: 当前进程无权限访问目录 `{d}`。"
        except Exception as e:
            return f"{path}: 目录检查失败 `{d}`（{e}）。"

    try:
        st = p.stat()
        if not os.access(p, os.R_OK):
            return (
                f"{path}: 文件不可读（{_mode_str(st.st_mode)}）。"
                    f" 建议按实际路径给应用账号授 ACL（见常见问题页第 12 条），或 `chown/chmod 640` 给服务用户。"
            )
    except FileNotFoundError:
        return f"{path}数据库文件不存在。"
    except PermissionError:
        return f"{path}: 当前进程无权限访问数据库文件 `{p}`。"
    except Exception as e:
        return f"{path}: 文件检查失败 `{p}`（{e}）。"

    # WAL 附属文件：存在但不可读会让只读连接在并发写入时失败
    for suffix in ("-wal", "-shm"):
        side = Path(str(p) + suffix)
        if side.exists() and not os.access(side, os.R_OK):
            return f"{path}: WAL 附属文件 `{side.name}` 不可读，需一并授权（见常见问题页第 12 条）。"

    try:
        conn = connect_readonly_with_fallback(path, timeout=2.0, table_probe_sql=probe_sql)
        conn.close()
    except Exception as e:
        msg = str(e).lower()
        if "no such table" in msg or "no such column" in msg:
            return (
                f"{path}: 数据库结构不兼容（{e}）。加权限不能解决结构问题；"
                "请确认路径指向的是本项目预期的库文件，并核对该系统应用的数据库版本。"
            )
        if "database is locked" in msg or "database is busy" in msg:
            return f"{path}: 数据库被其他进程占用，稍后重新保存检查即可。"
        if "not a database" in msg or "malformed" in msg:
            return (
                f"{path}: 数据库文件损坏或不是 SQLite 库（{e}）。"
                "请通过对应系统应用检查、备份和恢复，不要直接删除系统库。"
            )
        return f"{path}: 路径权限看起来正常，但数据库探测失败（{e}）。"
    return ""


def _collect_external_db_access_warnings(raw_cfg: dict, events: list[str]) -> list[str]:
    """按已选择事件收集外部数据库权限/可读性告警。"""
    monitor_events = set(events or [])
    warnings: list[str] = []

    backup_events = {"BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS"}
    trimmedia_events = {"TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"}
    trimactivity_events = {"MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT"}
    photo_events = {"PHOTO_SHARE_CREATED", "PHOTO_SHARE_EXPIRED", "PHOTO_DEVICE_REGISTERED", "FACE_RECOGNITION_UPDATED"}

    if monitor_events & backup_events:
        backup_db_path = (raw_cfg.get("backup_db_path") or "").strip()
        if not backup_db_path:
            warnings.append(
                "备份库: 未找到数据库（config 中 backup_db_path 为空，且容器内未探测到 basic_backup.db3；"
                "请确认 compose 已挂载 backup_service 目录）。"
            )
        else:
            issue = _check_db_access_issue(
                backup_db_path,
                "SELECT id FROM operations ORDER BY id DESC LIMIT 1",
            )
            if issue:
                warnings.append(f"备份库: {issue}")

    if monitor_events & trimmedia_events:
        trim_media_db_path = (raw_cfg.get("trim_media_db_path") or "").strip()
        if not trim_media_db_path:
            warnings.append(
                "影视库: 未找到 trimmedia.db（请确认 compose 已挂载 trim.media/database 或填写 trim_media_db_path）。"
            )
        else:
            issue = _check_db_access_issue(
                trim_media_db_path,
                "SELECT guid FROM item LIMIT 1",
            )
            if issue:
                warnings.append(f"影视库: {issue}")

    if monitor_events & trimactivity_events:
        trim_activity_db_path = (raw_cfg.get("trim_activity_db_path") or "").strip()
        if not trim_activity_db_path:
            warnings.append(
                "影视库: 未找到 trimactivity.db（请确认 compose 已挂载 trim.media/database 或填写 trim_activity_db_path）。"
            )
        else:
            issue = _check_db_access_issue(
                trim_activity_db_path,
                "SELECT token FROM user_token LIMIT 1",
            )
            if issue:
                warnings.append(f"影视库: {issue}")

    if monitor_events & photo_events:
        photo_db_path = (raw_cfg.get("photo_db_path") or "").strip()
        if not photo_db_path:
            warnings.append(
                "相册库: 未找到 photo.db（请确认 compose 已挂载 trim.photos/db 或填写 photo_db_path）。"
            )
        else:
            issue = _check_db_access_issue(
                photo_db_path,
                "SELECT id FROM share_link LIMIT 1",
            )
            if issue:
                warnings.append(f"相册库: {issue}")

    docker_ev = set(DOCKER_POLL_EVENTS)
    if monitor_events & docker_ev:
        sock = (raw_cfg.get("docker_socket_path") or "").strip() or "/var/run/docker.sock"
        docker_warn = _docker_socket_access_warning(sock)
        if docker_warn:
            warnings.append(docker_warn)

    scheduler_events = {"SCHEDULER_TASK_SUCCESS", "SCHEDULER_TASK_FAILED", "SCHEDULER_TASK_CONDITION_FAILED"}
    if monitor_events & scheduler_events:
        scheduler_db_path = (raw_cfg.get("scheduler_db_path") or "").strip()
        if not scheduler_db_path:
            warnings.append(
                "任务计划库: 未找到 scheduler.db（请挂载 fn-scheduler 数据目录并在 config 填写 scheduler_db_path，"
                "常见为 /var/apps/fn-scheduler/var/scheduler.db）。"
            )
        else:
            issue = _check_db_access_issue(
                scheduler_db_path,
                "SELECT id FROM task_results LIMIT 1",
            )
            if issue:
                warnings.append(f"任务计划库: {issue}")

    # 主日志库：除外部源外的大部分事件都依赖它；单独勾选外部源/巡检时不要求
    external_only = {
        *backup_events, *trimmedia_events, *trimactivity_events,
        *photo_events, *scheduler_events, *docker_ev,
        "NAS_PATROL_REPORT", "WAN_IP_CHANGED", "APP_START", "APP_STOP",
    }
    if (monitor_events - external_only) or as_bool(raw_cfg.get("media_lib_logger_enabled", False), False):
        logger_db_path = (raw_cfg.get("logger_db_path") or "").strip()
        if not logger_db_path:
            warnings.append("主日志库: 未配置 logger_db_path，且自动探测未命中（一般事件将无数据来源）。")
        else:
            issue = _check_db_access_issue(
                logger_db_path,
                "SELECT id FROM log LIMIT 1",
            )
            if issue:
                warnings.append(f"主日志库: {issue}")

    # SSH journal：勾选 SSH 事件时检查读取链路（有界探测，不整卷 dump）
    ssh_events = set(SSH_JOURNAL_EVENTS)
    if monitor_events & ssh_events:
        journal_warn = _ssh_journal_access_warning()
        if journal_warn:
            warnings.append(journal_warn)

    return warnings


def _ssh_journal_access_warning() -> str:
    """journal 读取链路探测：读取权限不足时给出组授权/挂载提示（详见 FAQ 第 10 条）。"""
    try:
        from monitor.patrol.probes import probe_journal_readability

        return probe_journal_readability()
    except Exception:
        return ""


def create_app(on_config_saved=None) -> Flask:
    """创建 Flask 应用。on_config_saved: 保存配置成功后的回调（用于热加载，无需重启）。"""
    app = Flask(__name__, template_folder=str(_TEMPLATE_DIR))
    _sk = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
    app.secret_key = _sk if (_sk and _sk.strip()) else secrets.token_hex(32)
    icon_ver = str(int(ICON_FILE.stat().st_mtime)) if ICON_FILE.exists() else ""
    # 页面均位于根路径下一级，统一用相对地址，兼容统一网关的 /app/<appname>/ 前缀
    icon_url = f"assets/icons/app-icon.png?v={icon_ver}" if icon_ver else ""
    favicon_url = f"favicon.ico?v={icon_ver}" if icon_ver else ""
    gh_ver = str(int(GITHUB_ICON_FILE.stat().st_mtime)) if GITHUB_ICON_FILE.exists() else ""
    github_icon_url = f"assets/icons/github.svg?v={gh_ver}" if gh_ver else "assets/icons/github.svg"
    _apply_initial_password_from_env()
    if not _has_password_set() and not _auth_disabled_by_env():
        _setup_codes.ensure()
    assets_dir = CODE_DIR / "assets"

    @app.get("/assets/<path:filename>")
    def serve_assets(filename: str):
        """提供项目 assets 目录下的静态文件。"""
        if not assets_dir.exists():
            abort(404)
        return send_from_directory(str(assets_dir), filename)

    @app.get("/favicon.ico")
    def favicon():
        """浏览器 favicon；复用 app-icon.png，避免 404。"""
        if ICON_FILE.exists():
            return send_from_directory(str(ICON_FILE.parent), ICON_FILE.name)
        abort(404)

    @app.get("/robots.txt")
    def robots_txt():
        return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/healthz")
    def healthz():
        """容器健康探针：仅确认 HTTP 服务可响应，不读取配置或 NAS 数据源。"""
        return jsonify({"ok": True})

    @app.after_request
    def _no_index(resp):
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
        # 含配置、webhook 打码值与推送记录的响应不得被浏览器/中间代理缓存
        path = request.path or ""
        if path.startswith("/api/") or path in {"/", "/history", "/faq"}:
            resp.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
            resp.headers.setdefault("Pragma", "no-cache")
        # nosniff：防止上传/回显内容被浏览器嗅探成可执行类型（不设 X-Frame-Options：
        # 配置页要嵌进飞牛桌面统一网关的 iframe，DENY 会直接弄坏应用窗口）
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # 会话在写请求中被续期后刷新 cookie 的 max_age，滚动过期
        if getattr(g, "refresh_auth_cookie", False) and _session_auth_active():
            resp.set_cookie(AUTH_COOKIE_NAME, _get_session_id_from_cookie(), **_auth_cookie_kwargs())
        return resp

    CHANNEL_OPTIONS = [
        {"id": "wechat", "name": "企业微信"},
        {"id": "dingtalk", "name": "钉钉"},
        {"id": "feishu", "name": "飞书"},
        {"id": "webhook", "name": "通用Webhook"},
    ]

    # 默认拒绝：只有以下路径无需登录；"/" 仅返回页面骨架，数据全部走需鉴权的接口
    PUBLIC_PATHS = {
        "/",
        "/favicon.ico",
        "/robots.txt",
        "/healthz",
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/set-password",
        "/api/auth/logout",
        "/session-guard.js",
    }
    PUBLIC_PREFIXES = ("/assets/",)

    def _home_url() -> str:
        return (request.script_root or "") + "/"

    @app.before_request
    def _require_auth():
        gw = _gateway_identity()
        if gw is not None and not gw["is_admin"]:
            msg = "仅 NAS 管理员可访问日志推送配置。"
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": msg}), 403
            return msg, 403, {"Content-Type": "text/plain; charset=utf-8"}
        # 写接口只接受 JSON，避免跨站表单以 text/plain 绕过预检
        if request.method == "POST" and not request.is_json:
            return jsonify({"ok": False, "message": "请求须为 application/json。"}), 415
        path = request.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return None
        config_error = _current_config_load_error()
        if config_error:
            return jsonify(_config_load_error_payload(config_error)), 500
        if _is_authenticated():
            # 写操作代表用户交互；统计、配置读取和身份轮询均不续期。
            # 仅密码会话需要续期——网关管理员/关闭鉴权没有会话可续。
            if request.method == "POST" and _session_auth_active():
                if not _touch_session(_get_session_id_from_cookie(), SESSION_IDLE_SECONDS, scope=_auth_cookie_path()):
                    return jsonify({"ok": False, "message": "会话已过期，请重新输入密码。"}), 401
                g.refresh_auth_cookie = True
            return None
        if path.startswith("/api/"):
            return jsonify({
                "ok": False,
                "need_setup": not _has_password_set(),
                "message": "未登录或会话已过期，请重新输入密码。",
            }), 401
        return redirect(_home_url())

    @app.get("/api/auth/status")
    def auth_status():
        """无需登录即可访问。返回是否需要设置密码、是否需要登录、是否已认证。"""
        config_error = _current_config_load_error()
        if config_error:
            return jsonify(_config_load_error_payload(config_error)), 500
        has_pw = _has_password_set()
        authenticated = _is_authenticated()
        need_setup = not has_pw and not authenticated
        setup_requires_code = need_setup and _setup_requires_code()
        if setup_requires_code:
            _setup_codes.ensure()
        return jsonify({
            "ok": True,
            "need_setup": need_setup,
            "setup_requires_code": setup_requires_code,
            "need_login": has_pw and not authenticated,
            "authenticated": authenticated,
            "via_gateway": _gateway_identity() is not None,
            "password_min_length": PASSWORD_MIN_LENGTH,
            "idle_seconds": SESSION_IDLE_SECONDS,
            # 密码会话返回真实剩余时间；网关管理员/关闭鉴权不随空闲过期
            "remaining_seconds": (
                SESSION_IDLE_SECONDS
                if not _session_auth_active()
                else _session_remaining_seconds(_get_session_id_from_cookie(), SESSION_IDLE_SECONDS, scope=_auth_cookie_path())
            ),
        })

    @app.post("/api/auth/activity")
    def auth_activity():
        """前端仅在真实用户交互后调用；鉴权钩子已刷新活跃时间。"""
        return jsonify({
            "ok": True,
            "authenticated": True,
            "idle_seconds": SESSION_IDLE_SECONDS,
            "remaining_seconds": _session_remaining_seconds(_get_session_id_from_cookie(), SESSION_IDLE_SECONDS, scope=_auth_cookie_path()),
        })

    @app.post("/api/auth/logout")
    def auth_logout():
        """删除服务端会话并清除 cookie（浏览器端由 session-guard.js 触发跳转）。"""
        _delete_session(_get_session_id_from_cookie(), scope=_auth_cookie_path())
        resp = jsonify({"ok": True})
        resp.delete_cookie(AUTH_COOKIE_NAME, path=_auth_cookie_path(), secure=_session_cookie_secure(), httponly=True, samesite="Lax")
        return resp

    @app.get("/session-guard.js")
    def session_guard_js():
        return send_from_directory(str(_TEMPLATE_DIR.parent / "static"), "session-guard.js")

    @app.post("/api/auth/set-password")
    @_config_transaction
    def auth_set_password():
        """首次设置密码：网关管理员/本机可直接设置，其余来源须提供初始化码。"""
        config_error = _current_config_load_error()
        if config_error:
            return jsonify(_config_load_error_payload(config_error)), 500
        if _has_password_set():
            return jsonify({"ok": False, "message": "已设置过密码，请使用登录。"}), 400
        key = _client_key()
        wait = _login_limiter.retry_after(key)
        if wait:
            return jsonify({"ok": False, "message": f"尝试次数过多，请 {wait} 秒后再试。"}), 429
        payload = request.get_json(silent=True) or {}
        if _setup_requires_code():
            if not _setup_codes.verify(str(payload.get("setup_code") or "")):
                _login_limiter.record_failure(key)
                return jsonify({
                    "ok": False,
                    "message": "初始化码错误。请在应用运行日志或配置目录的 .setup_code 文件中查看。",
                }), 403
        p1 = (payload.get("password") or "").strip()
        p2 = (payload.get("password_confirm") or "").strip()
        if not p1:
            return jsonify({"ok": False, "message": "请输入密码。"}), 400
        if len(p1) < PASSWORD_MIN_LENGTH:
            return jsonify({"ok": False, "message": f"密码长度至少 {PASSWORD_MIN_LENGTH} 位。"}), 400
        if p1 != p2:
            return jsonify({"ok": False, "message": "两次输入的密码不一致。"}), 400
        try:
            _save_raw_config(_apply_new_password(_load_raw_config(), p1))
        except Exception as e:
            return jsonify({"ok": False, "message": f"保存失败：{e}"}), 500
        _setup_codes.clear()
        _login_limiter.reset(key)
        # 换密码后作废旧会话再签发新的，旧 cookie 不能继续使用
        _delete_session(_get_session_id_from_cookie(), scope=_auth_cookie_path())
        session_id = _create_session(SESSION_IDLE_SECONDS, scope=_auth_cookie_path())
        resp = jsonify({"ok": True, "message": "密码设置成功。"})
        resp.set_cookie(AUTH_COOKIE_NAME, session_id, **_auth_cookie_kwargs())
        return resp

    @app.post("/api/auth/login")
    def auth_login():
        """使用密码登录（按来源 IP 限制连续失败次数）。"""
        config_error = _current_config_load_error()
        if config_error:
            return jsonify(_config_load_error_payload(config_error)), 500
        if not _has_password_set():
            return jsonify({"ok": False, "message": "尚未设置密码。"}), 400
        key = _client_key()
        wait = _login_limiter.retry_after(key)
        if wait:
            return jsonify({"ok": False, "message": f"密码错误次数过多，请 {wait} 秒后再试。"}), 429
        payload = request.get_json(silent=True) or {}
        password = (payload.get("password") or "").strip()
        if not password:
            return jsonify({"ok": False, "message": "请输入密码。"}), 400
        raw = _load_raw_config()
        salt, stored_hash = _get_password_config(raw)
        if not salt or not stored_hash:
            return jsonify({"ok": False, "message": "密码配置无效，请重新设置密码。"}), 400
        if not _verify_password(password, salt, stored_hash):
            _login_limiter.record_failure(key)
            return jsonify({"ok": False, "message": "密码错误。"}), 401
        _login_limiter.reset(key)
        _delete_session(_get_session_id_from_cookie(), scope=_auth_cookie_path())
        session_id = _create_session(SESSION_IDLE_SECONDS, scope=_auth_cookie_path())
        resp = jsonify({"ok": True, "message": "登录成功。"})
        resp.set_cookie(AUTH_COOKIE_NAME, session_id, **_auth_cookie_kwargs())
        return resp

    @app.get("/api/config")
    def get_config():
        raw = _load_raw_config()

        # 迁移：旧版默认多勾了「应用启动/自启动失败、UPS 开启/关闭」或「应用生命周期」时，改为新默认。
        # 注意：这是**读接口**，只在内存里返回迁移后的勾选状态，不写盘——
        # 原先在此回写 config.json 会让「打开配置页」产生磁盘副作用（含探测出来的库路径被固化）。
        # 用户在页面点保存时才会持久化。
        raw_events = raw.get("monitor_events")
        if isinstance(raw_events, list):
            raw_set = set(raw_events)
            new_default_set = set(DEFAULT_SELECTED_EVENTS)
            # 仅当配置恰好为「旧版默认（含启动失败/UPS 开关）」时迁移为新默认；不迁移「全选」（= 新默认+生命周期+额外），否则会覆盖用户的全选
            old_default_with_extra = new_default_set | OLD_DEFAULT_SELECTED_EVENTS_WITH_EXTRA
            old_full_default = new_default_set | APP_LIFECYCLE_EVENTS
            if raw_set == old_default_with_extra:
                monitor_events = DEFAULT_SELECTED_EVENTS
            elif raw_set == old_full_default:
                monitor_events = [e for e in raw_events if e not in APP_LIFECYCLE_EVENTS]
            else:
                monitor_events = raw_events
        else:
            monitor_events = DEFAULT_SELECTED_EVENTS

        # 与保存接口一致：剔除已下线的事件 ID（如历史配置中的 LISTEN_PORT_INBOUND），避免前端勾选状态与可选列表不一致
        events_by_category, third_party_events_by_category, valid_event_ids, _ = _events_catalog_bundle()
        if isinstance(monitor_events, list):
            monitor_events = [e for e in monitor_events if e in valid_event_ids]
        if not monitor_events:
            monitor_events = list(DEFAULT_SELECTED_EVENTS)

        channels = _mask_channels(_channels_from_raw(raw))

        try:
            from utils.cron_util import resolve_nas_patrol_cron

            _cron = resolve_nas_patrol_cron(
                raw.get("nas_patrol_cron", ""),
                raw.get("nas_patrol_interval_minutes", 720),
            )
        except Exception:
            _cron = "0 12 * * *"

        data = {
            "title": "FnMessageBot",
            "subtitle": "飞牛日志消息推送机器人",
            "version": APP_VERSION,
            "events_by_category": events_by_category,
            "third_party_events_by_category": third_party_events_by_category,
            "selected_events": monitor_events,
            "channels": channels,
            "title_prefix": _title_prefix_from_dict(raw),
            "log_retention_days": int(raw.get("log_retention_days", raw.get("max_log_age", 7))),
            "logger_poll_interval": int(raw.get("logger_poll_interval", 3)),
            "resume_from_cursor_on_start": as_bool(raw.get("resume_from_cursor_on_start", False), False),
            "dnd_enabled": as_bool(raw.get("dnd_enabled", False), False),
            "dnd_start_time": (raw.get("dnd_start_time") or "22:00").strip(),
            "dnd_end_time": (raw.get("dnd_end_time") or "07:00").strip(),
            "poll_batch_summary_enabled": as_bool(raw.get("poll_batch_summary_enabled", False), False),
            "media_batch_summary_enabled": as_bool(raw.get("media_batch_summary_enabled", True), True),
            "media_batch_window": _as_int(raw.get("media_batch_window", 90), 90),
            "minimal_push_enabled": as_bool(raw.get("minimal_push_enabled", False), False),
            "push_markdown_enabled": as_bool(raw.get("push_markdown_enabled", True), True),
            "ssh_ignore_loopback": as_bool(raw.get("ssh_ignore_loopback", False), False),
            "nas_patrol_enabled": as_bool(raw.get("nas_patrol_enabled", False), False),
            "nas_patrol_cron": _cron,
            "smart_auth": _smart_auth_state(),
            "channel_options": CHANNEL_OPTIONS,
        }
        db_access_warnings = _collect_external_db_access_warnings(raw, monitor_events)
        return jsonify({"ok": True, "data": data, "warnings": db_access_warnings})

    @app.post("/api/save-config")
    @_config_transaction
    def save_config():
        payload = request.get_json(silent=True) or {}

        _, _, valid_event_ids, _ = _events_catalog_bundle()
        events = payload.get("events") or []
        # 只保留后端认可的事件 ID，避免写入非法值导致热加载或重启异常
        events = [e for e in events if e in valid_event_ids]
        channels = payload.get("channels") or []
        channels = _normalize_push_channels(channels)
        channels, restore_err = _restore_masked_channels(channels, _channels_from_raw(_load_raw_config()))
        if restore_err:
            return jsonify({"ok": False, "message": restore_err}), 400
        log_retention_days = payload.get("log_retention_days", 7)
        logger_poll_interval = payload.get("logger_poll_interval", 3)
        resume_from_cursor_on_start = as_bool(payload.get("resume_from_cursor_on_start", False), False)
        dnd_enabled = as_bool(payload.get("dnd_enabled", False), False)
        dnd_start_time = (payload.get("dnd_start_time") or "22:00").strip()
        dnd_end_time = (payload.get("dnd_end_time") or "07:00").strip()
        poll_batch_summary_enabled = as_bool(payload.get("poll_batch_summary_enabled", False), False)
        media_batch_summary_enabled = as_bool(payload.get("media_batch_summary_enabled", True), True)
        media_batch_window = _as_int(payload.get("media_batch_window", 90), 90)
        minimal_push_enabled = as_bool(payload.get("minimal_push_enabled", False), False)
        push_markdown_enabled = as_bool(payload.get("push_markdown_enabled", True), True)
        ssh_ignore_loopback = as_bool(payload.get("ssh_ignore_loopback", False), False)
        nas_patrol_enabled = as_bool(payload.get("nas_patrol_enabled", False), False)
        nas_patrol_cron = (payload.get("nas_patrol_cron") or "").strip()
        title_prefix = _title_prefix_from_dict(payload)
        if title_prefix and len(title_prefix) > 20:
            return jsonify({"ok": False, "message": "标题前缀过长（最多 20 个字符）。"}), 400

        if dnd_enabled:
            if not dnd_start_time or not dnd_end_time:
                return jsonify({"ok": False, "message": "开启勿扰模式时请填写开始时间和结束时间。"}), 400
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", dnd_start_time):
                return jsonify({"ok": False, "message": "勿扰开始时间格式不正确，请使用 HH:MM（如 22:00）。"}), 400
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", dnd_end_time):
                return jsonify({"ok": False, "message": "勿扰结束时间格式不正确，请使用 HH:MM（如 07:00）。"}), 400

        if nas_patrol_enabled or nas_patrol_cron:
            try:
                from utils.cron_util import validate_cron_expr

                nas_patrol_cron = validate_cron_expr(nas_patrol_cron or "0 12 * * *")
            except ValueError as e:
                return jsonify({"ok": False, "message": str(e)}), 400
        else:
            nas_patrol_cron = (nas_patrol_cron or "0 12 * * *").strip() or "0 12 * * *"

        # 不允许选择内部保留事件
        if EVENT_IDS_HIDDEN_IN_UI & set(events):
            return jsonify({"ok": False, "message": "包含不可选的事件类型，请刷新页面重试。"}), 400

        # 基本校验
        if not events:
            return jsonify({"ok": False, "message": "请至少选择一个事件类型。"}), 400

        if not channels:
            return jsonify({"ok": False, "message": "请至少配置一个推送渠道。"}), 400
        if not any(bool(ch.get("enabled")) for ch in channels):
            return jsonify({"ok": False, "message": "请至少配置一个推送渠道。"}), 400

        known_types = {opt["id"] for opt in CHANNEL_OPTIONS}
        for ch in channels:
            ch_type = ch.get("type")
            url = (ch.get("url") or "").strip()
            if ch_type not in known_types:
                return jsonify({"ok": False, "message": "存在未知的推送渠道类型。"}), 400
            if not url:
                return jsonify({"ok": False, "message": "推送渠道地址不能为空。"}), 400
            if ch_type == "webhook":
                parts = []
                for part in (p.strip() for p in url.split("|") if p.strip()):
                    err = validate_channel_json(ch_type, part)
                    if err:
                        return jsonify({"ok": False, "message": err}), 400
                    # 多条配置以 | 拼接，JSON 字符串内的 | 转义保存
                    parts.append(json.dumps(json.loads(part), ensure_ascii=False).replace("|", "\\u007c"))
                ch["url"] = "|".join(parts)
            elif not url.startswith("http"):
                return (
                    jsonify({"ok": False, "message": f"推送地址格式不正确：{url}"}),
                    400,
                )

        if log_retention_days is None:
            log_retention_days = 7
        if logger_poll_interval is None:
            logger_poll_interval = 3
        try:
            log_retention_days = int(log_retention_days)
            logger_poll_interval = int(logger_poll_interval)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "日志缓存天数和轮询时间必须是整数。"}), 400

        if log_retention_days <= 0:
            return jsonify({"ok": False, "message": "日志缓存天数必须大于 0。"}), 400
        if logger_poll_interval <= 0:
            return jsonify({"ok": False, "message": "数据库轮询时间必须大于 0 秒。"}), 400

        # 完整列表写入 push_channels；扁平字段仅同步已开启渠道（供通知器推送）
        flat_keys = _sync_channel_flat_keys(channels)

        raw = _load_raw_config()
        # 已移除功能的残留键（含曾存过的 fnOS 账号密码）随保存一并清除
        for _removed_key in (
            "fnos_monitor_enabled",
            "fnos_monitor_url",
            "fnos_monitor_user",
            "fnos_monitor_password",
        ):
            raw.pop(_removed_key, None)
        raw.update(
            {
                **flat_keys,
                "push_channels": channels,
                "monitor_events": events,
                "log_retention_days": log_retention_days,
                "logger_poll_interval": logger_poll_interval,
                "resume_from_cursor_on_start": resume_from_cursor_on_start,
                "dnd_enabled": dnd_enabled,
                "dnd_start_time": dnd_start_time,
                "dnd_end_time": dnd_end_time,
                "poll_batch_summary_enabled": poll_batch_summary_enabled,
                "media_batch_summary_enabled": media_batch_summary_enabled,
                "media_batch_window": media_batch_window,
                "minimal_push_enabled": minimal_push_enabled,
                "push_markdown_enabled": push_markdown_enabled,
                "ssh_ignore_loopback": ssh_ignore_loopback,
                "nas_patrol_enabled": nas_patrol_enabled,
                "nas_patrol_cron": nas_patrol_cron,
                "title_prefix": title_prefix,
            }
        )

        try:
            _save_raw_config(raw)
        except Exception as e:
            return jsonify({"ok": False, "message": f"配置写入失败（{e}），请检查 config 目录是否可写。"}), 500

        db_access_warnings = _collect_external_db_access_warnings(raw, events)
        if nas_patrol_enabled or "NAS_PATROL_REPORT" in events:
            # 巡检主动诊断（限时预算）：区分权限缺失/未装工具/超时/无传感器
            try:
                from monitor.patrol.smart_auth import run_diagnostics, warning_messages

                db_access_warnings = list(db_access_warnings or []) + warning_messages(run_diagnostics())
            except Exception:
                pass
        if nas_patrol_cron and not nas_patrol_enabled:
            db_access_warnings = list(db_access_warnings or [])
            db_access_warnings.append(
                "已填写巡检 Cron，但未勾选「巡检任务」：定时不会自动执行（「立即巡检一次」仍可用）。"
            )

        if callable(on_config_saved):
            try:
                on_config_saved()
            except Exception as e:
                return jsonify({
                    "ok": True,
                    "message": f"配置已保存，但热加载失败（{e}），请重启容器后生效。",
                    "warnings": db_access_warnings,
                }), 200

        return jsonify({
            "ok": True,
            "message": "配置已保存，监控已热加载生效，无需重启容器。",
            "warnings": db_access_warnings,
        })

    @app.post("/api/test")
    def test_push():
        try:
            payload = request.get_json(silent=True) or {}
            # 内置测试消息：页面不再要求用户输入；保留 content 参数仅为兼容旧调用方
            content = resolve_test_push_content(payload)

            raw = _load_raw_config()
            notifier = _build_notifier_from_raw(raw)

            out = notifier.send_system_notification(
                "TEST_PUSH",
                content,
                {
                    "hostname": socket.gethostname(),
                    "version": APP_VERSION,
                },
            )
            ok = out.get("success", False) if isinstance(out, dict) else bool(out)
            channel_results = out.get("channel_results") if isinstance(out, dict) else None
            if ok:
                return jsonify({
                    "ok": True,
                    "message": "测试消息已发送，请检查各渠道是否收到。",
                    "channel_results": channel_results or [],
                })
            # 拼出各渠道失败原因，便于排查自建服务（如通用 Webhook 目标）
            fail_bits = []
            if isinstance(channel_results, list):
                for cr in channel_results:
                    if not isinstance(cr, dict):
                        continue
                    name = str(cr.get("channel") or "渠道")
                    err = cr.get("error")
                    if err:
                        fail_bits.append(f"{name}: {err}")
                    elif not cr.get("success"):
                        fail_bits.append(f"{name}: 发送失败")
            detail = "；".join(fail_bits) if fail_bits else "请检查配置"
            return jsonify({
                "ok": False,
                "message": f"所有渠道发送失败。{detail}",
                "channel_results": channel_results or [],
            }), 500
        except Exception as e:
            return jsonify({"ok": False, "message": f"测试发送异常：{e}"}), 500

    @app.post("/api/nas-patrol/run")
    def run_nas_patrol():
        """立即执行一次 NAS 巡检推送（不等待 Cron）。"""
        try:
            runtime = get_runtime_monitor_app()
            if runtime is None:
                return jsonify({
                    "ok": False,
                    "message": "监控进程未就绪（请确认以完整服务启动，而非仅 UI）。",
                }), 503
            from monitor.nas_patrol import run_nas_patrol_now

            ok, message = run_nas_patrol_now(runtime)
            if ok:
                return jsonify({"ok": True, "message": message})
            return jsonify({"ok": False, "message": message}), 500
        except Exception as e:
            return jsonify({"ok": False, "message": f"巡检执行异常：{e}"}), 500

    @app.get("/api/push-stats")
    def get_push_stats():
        """推送数据汇总：总条数/成功/失败，当日条数/成功/失败（基于 SQLite push_history）。"""
        try:
            stats = get_push_history_stats(_load_raw_config)
            return jsonify({
                "ok": True,
                "data": stats,
            })
        except Exception:
            return jsonify({
                "ok": True,
                "data": {
                    "total": {"total": 0, "success": 0, "fail": 0},
                    "today": {"total": 0, "success": 0, "fail": 0},
                },
            })

    @app.get("/api/push-history")
    def get_push_history():
        """推送记录列表：分页，可选按成功/失败筛选。"""
        try:
            limit = min(100, max(1, request.args.get("limit", 50, type=int)))
            offset = max(0, request.args.get("offset", 0, type=int))
            success_filter = _parse_success_filter(request.args.get("success"))
            rows = list_push_history_records(
                _load_raw_config,
                limit=limit,
                offset=offset,
                success_filter=success_filter,
            )
            return jsonify({"ok": True, "data": rows})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.get("/api/push-history/<int:record_id>")
    def get_push_history_detail(record_id):
        """单条推送记录详情。"""
        try:
            row = get_push_history_record(_load_raw_config, record_id)
            if row is None:
                return jsonify({"ok": False, "message": "记录不存在"}), 404
            return jsonify({"ok": True, "data": row})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.get("/history")
    def history_page():
        """推送记录已并入首页「概览」的卡片列表，不再单独成页。

        保留此路由并 302 到概览，而不是直接删除：旧书签、外部链接与
        浏览器历史里的 /history 仍能落到正确位置，不会变成 404。
        跳转目标用 _home_url() 拼，直接写 "/" 会在网关前缀部署下跳错。
        原有查询参数原样保留——丢掉的话 ?theme=dark 之类的状态就没了。
        """
        qs = request.query_string.decode("utf-8", "replace")
        if "tab=" not in qs:
            qs = (qs + "&" if qs else "") + "tab=overview"
        return redirect(_home_url() + "?" + qs)

    @app.get("/faq")
    def faq_page():
        """常见问题页。SMART 授权数据按当前系统实际设备生成，命令文本由模板拼装。"""
        return render_template(
            "faq.html",
            favicon_url=favicon_url,
            repo_url=PROJECT_REPO_URL,
            docs_url=PROJECT_DOCS_URL,
            footer_text=FOOTER_TEXT,
            smart_auth=_smart_auth_state(),
        )

    @app.get("/")
    def index():
        """单页应用（模板见 templates/index.html）。"""
        return render_template(
            "index.html",
            icon_url=icon_url,
            favicon_url=favicon_url,
            github_icon_url=github_icon_url,
            repo_url=PROJECT_REPO_URL,
            docs_url=PROJECT_DOCS_URL,
            footer_text=FOOTER_TEXT,
            version=APP_VERSION,
        )


    return app


def _serve_forever(server, name: str) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return thread


def start_ui_supervisor(on_config_saved=None) -> "UIServerSupervisor":
    """以监督方式启动配置 UI：TCP 与统一网关 Socket 两个入口分别监听、
    各自失败后自动重试（5 秒），某个入口的临时故障不会让后台永久不可用。
    返回的句柄带 stop()，供应用关停时先停 Web 再落盘。"""
    host = (os.getenv("UI_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    port = (os.getenv("UI_PORT") or "18080").strip() or "18080"
    sock_path = (os.getenv("FNMB_GATEWAY_SOCKET") or "").strip()
    prefix = (os.getenv("FNMB_GATEWAY_PREFIX") or "").strip()
    from web.server_lifecycle import UIServerSupervisor

    return UIServerSupervisor(
        app_factory=lambda: create_app(on_config_saved=on_config_saved),
        host=host,
        port=port,
        socket_path=sock_path,
        gateway_prefix=prefix,
    ).start()


if __name__ == "__main__":
    # 本地调试：只启动 UI，不启动监控（无需配置 Webhook 即可打开页面）
    repo_root = Path(__file__).resolve().parent.parent.parent
    os.chdir(repo_root)
    app = create_app()
    app.wsgi_app = StripGatewayHeadersMiddleware(app.wsgi_app)
    port = int(os.getenv("UI_PORT", "18080"))
    print(f"配置 UI: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=True)
