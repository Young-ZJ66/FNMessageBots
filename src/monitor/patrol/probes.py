"""系统探针：CPU/内存/磁盘占用、温度、主机名、内外网 IP、系统版本等基础信息采集。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import ipaddress

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

from utils.logtime_display import format_ts_for_display
from ..sqlite_uri import connect_readonly_with_fallback



def _cpu_mem_disk() -> Tuple[str, str, str]:
    """返回 (cpu%, mem%, 根分区剩余 GB) 的字符串，不可用时为 —"""
    if not psutil:
        return "—", "—", "—"
    try:
        cpu = f"{psutil.cpu_percent(interval=1.0):.1f}"
    except Exception:
        cpu = "—"
    try:
        mem = f"{psutil.virtual_memory().percent:.1f}"
    except Exception:
        mem = "—"
    try:
        free_gb = psutil.disk_usage("/").free / (1024**3)
        disk = f"{free_gb:.1f}"
    except Exception:
        disk = "—"
    return cpu, mem, disk


def _cpu_disk_temp_c() -> Tuple[str, str]:
    """返回 CPU 温度、磁盘/存储相关温度（摄氏度数值字符串，无时为 —）。"""
    cpu_t, disk_t = "—", "—"
    if not psutil:
        return cpu_t, disk_t
    try:
        t = psutil.sensors_temperatures()
        if isinstance(t, dict):
            if "coretemp" in t and t["coretemp"]:
                cpu_t = f"{t['coretemp'][0].current:.1f}"
            elif "cpu_thermal" in t and t["cpu_thermal"]:
                cpu_t = f"{t['cpu_thermal'][0].current:.1f}"
            elif "cpu-thermal" in t and t["cpu-thermal"]:
                cpu_t = f"{t['cpu-thermal'][0].current:.1f}"
            if "nvme" in t and t["nvme"]:
                disk_t = f"{t['nvme'][0].current:.1f}"
            elif "sata" in t and t["sata"]:
                disk_t = f"{t['sata'][0].current:.1f}"
    except Exception:
        pass
    if disk_t == "—":
        for hw in ("/sys/class/hwmon/hwmon1/temp1_input", "/sys/class/hwmon/hwmon0/temp1_input"):
            try:
                v = int(Path(hw).read_text(encoding="utf-8").strip())
                disk_t = f"{v / 1000.0:.1f}"
                break
            except Exception:
                continue
    return cpu_t, disk_t


def _resolve_cmd(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    for prefix in ("/usr/sbin", "/usr/bin", "/sbin", "/bin", "/usr/local/bin", "/usr/local/sbin"):
        c = str(Path(prefix) / name)
        if Path(c).exists():
            return c
    return name


def _run_cmd(args: List[str], timeout: float = 2.0) -> str:
    try:
        cmd = list(args)
        if cmd:
            cmd[0] = _resolve_cmd(cmd[0])
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    out = (p.stdout or "").strip()
    if out:
        return out
    return (p.stderr or "").strip()


def _run_smart_probe(args: List[str], timeout: float) -> str:
    """SMART 只读探测：argv 参数列表、shell=False；异常（找不到命令/超时）
    向上抛出由诊断层分类，返回合并的 stdout+stderr。"""
    p = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )
    return (p.stdout or "") + (p.stderr or "")


def probe_journal_readability() -> str:
    """journal 读取链路探测（配置保存时调用，有界：只读 1 条，不整卷 dump）。

    返回给用户的告警文本；链路正常或环境无 journalctl 时返回空串。
    判据与 SSH journal 轮询器一致：必须真的读出带游标的条目才算可用。
    注意：本探测不带 --directory 定制（轮询器支持 SSH_JOURNAL_DIRECTORY 环境变量），
    定制了目录且默认路径读不到的场景会误报——以「立即巡检」结果为准。
    """
    if not shutil.which("journalctl"):
        return ""
    try:
        out = _run_smart_probe(["journalctl", "--system", "--no-pager", "-n", "1", "--show-cursor"], timeout=5.0)
    except Exception:
        return ""
    if out and "-- cursor:" in out:
        return ""
    reason = (out or "").strip()[:200] or "读不到带游标的条目"
    if Path("/.dockerenv").exists():
        hint = (
            "Docker 部署：在 compose 挂载宿主 journal 目录与 /etc/machine-id；"
            "非 root 容器还需把宿主 systemd-journal 组加入 group_add 后重建容器。"
        )
    else:
        hint = "native 部署：把应用账号加入 systemd-journal 组，然后在应用中心停止并重新启动应用。"
    return (
        "SSH journal: 当前账号读不到系统 journal（" + reason + "）。" + hint + " "
        "具体命令见配置页常见问题第 10 条。"
    )


def _run_cmd_any(candidates: List[List[str]], timeout: float = 2.0) -> str:
    for c in candidates:
        out = _run_cmd(c, timeout=timeout)
        if out:
            return out
    return ""


def _run_upsc_cmd(args: List[str], timeout: float = 2.0) -> str:
    """仅采纳 upsc 成功时的 stdout，避免把帮助/版本 stderr 当成 UPS 数据。"""
    try:
        cmd = list(args)
        if cmd:
            cmd[0] = _resolve_cmd(cmd[0])
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    if p.returncode != 0:
        return ""
    return (p.stdout or "").strip()


_UPSC_KNOWN_VAR_PREFIXES: Tuple[str, ...] = (
    "ups.",
    "battery.",
    "input.",
    "output.",
    "device.",
    "driver.",
    "upsmon.",
)


def _upsc_text_is_error_or_help(text: str) -> bool:
    if not (text or "").strip():
        return True
    low = text.lower()
    if any(
        m in low
        for m in (
            "network ups tools",
            "display this help",
            "usage:",
            "error:",
            "connection refused",
            "driver not connected",
            "unknown argument",
            "can't connect",
            "cannot connect",
            "no such host",
            "timed out",
        )
    ):
        return True
    first = text.splitlines()[0].strip()
    return bool(first.startswith("-") and " " in first)


def _upsc_is_plausible_device_name(name: str) -> bool:
    s = (name or "").strip()
    if not s or len(s) > 128 or _upsc_text_is_error_or_help(s):
        return False
    if " " in s or s.startswith("-"):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", s))


def _upsc_kv_has_ups_vars(kv: Dict[str, str]) -> bool:
    for k in kv:
        kl = k.lower()
        if any(kl.startswith(p) for p in _UPSC_KNOWN_VAR_PREFIXES):
            return True
    return False


def _safe_path_exists(p: Path) -> bool:
    try:
        return p.exists()
    except Exception:
        return False


def _safe_glob(base: Path, pattern: str) -> List[Path]:
    try:
        return list(base.glob(pattern))
    except Exception:
        return []


def _valid_sysfs_block_token(s: str) -> bool:
    """内核块设备名（PKNAME/slave 名等），排除 lsblk 报错行被误解析。"""
    t = (s or "").strip()
    if not t or len(t) > 80:
        return False
    low = t.lower()
    if low.startswith("lsblk"):
        return False
    if any(ch in t for ch in " \t:/\\"):
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_+-]+", t))


def _fmt_uptime(sec: float) -> str:
    total = max(0, int(sec))
    days = total // 86400
    remain = total % 86400
    hours = remain // 3600
    remain %= 3600
    mins = remain // 60
    secs = remain % 60
    return f"{days}天{hours}时{mins}分{secs}秒"


def _fmt_boot_time(ts: float) -> str:
    try:
        return format_ts_for_display(ts)
    except Exception:
        return "--"


def _pick_lan_ip() -> str:
    """优先使用默认路由上的活动网卡，桥接容器可显式指定 NAS_LAN_IP。"""
    override = (os.getenv("NAS_LAN_IP") or "").strip()
    if override:
        try:
            addr = ipaddress.IPv4Address(override)
            # 内网语义从严：拒绝 loopback/link-local/未指定/组播与公网地址
            if (
                not (addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast)
                and addr.is_private
            ):
                return str(addr)
        except ValueError:
            pass
        logging.getLogger(__name__).warning("NAS_LAN_IP 不是有效的内网 IPv4，已忽略")
    if not psutil:
        return "--"
    try:
        interfaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        default_interface = _default_ipv4_interface()
        candidates: List[Tuple[int, int, str, str]] = []
        for name, addrs in interfaces.items():
            if re.match(r"^(lo$|docker|br-|virbr|veth|cni|flannel|tun|tap|wg|tailscale|zt)", name):
                continue
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                ip = str(getattr(a, "address", "") or "").strip()
                try:
                    obj = ipaddress.IPv4Address(ip)
                except ValueError:
                    continue
                if obj.is_loopback or obj.is_link_local or obj.is_unspecified or obj.is_multicast:
                    continue
                # 默认路由上的网卡优先，其次私网地址（避免选中 docker0 的 172.x）
                candidates.append((0 if name == default_interface else 1, 0 if obj.is_private else 1, name, ip))
        if candidates:
            return min(candidates)[3]
    except Exception as e:
        logging.getLogger(__name__).warning("内网 IP 检测失败: %s", e)
        return "--"
    return "--"


def _default_ipv4_interface() -> str:
    """读取 Linux 默认路由，多个出口时选 UP 且 metric 最低的一项。"""
    routes: List[Tuple[int, str]] = []
    try:
        for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 8 and parts[1] == "00000000" and int(parts[3], 16) & 1:
                routes.append((int(parts[6]), parts[0]))
    except (OSError, ValueError):
        pass
    return min(routes)[1] if routes else ""


def _http_get_ip(url: str, *, family: int, timeout: float = 3.0) -> str:
    """GET 公网 IP 接口，强制使用指定地址族（避免双栈误走另一族）。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    last_err: Optional[BaseException] = None
    for res in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        af, socktype, proto, _canon, sa = res
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(sa)
            if parsed.scheme == "https":
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            req = (
                f"GET {path} HTTP/1.0\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: FNMessageBots\r\n"
                f"Accept: text/plain\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > 65536:
                    break
            text = raw.decode("utf-8", errors="ignore")
            if "\r\n\r\n" in text:
                body = text.split("\r\n\r\n", 1)[1]
            elif "\n\n" in text:
                body = text.split("\n\n", 1)[1]
            else:
                body = text
            line = (body or "").strip().splitlines()
            return line[0].strip() if line else ""
        except Exception as e:
            last_err = e
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    if last_err:
        raise last_err
    return ""


def _is_global_ip(text: str, version: int) -> bool:
    """外网 IP 有效性：版本匹配且是全局地址（拒绝私网/环回/链路本地/组播）。"""
    try:
        obj = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return False
    if obj.version != version:
        return False
    if obj.is_multicast or obj.is_loopback or obj.is_link_local or obj.is_unspecified:
        return False
    return obj.is_global


def _pick_wan_ip() -> str:
    """优先拿到 IPv4；仅当 IPv4 全部失败时才用 IPv6。

    避免偶发 IPv4 超时回退到 IPv6、下次又拿到 IPv4，导致通知在 v4/v6 间来回刷。
    结果必须是全局地址：错误页/门户页里出现的私网地址不能进基线。
    """
    v4_urls = (
        "https://api4.ipify.org",
        "https://ipv4.icanhazip.com",
        "https://v4.ident.me",
        "https://ifconfig.me/ip",
        "https://ipinfo.io/ip",
    )
    # 两轮穷尽 IPv4，降低偶发超时误落到 IPv6 的概率
    for round_i in range(2):
        for u in v4_urls:
            try:
                text = _http_get_ip(u, family=socket.AF_INET, timeout=3.0)
                if _is_global_ip(text, 4):
                    return text.strip()
            except Exception:
                continue
        if round_i == 0:
            time.sleep(0.5)

    v6_urls = (
        "https://api6.ipify.org",
        "https://ipv6.icanhazip.com",
        "https://v6.ident.me",
        "https://api64.ipify.org",
    )
    for u in v6_urls:
        try:
            text = _http_get_ip(u, family=socket.AF_INET6, timeout=3.0)
            if _is_global_ip(text, 6):
                return text.strip()
        except Exception:
            continue
    return "--"


def _read_system_version() -> str:
    """读取宿主机 OS；容器中缺少宿主挂载时不使用镜像自身的 Debian 版本。"""
    paths = ["/host/etc/os-release", "/rootfs/etc/os-release", "/mnt/host/etc/os-release"]
    if not Path("/.dockerenv").exists() and not Path("/run/.containerenv").exists():
        paths.extend(["/etc/os-release", "/usr/lib/os-release"])
    for filename in paths:
        try:
            values = {}
            for line in Path(filename).read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip().strip("\"'")
            version = values.get("PRETTY_NAME") or " ".join(
                v for v in (values.get("NAME"), values.get("VERSION_ID")) if v
            )
            if version:
                return version[:160]
        except (OSError, UnicodeError):
            continue
    logging.getLogger(__name__).warning(
        "未读到宿主系统版本：Docker 请只读挂载 /etc/os-release 到 /host/etc/os-release"
    )
    return "--"


def _looks_like_container_hostname(s: str) -> bool:
    """Docker 短 ID 等：12/64 位十六进制，容器内常被当作 hostname。"""
    t = (s or "").strip().lower()
    if not t:
        return True
    if re.fullmatch(r"[0-9a-f]{12}", t):
        return True
    if re.fullmatch(r"[0-9a-f]{64}", t):
        return True
    return False


def _default_ipv4_gateway() -> str:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dest, gw_hex = parts[1], parts[2]
                if dest != "00000000" or gw_hex == "00000000":
                    continue
                gw_bytes = bytes.fromhex(gw_hex)
                if len(gw_bytes) != 4:
                    continue
                return socket.inet_ntoa(gw_bytes[::-1])
    except Exception:
        return ""
    return ""


_JSON_HOST_KEYS = (
    "nasName",
    "hostname",
    "hostName",
    "machineName",
    "deviceName",
    "stationName",
    "devName",
    "serverName",
    "nas_hostname",
)


def _deep_find_strings_by_keys(obj: Any, keys: Tuple[str, ...]) -> str:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                s = v.strip()
                if s and not _looks_like_container_hostname(s):
                    return s[:128]
        for v in obj.values():
            s = _deep_find_strings_by_keys(v, keys)
            if s:
                return s
    elif isinstance(obj, list):
        for it in obj:
            s = _deep_find_strings_by_keys(it, keys)
            if s:
                return s
    return ""


_FNOS_VER_KEYS = (
    "fnosVersion",
    "fnos_version",
    "fnOsVersion",
    "sysVersion",
    "systemVersion",
    "trimVersion",
    "miniOsVersion",
)


def _sanitize_fnos_version_candidate(raw: str) -> str:
    s = (raw or "").strip()
    if not s or len(s) > 160:
        return ""
    low = s.lower()
    if "error" in low or "unknown" in low:
        return ""
    if "fnos" in low:
        return s[:160]
    if re.search(r"\d+\.\d+\.\d+", s) or re.match(r"^v?\d+\.\d+(\.\d+)?$", s.strip(), re.I):
        ver = re.sub(r"^v", "", s, flags=re.I).strip()
        return f"FnOS {ver}"[:160] if not ver.lower().startswith("fnos") else s[:160]
    return ""


def _deep_find_fnos_version_json(obj: Any) -> str:
    if isinstance(obj, dict):
        for k in _FNOS_VER_KEYS:
            v = obj.get(k)
            if v is None:
                continue
            s = _sanitize_fnos_version_candidate(str(v))
            if s:
                return s
        for v in obj.values():
            s = _deep_find_fnos_version_json(v)
            if s:
                return s
    elif isinstance(obj, list):
        for it in obj:
            s = _deep_find_fnos_version_json(it)
            if s:
                return s
    return ""


def _patrol_read_hostname_fnos_from_logger_db(cfg: Any) -> Tuple[str, str]:
    """从已挂载的 eventlogger SQLite 的 parameter JSON 推断 NAS 展示名与系统版本（Docker 内常用）。"""
    path = (os.getenv("LOGGER_DB_PATH") or str(getattr(cfg, "logger_db_path", "") or "")).strip()
    if not path or not os.path.isfile(path):
        return "", ""
    conn: Optional[sqlite3.Connection] = None
    rows: List[Any] = []
    try:
        conn = connect_readonly_with_fallback(path, timeout=3.0)
        rows = conn.execute("SELECT parameter FROM log ORDER BY rowid DESC LIMIT 500").fetchall()
    except Exception:
        return "", ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    best_h, best_v = "", ""
    for row in rows:
        param = row[0] if row else None
        if not param or not str(param).strip():
            continue
        try:
            obj = json.loads(param)
        except Exception:
            continue
        if not best_h:
            cand = _deep_find_strings_by_keys(obj, _JSON_HOST_KEYS)
            if cand:
                best_h = cand
        if not best_v:
            candv = _deep_find_fnos_version_json(obj)
            if candv:
                best_v = candv
        if best_h and best_v:
            break
    return best_h, best_v


def _hostname_from_etc_hosts() -> str:
    try:
        text = Path("/etc/hosts").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] != "127.0.1.1":
            continue
        for name in parts[1:]:
            if name.lower() in ("localhost",):
                continue
            if _looks_like_container_hostname(name):
                continue
            return name.strip()[:128]
    return ""


