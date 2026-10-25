"""
从宿主机 systemd journal 轮询 SSH 登录相关日志。

飞牛新版本 eventlogger 往往不再写入 Sshd*，且 /var/log/auth.log 已取消；
改用 ``journalctl -u ssh -u sshd`` 增量采集，映射到现有 SSH_* 事件。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .models import JournalEntry
from utils.logtime_display import format_ts_for_display, now_for_display

SSH_LOGIN_SUCCESS = "SSH_LOGIN_SUCCESS"
SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
SSH_INVALID_USER = "SSH_INVALID_USER"
SSH_DISCONNECTED = "SSH_DISCONNECTED"

SSH_JOURNAL_EVENTS: Set[str] = {
    SSH_LOGIN_SUCCESS,
    SSH_AUTH_FAILED,
    SSH_INVALID_USER,
    SSH_DISCONNECTED,
}

_STATE_FILENAME = "ssh_journal_poller_state.json"

# 「读不到宿主机 journal」的排查提示。飞牛的 journal 常见是 volatile（/run/log/journal，tmpfs），
# 也有机器启用了持久化（/var/log/journal）；因此要按**宿主机实际存在**的那个目录来挂，
# 挂一个不存在的路径只会创建一个空目录，反而更迷惑。
JOURNAL_MOUNT_HINT = (
    "常见原因：容器未挂载宿主机的 journal 目录与 /etc/machine-id。"
    "先在宿主机执行 `ls -ld /var/log/journal /run/log/journal` 确认哪个存在，"
    "然后按实际存在的挂载（飞牛多为 /run/log/journal），并必须挂载宿主机的 "
    "/etc/machine-id —— journal 按 machine-id 分目录存放，容器用它自己的 id 会找不到；"
    "或 SSH 的 unit 名不是 ssh/sshd。"
)

# 开启「忽略本机回环」时只静默这两类；失败 / 无效用户仍推送，避免内网穿透场景下漏掉暴力破解
SSH_LOOPBACK_QUIET_EVENTS = frozenset({SSH_LOGIN_SUCCESS, SSH_DISCONNECTED})


def is_loopback_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(addr.is_loopback or (mapped is not None and mapped.is_loopback))


def should_ignore_loopback_ssh(event_type: str, event_data: Dict[str, Any]) -> bool:
    if event_type not in SSH_LOOPBACK_QUIET_EVENTS:
        return False
    return is_loopback_ip(str(event_data.get("IP") or event_data.get("ip") or ""))

# Accepted publickey/password/keyboard-interactive for user from ip port N ssh2
_RE_ACCEPTED = re.compile(
    r"Accepted\s+\S+\s+for\s+(\S+)\s+from\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)
_RE_FAILED = re.compile(
    r"Failed\s+password\s+for\s+(?:invalid\s+user\s+)?(\S+)\s+from\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)
_RE_INVALID = re.compile(
    r"Invalid\s+user\s+(\S+)\s+from\s+(\S+)(?:\s+port\s+\d+)?",
    re.IGNORECASE,
)
_RE_DISCONNECT = re.compile(
    r"Disconnected\s+from(?:\s+user)?\s+(\S+)\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)
# Disconnected from authenticating user X IP port N
_RE_DISCONNECT_AUTH = re.compile(
    r"Disconnected\s+from\s+authenticating\s+user\s+(\S+)\s+(\S+)\s+port\s+\d+",
    re.IGNORECASE,
)


def journalctl_available() -> bool:
    return bool(shutil.which("journalctl"))


def parse_ssh_journal_message(message: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    解析单行 sshd MESSAGE，返回 (event_type, event_data) 或 None（忽略噪声/无关行）。
    """
    msg = (message or "").strip()
    if not msg:
        return None
    # 扫端口 / 非 SSH 协议探测，不推送
    low = msg.lower()
    if "kex_exchange_identification" in low or "invalid protocol identifier" in low:
        return None
    if "banner exchange" in low and "invalid format" in low:
        return None
    # pam 细节交给 Failed password / Accepted 行，避免双推
    if "pam_unix(sshd:auth)" in low or "pam_winbind(sshd:auth)" in low:
        return None
    if "pam_unix(sshd:session): session opened" in low:
        return None

    m = _RE_ACCEPTED.search(msg)
    if m:
        return SSH_LOGIN_SUCCESS, {"user": m.group(1), "IP": m.group(2)}

    m = _RE_INVALID.search(msg)
    if m:
        return SSH_INVALID_USER, {"user": m.group(1), "IP": m.group(2)}

    m = _RE_FAILED.search(msg)
    if m:
        user = m.group(1)
        ip = m.group(2)
        # "Failed password for invalid user xxx" 已由 Invalid user 覆盖；此处 invalid 前缀再判一次
        if re.search(r"Failed\s+password\s+for\s+invalid\s+user\s+", msg, re.I):
            return SSH_INVALID_USER, {"user": user, "IP": ip}
        return SSH_AUTH_FAILED, {"user": user, "IP": ip}

    m = _RE_DISCONNECT_AUTH.search(msg) or _RE_DISCONNECT.search(msg)
    if m:
        return SSH_DISCONNECTED, {"user": m.group(1), "IP": m.group(2)}

    # pam「session closed」行不带 IP，同一次断开已由上方 Disconnected 行推送，不再单独解析
    return None


