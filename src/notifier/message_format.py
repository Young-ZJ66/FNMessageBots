"""消息格式层：各渠道消息体上限、截断、纯文本→markdown 转换、渠道分发注册表。

从 MultiPlatformNotifier 拆出的无状态工具层——
MultiPlatformMessage 与各平台 payload 的组装、字节/字符上限与富文本能力记忆都在这里。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# PushPlus 固定接口地址（HTTPS：token 与消息正文不再明文传输）
PUSHPLUS_URL = "https://www.pushplus.plus/send"

# MeoW 返回体 status（HTTP 状态统一为 200）
MEOW_STATUS_HINTS = {
    "400": "标题或消息参数错误",
    "403": "内容策略禁止发送",
    "404": "昵称未注册，请确认 MeoW App 中的昵称；同一 IP 一天内输错 3 次会被封禁",
    "429": "发送频率超限，非会员 3 秒 1 条、每分钟 15 条、每小时 60 条",
    "500": "MeoW 服务器错误",
}


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return text or ""
    return raw[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."

# Docker Engine 容器事件（与 monitor.docker_events_poller 中映射一致）
DOCKER_CONTAINER_EVENT_TYPES = frozenset({
    "DOCKER_CONTAINER_CREATE",
    "DOCKER_CONTAINER_START",
    "DOCKER_CONTAINER_STOP",
    "DOCKER_CONTAINER_DIE",
    "DOCKER_CONTAINER_OOM",
    "DOCKER_CONTAINER_KILL",
    "DOCKER_CONTAINER_PAUSE",
    "DOCKER_CONTAINER_UNPAUSE",
    "DOCKER_CONTAINER_RESTART",
    "DOCKER_CONTAINER_DESTROY",
})

# 渠道「不支持富文本」的记忆（进程级）：确认过一次格式被拒后，后续直接发纯文本，
# 避免每条通知都先失败一次——那会把推送记录里的失败数撑起来，掩盖真正需要关注的失败。
# 进程重启后重新探测一次；平台升级支持富文本时不需要改配置。
_RICH_TEXT_REJECTED_CHANNELS: Dict[str, str] = {}
_RICH_TEXT_REJECTED_LOCK = threading.Lock()

# 明确指向「消息格式/类型不被接受」的错误特征；命中才记忆，
# 避免把超时、鉴权失败、限流等临时问题误判成渠道能力不足。
#
# 注意：这里**只按文本特征判断，不按错误码**。各平台错误码会变、且不同接口编码体系不同，
# 早期版本曾按错误码判断（写入 19021/19024 等），实际钉钉自定义机器人的群消息校验错误
# 统一返回 errcode=310000 + errmsg 文本，那些编码是错的。文本特征更稳。
_FORMAT_REJECT_HINTS = (
    "invalid message type",
    "invalid msgtype",
    "invalid msg_type",
    "unsupported message type",
    "unsupported msgtype",
    "not support",           # 如 "message type not support"
    "message type",
    "msgtype",
    "格式",                  # 中文文案：格式不正确 / 不支持的格式
    "不支持",
)

# 平台侧「安全设置/鉴权」类错误：回退纯文本同样会失败，因此既不能记忆为
# 「渠道不支持富文本」，也不该报成「渠道不可用」——要给出指向安全配置的提示。
# 依据：钉钉自定义机器人群消息校验失败统一返回 errcode=310000，errmsg 为下列文本之一。
_SECURITY_REJECT_HINTS = (
    "keywords not in content",     # 消息未包含自定义关键词
    "sign not match",              # 加签不匹配
    "invalid timestamp",           # 加签时间戳无效
    "not in whitelist",            # 来源 IP 不在白名单
    "keyword",                     # 中文/简写提示兜底
    "关键词",
    "签名",
    "加签",
    "白名单",
)

_SECURITY_HINT = (
    "该错误来自机器人「安全设置」（关键词/加签/IP 白名单），不是消息格式问题；"
    "请到该机器人安全设置中核对：关键词需出现在消息文本内、或改用加签、或放行本机出口 IP"
)


def _looks_like_security_rejection(error: str) -> bool:
    """错误信息是否指向机器人安全设置（关键词/加签/IP 白名单）未通过。"""
    msg = (error or "").strip().lower()
    if not msg:
        return False
    return any(hint in msg for hint in _SECURITY_REJECT_HINTS)


def _rejection_text(result: Dict[str, Any]) -> str:
    """把一次失败结果里可用于判断原因的文本拼起来。

    ConnectionPool 对 HTTP 4xx/5xx 只给出 "HTTP 400"，而「格式不被接受」的线索
    在返回体里（如 errcode/errmsg、msgtype 等），因此必须一并纳入判断。
    """
    parts = [str(result.get("error") or "")]
    resp = result.get("response")
    if isinstance(resp, dict):
        for key in ("errcode", "code", "StatusCode", "errmsg", "msg", "message", "text_preview"):
            val = resp.get(key)
            if val not in (None, ""):
                parts.append(str(val))
        inner = resp.get("json")
        if isinstance(inner, dict):
            for key in ("errcode", "code", "errmsg", "msg", "message"):
                val = inner.get(key)
                if val not in (None, ""):
                    parts.append(str(val))
    elif resp is not None:
        parts.append(str(resp))
    return " ".join(p for p in parts if p)


def _looks_like_format_rejection(error: str) -> bool:
    """错误信息是否指向「渠道不接受该消息格式」（而非安全设置/网络/鉴权/限流问题）。"""
    msg = (error or "").strip().lower()
    if not msg:
        return False
    # 安全设置类错误：回退纯文本同样会失败，不属于格式问题
    if _looks_like_security_rejection(msg):
        return False
    # 网络类与鉴权类明确排除
    for transient in (
        "timeout", "timed out", "connection", "resolve", "dns", "ssl", "tls",
        "unauthorized", "forbidden", "401", "403", "429", "rate", "limit", "locked", "busy",
    ):
        if transient in msg:
            return False
    return any(hint in msg for hint in _FORMAT_REJECT_HINTS)


def rich_text_rejected_channels() -> Dict[str, str]:
    """返回已被确认拒绝富文本的渠道（渠道名 → 触发原因），供排障与测试使用。"""
    with _RICH_TEXT_REJECTED_LOCK:
        return dict(_RICH_TEXT_REJECTED_CHANNELS)


def reset_rich_text_rejected_channels() -> None:
    """清除记忆（测试或「渠道升级后再试一次」时使用）。"""
    with _RICH_TEXT_REJECTED_LOCK:
        _RICH_TEXT_REJECTED_CHANNELS.clear()


def _remember_rich_text_rejection(channel: str, reason: str) -> None:
    import logging

    with _RICH_TEXT_REJECTED_LOCK:
        first_time = channel not in _RICH_TEXT_REJECTED_CHANNELS
        _RICH_TEXT_REJECTED_CHANNELS[channel] = reason
    if first_time:
        logging.getLogger(__name__).warning(
            "%s 不接受结构化消息（%s），后续直接发送纯文本；重启进程可重新探测",
            channel,
            reason,
        )


def normalize_disk_io_payload(ed: Dict[str, Any]) -> Tuple[str, str, str, Any]:
    """DISK_IO_ERR：兼容 parameter 内 data.DEV 与顶层 disk/model/serial（与 DiskWakeup 扁平结构一致）。"""
    nested = ed.get("data")
    data = nested if isinstance(nested, dict) else {}
    dev = (data.get("DEV") or data.get("dev") or ed.get("disk") or ed.get("dev") or "")
    dev = str(dev).strip()
    model = (data.get("MODEL") or data.get("model") or ed.get("model") or "")
    model = str(model).strip()
    sn = (data.get("SN") or data.get("sn") or ed.get("serial") or "")
    sn = str(sn).strip()
    err_cnt = data.get("ERR_CNT", data.get("err_cnt"))
    if err_cnt is None:
        err_cnt = ed.get("ERR_CNT", ed.get("err_cnt", 0))
    return dev, model, sn, err_cnt


# ── 渠道分发注册表 ────────────────────────────────────────────────────────────
# 三个发送入口（普通事件 / 合并磁盘事件 / 系统通知）历史上各抄了一份
# 「if 有参数 → 构造消息 → 发送 → 记录」的链条，加渠道要改三处、极易漏改。
# 这里集中登记，分发逻辑收敛到 MultiPlatformNotifier._dispatch_channels。
@dataclass(frozen=True)
class ChannelSpec:
    """一个推送渠道的登记信息。

    key:          渠道标识（与 Web 配置页 / active_platforms 一致）
    label:        日志与结果里的中文名
    param_attr:   配置属性名；取值空则跳过该渠道
    sender:       发送方法名（形如 _send_to_xxx，返回 (是否成功, 渠道结果)）
    """

    key: str
    label: str
    param_attr: str
    sender: str


# 顺序即发送顺序（与历史行为一致）
CHANNEL_SPECS: Tuple[ChannelSpec, ...] = (
    ChannelSpec("wechat", "企业微信", "wechat_webhook_url", "_send_to_wechat"),
    ChannelSpec("dingtalk", "钉钉", "dingtalk_webhook_url", "_send_to_dingtalk"),
    ChannelSpec("feishu", "飞书", "feishu_webhook_url", "_send_to_feishu"),
    ChannelSpec("webhook", "通用Webhook", "webhook_params", "_send_to_webhook"),
)


# ── 各渠道消息体上限（官方文档）────────────────────────────────────────────────
# 企业微信 markdown/markdown_v2：content 最长 4096 字节、必须 utf8
WECOM_CONTENT_MAX_BYTES = 4096
# 钉钉 markdown：text 最大 5000 字符；title 有独立上限（官方 64 字符，取保守值）
DINGTALK_TEXT_MAX_CHARS = 5000
DINGTALK_TITLE_MAX_CHARS = 64
# 飞书自定义机器人：请求体不超过 20KB（留 2KB 给 JSON 结构与其它字段）
FEISHU_BODY_MAX_BYTES = 18 * 1024
# 截断时预留的余量，避免「正好等于上限」仍被平台侧判超限
_TRUNCATE_MARGIN = 16
TRUNCATE_SUFFIX = "…（内容过长已截断）"


def _truncate_chars(text: str, max_chars: int, suffix: str = TRUNCATE_SUFFIX) -> str:
    """按字符数截断（用于钉钉的 5000 字符上限，该限制按字符计）。"""
    s = str(text or "")
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    keep = max(0, max_chars - len(suffix))
    return s[:keep] + suffix


def _truncate_bytes(text: str, max_bytes: int, suffix: str = TRUNCATE_SUFFIX) -> str:
    """按 UTF-8 字节数截断（用于企业微信 4096 字节上限），不切坏多字节字符。"""
    s = str(text or "")
    if max_bytes <= 0:
        return ""
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    budget = max(0, max_bytes - len(suffix.encode("utf-8")))
    raw = s.encode("utf-8")[:budget]
    return raw.decode("utf-8", errors="ignore") + suffix


def _markdown_body_from_plain(text: str) -> str:
    """把纯文本正文转成 markdown 结构。只做两件事：

    1. 「【组名】」分组标题行转加粗；
    2. 去除行首缩进（企微 markdown 会吃掉空白，缩进没有意义）。

    刻意**不生成列表标记**：企微群机器人 markdown 不支持列表语法，
    "- " 会原样显示为字符；明细行直接换行即可正确分行。
    """
    out_lines: List[str] = []
    for raw in str(text or "").split("\n"):
        stripped = raw.strip()
        if not stripped:
            out_lines.append("")
            continue
        group_m = re.match(r"^【(.+?)】$", stripped)
        if group_m:
            out_lines.append(f"**{group_m.group(1).strip()}**")
        else:
            out_lines.append(stripped)
    return "\n".join(out_lines).strip("\n")


def _feishu_post_content(title: str, body: str, *, max_segments: int = 400) -> Dict[str, Any]:
    """飞书自定义机器人富文本（msg_type=post）内容。

    post 需要按段落拆分，逐段只放一个文本片段即可保持原文行序与换行；
    这里是纯结构转换，不做任何文案改写。
    """
    paragraphs: List[List[Dict[str, Any]]] = []
    for raw in str(body or "").split("\n"):
        if len(paragraphs) >= max_segments:
            break
        paragraphs.append([{"tag": "text", "text": raw.strip()}])
    if not paragraphs:
        paragraphs = [[{"tag": "text", "text": ""}]]
    return {
        "title": (title or "飞牛NAS")[:200],
        "content": paragraphs,
    }


@dataclass
class MultiPlatformMessage:
    """多平台消息"""

    title: str = ""
    content: str = ""
    markdown: str = ""

    def merged_plain_text(self, *, blank_line_between: bool = False) -> str:
        """标题与正文合并为一段纯文本。正文为空时不追加分隔符，避免极简模式末尾多余空行。"""
        title = self.title or ""
        content_raw = self.content or ""
        if not content_raw.strip():
            return title
        sep = "\n\n" if blank_line_between else "\n"
        return f"{title}{sep}{content_raw}"

    def merged_markdown(self, *, blank_line_between: bool = True) -> str:
        """标题与正文合并为 markdown；未预渲染时回退为纯文本正文的等价结构。

        标题使用「##」二级标题样式：企微/钉钉 markdown 渲染为大号字体，
        与概要、明细在视觉上分层（加粗 ** 不改变字号，标题会与概要混同）。
        """
        title = (self.title or "").strip()
        body = self.markdown if self.markdown.strip() else _markdown_body_from_plain(self.content or "")
        if not body.strip():
            return f"## {title}" if title else ""
        sep = "\n\n" if blank_line_between else "\n"
        return f"## {title}{sep}{body}"

    def to_wechat_format(self) -> Dict[str, Any]:
        """转换为企业微信格式（纯文本）"""
        return {
            "msgtype": "text",
            "text": {
                "content": self.merged_plain_text()
            }
        }

    def to_wechat_markdown_format(self) -> Dict[str, Any]:
        """企业微信群机器人 markdown（msgtype=markdown，content ≤ 4096 字节，按字节截断）。"""
        content = _truncate_bytes(self.merged_markdown(), WECOM_CONTENT_MAX_BYTES - _TRUNCATE_MARGIN)
        return {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }

    def to_dingtalk_format(self) -> Dict[str, Any]:
        """转换为钉钉格式（纯文本）"""
        return {
            "msgtype": "text",
            "text": {
                "content": self.merged_plain_text()
            }
        }

    def to_dingtalk_markdown_format(self) -> Dict[str, Any]:
        """钉钉自定义机器人 markdown（text ≤ 5000 字符，title 有独立上限）。"""
        title = _truncate_chars(self.title or "飞牛NAS", DINGTALK_TITLE_MAX_CHARS)
        body = self.merged_markdown()
        # text 上限按字符计；标题也会在首屏展示，因此预留「标题 + 换行」的长度
        budget = DINGTALK_TEXT_MAX_CHARS - _TRUNCATE_MARGIN - len(title) - 1
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": _truncate_chars(body, max(budget, 0)),
            },
        }

    def to_feishu_format(self) -> Dict[str, Any]:
        """转换为飞书格式（纯文本）"""
        return {
            "msg_type": "text",
            "content": {
                "text": self.merged_plain_text()
            }
        }

    def to_feishu_post_format(self) -> Dict[str, Any]:
        """飞书自定义机器人富文本 post（结构化：标题 + 逐行段落），正文按 20KB 上限截断。"""
        body = self.markdown if self.markdown.strip() else _markdown_body_from_plain(self.content or "")
        title = _truncate_chars(self.title or "", 200)
        body = _truncate_bytes(body, FEISHU_BODY_MAX_BYTES)
        return {
            "msg_type": "post",
            "content": {"post": {"zh_cn": _feishu_post_content(title, body)}},
        }