def _hostname_from_mounted_host_paths() -> str:
    for fp in ("/host/etc/hostname", "/rootfs/etc/hostname", "/mnt/host/etc/hostname"):
        try:
            p = Path(fp)
            if not p.is_file():
                continue
            lines = p.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if lines and lines[0].strip():
                cand = lines[0].strip()
                if not _looks_like_container_hostname(cand):
                    return cand[:128]
        except Exception:
            continue
    return ""


def _read_hostname(logger_db_hostname: str = "") -> str:
    hint = (logger_db_hostname or "").strip()
    if hint and not _looks_like_container_hostname(hint):
        return hint[:128]
    h2 = _hostname_from_etc_hosts()
    if h2:
        return h2
    h3 = _hostname_from_mounted_host_paths()
    if h3:
        return h3
    sock = (socket.gethostname() or "").strip()
    if sock and not _looks_like_container_hostname(sock):
        return sock[:128]
    return sock or "--"


def _read_fnos_version(logger_db_fnos: str = "") -> str:
    """飞牛版本：优先开放 API / TRIM_SYS_VERSION，再回退本地探测（手装 Docker 等环境）。

    当前宿主的版本文件/包信息优先于历史日志——旧事件里的版本号可能早已过期；
    只有日志来源且无法净化确认时才打 warning 并弃用。
    """
    try:
        from utils.fnos_platform import resolve_fnos_version_via_api

        via_api = resolve_fnos_version_via_api()
        if via_api:
            return via_api[:160]
    except Exception:
        pass

    for rel in (
        "/etc/fnos-release",
        "/etc/fnos_version",
        "/usr/trim/etc/version",
        "/usr/trim/etc/fnos_version",
        "/usr/trim/VERSION",
        "/run/fnos/version",
    ):
        try:
            p = Path(rel)
            if not p.is_file():
                continue
            lines = p.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if lines:
                version = _sanitize_fnos_version_candidate(lines[0])
                if version:
                    return version
        except Exception:
            continue

    # 容器内通常没有飞牛的 trim 包；若把「宿主的 dpkg status」只读挂进容器，可解析出与宿主机 dpkg 一致的版本
    ver_host = _fnos_trim_version_from_host_dpkg_status()
    if ver_host:
        return _format_fnos_version_display(ver_host)

    # 容器自身 dpkg（仅当镜像/环境内确实安装了 trim 时有效）
    for sh_line in (
        "dpkg -s trim 2>/dev/null | grep Version | awk '{print $2}' || true",
        "dpkg-query -W -f='${Version}\\n' trim 2>/dev/null || true",
    ):
        out = _run_cmd(["sh", "-lc", sh_line], timeout=3.0)
        raw_lines = (out or "").strip().splitlines()
        if not raw_lines or not raw_lines[0].strip():
            continue
        raw = raw_lines[0].strip()
        low = raw.lower()
        if "dpkg-query" in low or "not installed" in low or "no information" in low or "no packages" in low:
            continue
        if low.startswith("e:"):
            continue
        version = _sanitize_fnos_version_candidate(raw)
        if version:
            return _format_fnos_version_display(version)

    version = _sanitize_fnos_version_candidate(logger_db_fnos)
    if version:
        logging.getLogger(__name__).warning(
            "忽略历史日志中的飞牛版本，无法确认是否为当前版本: %s", version
        )
    logging.getLogger(__name__).warning(
        "未读到飞牛版本：请检查开放 API、TRIM_SYS_VERSION 或宿主 dpkg status 挂载"
    )
    return "--"