class SshJournalPoller:
    """轮询 journalctl 增量，推送 SSH_* 事件。"""

    def __init__(
        self,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
        journal_units: Optional[List[str]] = None,
        ignore_loopback: bool = False,
    ):
        self.cursor_dir = Path(cursor_dir)
        self.ignore_loopback = bool(ignore_loopback)
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.journal_units = list(journal_units or ["ssh", "sshd"])
        self.event_handlers: Dict[str, Callable] = {}
        self.poll_batch_summary_enabled = False
        self.summary_batch_enqueue: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._state_file = self.cursor_dir / _STATE_FILENAME
        self.logger = logging.getLogger(__name__)
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._available: Optional[bool] = None
        # 「取不到游标」的告警只打一次：游标为空时每个轮询周期都会重新对齐，
        # 不去重会把日志刷爆，反而掩盖真正的问题。
        self._align_warned = False

    def add_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler

    def clear_handlers(self) -> None:
        self.event_handlers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        ignore_loopback: Optional[bool] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if ignore_loopback is not None:
            self.ignore_loopback = bool(ignore_loopback)

    def set_poll_batch_summary(
        self,
        enabled: bool,
        enqueue: Optional[Callable[[List[Dict[str, Any]]], None]],
    ) -> None:
        self.poll_batch_summary_enabled = bool(enabled)
        self.summary_batch_enqueue = enqueue

    def is_available(self, *, force_reprobe: bool = False) -> bool:
        if force_reprobe or self._available is None:
            self._available = self._probe()
        return bool(self._available)

    def _probe(self) -> bool:
        """判断本采集链路是否真的可用。

        只看 journalctl 的退出码是不够的：**读不到任何 journal 文件时它同样返回 0**
        （仅在 stderr 输出 "No journal files were found."）。早期实现据此判定「可用」，
        后果是两条路径互相抵消——
          1. `main.py` 因为这里返回 True 而设置 `db_log_poller.skip_ssh_events = True`，
             库内的 Sshd* 事件被丢弃；
          2. 本轮询器又拿不到游标，一条都不推。
        于是 SSH 事件**完全静默丢失**，只留下一条看起来无害的 INFO。

        因此这里改为：必须真的能读出一份 journal 并拿到游标才算可用。
        用**不带 `-u` 过滤**的读取做判据——宿主机 journal 只要被正确挂载，
        就一定有系统自身的条目（内核/服务启动等），因此必然能取到游标；
        取不到即说明容器看不到宿主机的 journal，此时应判定不可用并让库表兜底。
        """
        if not journalctl_available():
            self.logger.warning("未找到 journalctl，无法采集 SSH journal")
            return False
        code, out, err = self._run_journalctl(["-n", "1", "--show-cursor"], with_units=False)
        if code != 0:
            self.logger.warning(
                "journalctl 不可用或无权读取 journal: %s",
                (err or out or "").strip()[:240],
            )
            return False
        if not self._extract_show_cursor(out):
            self.logger.warning(
                "容器读不到宿主机的 journal（journalctl 返回 0 但没有游标，通常还伴随 "
                '"No journal files were found."）。%s'
                "已跳过 SSH journal 采集，改由库表 logger_data 中的 Sshd* 事件兜底。",
                JOURNAL_MOUNT_HINT,
            )
            return False
        return True

    def _unit_args(self) -> List[str]:
        args: List[str] = []
        for u in self.journal_units:
            u = (u or "").strip()
            if u:
                args.extend(["-u", u])
        return args or ["-u", "ssh", "-u", "sshd"]

    def _run_journalctl(self, extra: List[str], with_units: bool = True) -> Tuple[int, str, str]:
        cmd: List[str] = ["journalctl"]
        # 容器内读宿主机 journal 时，若挂载了目录可显式指定
        journal_dir = (os.environ.get("SSH_JOURNAL_DIRECTORY") or "").strip()
        if journal_dir and Path(journal_dir).is_dir():
            cmd.append(f"--directory={journal_dir}")
        if with_units:
            cmd.extend(self._unit_args())
        cmd.extend(["--no-pager", *extra])
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return p.returncode, p.stdout or "", p.stderr or ""
        except FileNotFoundError:
            return 127, "", "journalctl not found"
        except subprocess.TimeoutExpired:
            return 124, "", "journalctl timeout"
        except Exception as e:
            return 1, "", str(e)

    def _load_state(self) -> Dict[str, Any]:
        # boot_id：对齐时记录的内核 boot id，用于识别「宿主机重启后 volatile journal 被清空」
        default = {"version": 1, "cursor": "", "aligned": False, "boot_id": ""}
        try:
            if self._state_file.exists():
                obj = json.loads(self._state_file.read_text(encoding="utf-8") or "{}")
                if isinstance(obj, dict):
                    default.update(obj)
        except Exception as e:
            self.logger.warning("读取 SSH journal 状态失败: %s", e)
        return default

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            self._state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.logger.warning("写入 SSH journal 状态失败: %s", e)

    def _extract_show_cursor(self, text: str) -> str:
        # 末行形如: -- cursor: s=...
        for line in reversed((text or "").splitlines()):
            line = line.strip()
            if line.startswith("-- cursor:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _align_cursor(self, state: Dict[str, Any]) -> None:
        """对齐到当前 journal 末尾，不补推历史。

        先试 `-n 0 --show-cursor`；但部分 systemd 版本在「输出 0 条」时不打印
        `-- cursor:` 行（没有"最后一条"可定位），这种版本下首次对齐永远拿不到游标，
        采集链路会静默卡死。故回退 `-n 1 --show-cursor` 取最后一条的游标——
        之后用 `--after-cursor` 依然会跳过该条，不会补推历史。

        两种方式都拿不到时，把 journalctl 的**实际输出**打进日志：否则这条链路
        失败时完全没有线索（最常见原因是容器没挂载宿主机的 journal 目录与
        /etc/machine-id，此时 journalctl 看到的是空 journal，退出码仍是 0）。
        """
        attempts: List[Tuple[List[str], str, str]] = []
        for args in (["-n", "0", "--show-cursor"], ["-n", "1", "--show-cursor"]):
            code, out, err = self._run_journalctl(args)
            if code != 0:
                self.logger.warning(
                    "SSH journal 对齐失败（journalctl %s）: %s",
                    " ".join(args),
                    (err or out).strip()[:240],
                )
                return
            cur = self._extract_show_cursor(out)
            if cur:
                state["cursor"] = cur
                state["aligned"] = True
                # 记下对齐时的 boot id，用于识别「宿主机重启后 journal 被清空」
                state["boot_id"] = self._current_boot_id()
                self._save_state(state)
                self.logger.info("SSH journal 已对齐当前游标（不推送历史）")
                return
            attempts.append((args, out, err))

        state["aligned"] = True
        # 空 journal 拿不到游标：固定起始时间，后续轮询接收此后首条新日志。
        # 否则「重启后第一条 SSH 事件」会因为拿不到 --after-cursor 而永久丢失。
        state["cursor"] = ""
        state["since"] = f"@{time.time():.6f}"
        self._save_state(state)
        if not self._align_warned:
            self._align_warned = True
            detail = " / ".join(
                f"journalctl {' '.join(a)} → stdout={(o or '').strip()[:100]!r}"
                f" stderr={(e or '').strip()[:100]!r}"
                for a, o, e in attempts
            )
            self.logger.warning(
                "SSH journal 暂无游标（journal 可能为空或不可见），已改用时间起点等待新日志。"
                "%s 本次实际输出：%s",
                JOURNAL_MOUNT_HINT,
                detail,
            )

    def _fetch_entries(self, cursor: str, since: str = "") -> Tuple[List[Dict[str, Any]], str]:
        """返回 (entries, new_cursor)。entries 为 journal -o json 对象列表。

        必须带 ``__CURSOR``（不是 CURSOR），否则游标不前进会每轮重复推送。
        无游标时使用固定起始时间，避免遗漏首条事件或把历史当增量刷屏。
        """
        if not cursor and not since:
            return [], cursor
        extra = [
            "-o",
            "json",
            "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
            "--show-cursor",
        ]
        extra.extend(["--after-cursor", cursor] if cursor else ["--since", since])
        code, out, err = self._run_journalctl(extra)
        if code != 0:
            self.logger.warning("读取 SSH journal 失败: %s", (err or out)[:240])
            return [], cursor
        entries: List[Dict[str, Any]] = []
        new_cursor = cursor
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("-- cursor:"):
                c = line.split(":", 1)[1].strip()
                if c:
                    new_cursor = c
                continue
            if line.startswith("--"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            entries.append(obj)
            c = (obj.get("__CURSOR") or "").strip()
            if c:
                new_cursor = c
        # 本轮无新条目时，--show-cursor 仍可能给出当前位置
        if not entries:
            tail_cur = self._extract_show_cursor(out)
            if tail_cur:
                new_cursor = tail_cur
        return entries, new_cursor

    @staticmethod
    def _ts_from_entry(obj: Dict[str, Any]) -> str:
        raw = obj.get("__REALTIME_TIMESTAMP")
        try:
            # 微秒
            sec = int(raw) / 1_000_000.0
        except (TypeError, ValueError):
            return now_for_display()
        return format_ts_for_display(sec)

    def _emit(self, event_type: str, event_data: Dict[str, Any], raw_msg: str, ts: str) -> None:
        if self.monitor_events and event_type not in self.monitor_events:
            return
        if self.ignore_loopback and should_ignore_loopback_ssh(event_type, event_data):
            return
        handler = self.event_handlers.get(event_type)
        if not handler:
            return
        event_data = dict(event_data)
        event_data.setdefault("_source", "ssh_journal")
        event_data.setdefault("_source_event_id", event_type)
        entry = JournalEntry(
            cursor=f"ssh-journal-{event_type}-{int(time.time())}",
            timestamp=ts,
            hostname="sshd",
            syslog_identifier="sshd",
            message=raw_msg,
            priority=0,
            pid=0,
            raw_data=raw_msg,
            original_line=raw_msg,
        )
        try:
            if self.poll_batch_summary_enabled and self.summary_batch_enqueue:
                rid = hash(raw_msg) & 0x7FFFFFFF
                self.summary_batch_enqueue(
                    [
                        {
                            "row_id": rid,
                            "db_event_id": event_type,
                            "event_type": event_type,
                            "event_data": event_data,
                            "entry": entry,
                            "handler": handler,
                            "source": "ssh_journal",
                        }
                    ]
                )
            else:
                handler(event_data, entry)
        except Exception as e:
            self.logger.error("处理 SSH journal 事件失败 %s: %s", event_type, e, exc_info=True)

    @staticmethod
    def _current_boot_id() -> str:
        """当前内核的 boot id（宿主机的，容器共享同一内核）。

        用于识别「journal 已被重置」：journalctl 的游标是**不透明字符串**，但我们不需要
        解析它——只要记住对齐当时的 boot id，之后比对即可。飞牛的 journal 常是 volatile
        （/run/log/journal，tmpfs），宿主机一重启就清空，此时旧游标指向一个不存在的
        boot，`--after-cursor` 会一直失败而卡住。读不到时返回空串（视为无法判断）。
        """
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _poll_once(self, state: Dict[str, Any]) -> None:
        if not state.get("aligned"):
            self._align_cursor(state)
            return
        # journal 被重置（宿主机重启、volatile journal 清空）时旧游标已失效，重新对齐。
        # 不处理的话 `--after-cursor` 会拿一个属于旧 boot 的游标反复失败，链路再次静默卡死。
        cur_boot = self._current_boot_id()
        saved_boot = str(state.get("boot_id") or "")
        if cur_boot and saved_boot and cur_boot != saved_boot:
            self.logger.info("检测到 journal 已重置（boot id 变化），重新对齐游标")
            state["cursor"] = ""
            state["aligned"] = False
            self._align_cursor(state)
            return
        cursor = str(state.get("cursor") or "")
        since = str(state.get("since") or "")
        if not cursor and not since:
            # 兼容旧版保存的空游标状态；时间只记录一次，空闲轮询不再重新对齐。
            state["since"] = f"@{time.time():.6f}"
            self._save_state(state)
            since = str(state.get("since") or "")
        entries, new_cursor = self._fetch_entries(cursor, since)
        for obj in entries:
            msg = obj.get("MESSAGE")
            if isinstance(msg, list):
                msg = "".join(str(x) for x in msg)
            msg = str(msg or "")
            parsed = parse_ssh_journal_message(msg)
            if not parsed:
                continue
            event_type, event_data = parsed
            self._emit(event_type, event_data, msg, self._ts_from_entry(obj))
        if new_cursor and new_cursor != cursor:
            # 拿到新游标后不再需要时间起点
            state["cursor"] = new_cursor
            state.pop("since", None)
            self._save_state(state)

    def _run_loop(self) -> None:
        state = self._load_state()
        if not state.get("aligned"):
            # since 模式（cursor 为空但有时间起点）也算已对齐，不再重跑
            self._align_cursor(state)
        self.logger.info(
            "SSH journal 轮询启动 units=%s interval=%ss",
            ",".join(self.journal_units),
            self.poll_interval,
        )
        while self.running:
            try:
                if SSH_JOURNAL_EVENTS & self.monitor_events:
                    self._poll_once(state)
            except Exception as e:
                self.logger.error("SSH journal 轮询异常: %s", e, exc_info=True)
            for _ in range(self.poll_interval):
                if not self.running:
                    return
                time.sleep(1)

    def start(self) -> None:
        if self.running:
            return
        if not (SSH_JOURNAL_EVENTS & self.monitor_events):
            self.logger.info("monitor_events 未包含 SSH 事件，跳过 SshJournalPoller")
            return
        if not self.is_available():
            self.logger.warning("SSH journal 不可用，跳过 SshJournalPoller（将回退 logger_data 中的 Sshd*）")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, name="SshJournalPoller", daemon=False)
        self._thread.start()
        self.logger.info("SshJournalPoller 已启动")

    def stop(self) -> None:
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.poll_interval + 2)
        self.logger.info("SshJournalPoller 已停止")