def _format_fnos_version_display(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "--"
    if raw.lower().startswith("fnos"):
        return raw[:160]
    return f"FnOS {raw}"[:160]


def _fnos_trim_version_from_host_dpkg_status() -> str:
    """从 Debian dpkg status 流式解析 Package: trim 的 Version（避免整文件读入内存）。"""
    candidates = (
        "/host/dpkg/status",
        "/host/var/lib/dpkg/status",
        "/rootfs/var/lib/dpkg/status",
        "/mnt/host/var/lib/dpkg/status",
    )
    for rel in candidates:
        p = Path(rel)
        if not p.is_file():
            continue
        v = _fnos_trim_version_stream_dpkg_status(p)
        if v:
            return v
    return ""


def _fnos_trim_version_stream_dpkg_status(path: Path, max_read_bytes: int = 64 * 1024 * 1024) -> str:
    in_trim = False
    nread = 0
    try:
        with open(path, "rb") as fb:
            for raw in fb:
                nread += len(raw)
                if nread > max_read_bytes:
                    break
                line = raw.decode("utf-8", errors="ignore")
                if line.startswith("Package:"):
                    in_trim = line.split(":", 1)[1].strip() == "trim"
                elif in_trim and line.startswith("Version:"):
                    ver = line.split(":", 1)[1].strip()
                    if ver:
                        return ver
    except Exception:
        return ""
    return ""


def _read_update_status() -> str:
    """按用户要求关闭更新检查。"""
    return "不检查"


