"""渠道发送层：各推送平台的实际投递实现。

ChannelSendersMixin —— 依赖宿主（MultiPlatformNotifier）提供的
connection_pool、配置属性（wechat_webhook_url 等）与 markdown_enabled 开关；
结构化消息被渠道拒绝时自动回退纯文本，回退与记忆逻辑复用 message_format 的判定工具。
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.parse
from datetime import datetime
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Dict, List, Optional, Tuple

from .channel_params import (
    meow_fields,
    parse_json_object,
    render_webhook_request,
    validate_meow,
    validate_webhook,
)
from .message_format import (
    MEOW_STATUS_HINTS,
    MultiPlatformMessage,
    PUSHPLUS_URL,
    _SECURITY_HINT,
    _looks_like_format_rejection,
    _looks_like_security_rejection,
    _remember_rich_text_rejection,
    _rejection_text,
    rich_text_rejected_channels,
)

_logger = logging.getLogger(__name__)


class ChannelSendersMixin:
    """各渠道投递实现（宿主需提供 connection_pool 与渠道配置属性）。"""

    def _iter_urls(self, raw: str):
        """将配置的URL拆分为列表，支持使用 '|' 分隔配置多个地址。"""
        if not raw:
            return []
        return [u.strip() for u in str(raw).split('|') if u.strip()]

    def _channel_result(self, channel_name: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """从多次请求结果聚合成一条渠道结果（多 URL 取首个成功或最后一条）。"""
        if not results:
            return {"channel": channel_name, "success": False, "response": None, "error": "未请求"}
        ok = next((r for r in results if r.get("success")), None)
        last = results[-1]
        if ok:
            return {"channel": channel_name, "success": True, "response": ok.get("response"), "error": None}
        return {
            "channel": channel_name,
            "success": False,
            "response": last.get("response"),
            "error": last.get("error") or "请求失败",
        }

    def _send_structured_first(
        self,
        channel_name: str,
        urls: List[str],
        structured_payload: Dict[str, Any],
        plain_payload: Dict[str, Any],
    ) -> tuple:
        """先发结构化（富文本）消息，渠道拒绝时自动回退纯文本的一次发送流程。

        企业微信/钉钉/飞书的差异只在 payload 形态，分发与回退逻辑完全一致，收敛在此处；
        多 webhook 场景下每个地址都有独立的回退机会，保证不因富文本被拒而丢通知。

        若该渠道此前已被确认拒绝富文本（格式类错误），直接发纯文本，避免每条通知
        都先失败一次——那会污染推送记录里的失败统计。
        """
        if channel_name in rich_text_rejected_channels():
            results = [self.connection_pool.post(url, plain_payload) for url in urls]
            return any(r.get("success") for r in results), self._channel_result(channel_name, results)

        results = [self.connection_pool.post(url, structured_payload) for url in urls]
        if any(r.get("success") for r in results):
            return True, self._channel_result(channel_name, results)

        failure_errors = [str(r.get("error") or "") for r in results if not r.get("success")]
        self.logger.warning(
            "%s 结构化消息发送失败，回退纯文本: %s",
            channel_name,
            failure_errors[0] if failure_errors else "未知原因",
        )
        failure_reasons = [_rejection_text(r) for r in results if not r.get("success")]
        if any(_looks_like_security_rejection(t) for t in failure_reasons):
            # 安全设置没通过：回退纯文本也会同样失败，且不应记忆为「不支持富文本」
            self.logger.error("%s %s", channel_name, _SECURITY_HINT)
        elif any(_looks_like_format_rejection(t) for t in failure_reasons):
            _remember_rich_text_rejection(
                channel_name, failure_errors[0] or (failure_reasons[0] if failure_reasons else "格式被拒绝")
            )

        fallback = [self.connection_pool.post(url, plain_payload) for url in urls]
        ok = any(r.get("success") for r in fallback)
        # 失败时只报结构化那一轮，避免把「格式被拒」误报成渠道不可用；成功时合并以便记录实际结果
        merged = results + fallback if ok else results
        return ok, self._channel_result(channel_name, merged)

    def _send_to_wechat(self, message: MultiPlatformMessage) -> tuple:
        """发送到企业微信。优先 markdown（结构化），失败自动回退纯文本。"""
        urls = self._iter_urls(self.wechat_webhook_url)
        if not urls:
            return False, self._channel_result("企业微信", [])
        if self.markdown_enabled:
            return self._send_structured_first(
                "企业微信", urls, message.to_wechat_markdown_format(), message.to_wechat_format()
            )
        results = [self.connection_pool.post(url, message.to_wechat_format()) for url in urls]
        return any(r.get("success") for r in results), self._channel_result("企业微信", results)

    def _send_to_dingtalk(self, message: MultiPlatformMessage) -> tuple:
        """发送到钉钉。优先 markdown（结构化），失败自动回退纯文本。"""
        urls = self._iter_urls(self.dingtalk_webhook_url)
        if not urls:
            return False, self._channel_result("钉钉", [])
        if self.markdown_enabled:
            return self._send_structured_first(
                "钉钉", urls, message.to_dingtalk_markdown_format(), message.to_dingtalk_format()
            )
        results = [self.connection_pool.post(url, message.to_dingtalk_format()) for url in urls]
        return any(r.get("success") for r in results), self._channel_result("钉钉", results)

    def _send_to_feishu(self, message: MultiPlatformMessage) -> tuple:
        """发送到飞书。优先富文本 post（结构化），失败自动回退纯文本。"""
        urls = self._iter_urls(self.feishu_webhook_url)
        if not urls:
            return False, self._channel_result("飞书", [])
        if self.markdown_enabled:
            return self._send_structured_first(
                "飞书", urls, message.to_feishu_post_format(), message.to_feishu_format()
            )
        results = [self.connection_pool.post(url, message.to_feishu_format()) for url in urls]
        return any(r.get("success") for r in results), self._channel_result("飞书", results)

    def _bark_key_from_url(self, raw_url: str) -> str:
        """从 Bark URL 中提取 key（路径最后一段）。"""
        return raw_url.rstrip('/').rsplit('/', 1)[-1] if '/' in raw_url else raw_url

    def _bark_base_url(self, raw_url: str) -> str:
        """从 Bark URL 中提取 base URL（scheme + host）。"""
        parts = urllib.parse.urlsplit(raw_url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else "https://api.day.app"

    def _send_to_bark(self, message: MultiPlatformMessage) -> tuple:
        """发送到Bark（POST JSON，避免 URL 编码问题）。"""
        urls = self._iter_urls(self.bark_url)
        if not urls:
            return False, {"channel": "Bark", "success": False, "response": None, "error": "未配置"}
        results = []
        for raw_url in urls:
            push_url = f"{self._bark_base_url(raw_url)}/push"
            payload = {
                "device_key": self._bark_key_from_url(raw_url),
                "title": message.title,
                "body": message.content,
            }
            if self.bark_icon:
                payload["icon"] = self.bark_icon
            results.append(self.connection_pool.post(push_url, payload))
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("Bark", results)

    def _send_to_pushplus(self, message: MultiPlatformMessage) -> tuple:
        """发送到 PushPlus。返回 (是否有任一成功, 渠道结果 dict)。"""
        param_list = self._iter_urls(self.pushplus_params)
        if not param_list:
            return False, {"channel": "PushPlus", "success": False, "response": None, "error": "未配置"}
        results = []
        for param_str in param_list:
            try:
                payload = json.loads(param_str)
                if not isinstance(payload, dict) or 'token' not in payload:
                    continue
                user_title = (payload.get('title') or '').strip()
                if user_title == '{title}':
                    final_title = message.title
                    final_content = message.content
                else:
                    final_title = user_title or message.title
                    final_content = message.merged_plain_text(blank_line_between=True)
                payload['title'] = final_title
                payload['content'] = final_content
                results.append(self.connection_pool.post(PUSHPLUS_URL, payload))
            except json.JSONDecodeError:
                results.append({"success": False, "response": None, "error": "参数 JSON 解析失败"})
            except Exception as e:
                results.append({"success": False, "response": None, "error": str(e)[:80]})
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("PushPlus", results)

    def _send_to_smtp(self, message: MultiPlatformMessage) -> tuple:
        """发送到 SMTP 邮件。返回 (是否有任一成功, 渠道结果 dict)。"""
        param_list = self._iter_urls(self.smtp_params)
        if not param_list:
            return False, {"channel": "SMTP邮件", "success": False, "response": None, "error": "未配置"}
        results = []
        for param_str in param_list:
            try:
                cfg = json.loads(param_str)
                if not isinstance(cfg, dict):
                    results.append({"success": False, "response": None, "error": "参数须为 JSON 对象"})
                    continue
                server = (cfg.get("server") or "").strip()
                username = (cfg.get("username") or "").strip()
                password = cfg.get("password") or ""
                to_raw = (cfg.get("to") or "").strip()
                from_addr = (cfg.get("from") or username).strip()
                if not server or not username or not password or not to_raw:
                    results.append({"success": False, "response": None, "error": "缺少 server/username/password/to"})
                    continue

                recipients = [x.strip() for x in to_raw.split(",") if x.strip()]
                if not recipients:
                    results.append({"success": False, "response": None, "error": "收件人不能为空"})
                    continue

                try:
                    port = int(cfg.get("port", 465))
                except (TypeError, ValueError):
                    results.append({"success": False, "response": None, "error": "端口必须是整数"})
                    continue
                em = EmailMessage()
                em["Subject"] = (message.title or "").strip() or "系统通知"
                if from_addr and "@" not in from_addr:
                    # 用户填写昵称时，按 RFC 组装为“昵称 <登录邮箱>”。
                    em["From"] = formataddr((str(Header(from_addr, "utf-8")), username))
                else:
                    em["From"] = from_addr or username
                em["To"] = ", ".join(recipients)
                body = (message.content or "").strip()
                if not body and message.title:
                    body = (message.title or "").strip()
                em.set_content(body or "系统通知")

                # 按端口自动判定加密方式：465 用 SMTP_SSL，其它端口走 STARTTLS。
                if port == 465:
                    with smtplib.SMTP_SSL(server, port, timeout=15, context=ssl.create_default_context()) as client:
                        client.login(username, password)
                        # 兼容多数服务商限制：信封发件人必须是登录账号。
                        client.send_message(em, from_addr=username, to_addrs=recipients)
                else:
                    with smtplib.SMTP(server, port, timeout=15) as client:
                        client.ehlo()
                        client.starttls(context=ssl.create_default_context())
                        client.ehlo()
                        client.login(username, password)
                        # 兼容多数服务商限制：信封发件人必须是登录账号。
                        client.send_message(em, from_addr=username, to_addrs=recipients)

                results.append({
                    "success": True,
                    "response": {"to": recipients, "port": port, "secure_mode": ("ssl" if port == 465 else "starttls")},
                    "error": None
                })
            except json.JSONDecodeError:
                results.append({"success": False, "response": None, "error": "参数 JSON 解析失败"})
            except Exception as e:
                results.append({"success": False, "response": None, "error": str(e)[:120]})
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("SMTP邮件", results)

    def _send_to_webhook(self, message: MultiPlatformMessage) -> tuple:
        """通用 Webhook：按用户配置的方法、请求头、请求体模板发送。"""
        param_list = self._iter_urls(self.webhook_params)
        if not param_list:
            return False, {"channel": "通用Webhook", "success": False, "response": None, "error": "未配置"}
        values = {
            "title": message.title or "",
            "content": message.content or "",
            "text": message.merged_plain_text(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        results = []
        for param_str in param_list:
            obj, err = parse_json_object(param_str, "通用 Webhook")
            if err or obj is None:
                results.append({"success": False, "response": None, "error": err})
                continue
            err = validate_webhook(obj)
            if err:
                results.append({"success": False, "response": None, "error": err})
                continue
            req, err = render_webhook_request(obj, values)
            if err or req is None:
                results.append({"success": False, "response": None, "error": err})
                continue
            results.append(
                self.connection_pool.request(
                    req["method"], req["url"], body=req["body"], headers=req["headers"]
                )
            )
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("通用Webhook", results)

    def _send_to_meow(self, message: MultiPlatformMessage) -> tuple:
        """MeoW：POST {api_base}/{昵称}，JSON 携带 title / msg；业务结果看返回体 status。"""
        param_list = self._iter_urls(self.meow_params)
        if not param_list:
            return False, {"channel": "MeoW", "success": False, "response": None, "error": "未配置"}
        # 极简推送时 content 为空、整行在 title；MeoW 的 msg 必填，若 title/msg 都用同一行会重复显示
        title = (message.title or "").strip() or "MeoW"
        content = (message.content or "").strip()
        if content:
            msg = content
        else:
            msg = title
            title = (self.title_prefix or "").strip() or "MeoW"
        results = []
        for param_str in param_list:
            obj, err = parse_json_object(param_str, "MeoW")
            if err or obj is None:
                results.append({"success": False, "response": None, "error": err})
                continue
            err = validate_meow(obj)
            if err:
                results.append({"success": False, "response": None, "error": err})
                continue
            f = meow_fields(obj)
            key = (f["api_base"], f["nickname"])
            if key in self._meow_unregistered:
                results.append({
                    "success": False,
                    "response": None,
                    "error": f"昵称「{f['nickname']}」未注册，已暂停推送以免 IP 被封禁；请修改昵称并保存配置",
                })
                continue
            payload: Dict[str, Any] = {"title": title, "msg": msg}
            if f["url"]:
                payload["url"] = f["url"]
            result = self.connection_pool.post(
                f"{f['api_base']}/{urllib.parse.quote(f['nickname'], safe='')}", payload
            )
            body = result.get("response") if isinstance(result.get("response"), dict) else {}
            status = body.get("status")
            if result.get("success") and status is not None and str(status) != "200":
                if str(status) == "404":
                    self._meow_unregistered.add(key)
                hint = MEOW_STATUS_HINTS.get(str(status), "")
                detail = body.get("message") or body.get("msg") or f"status={status}"
                result = {**result, "success": False, "error": f"{detail}{'（' + hint + '）' if hint else ''}"}
            results.append(result)
        any_ok = any(r.get("success") for r in results)
        return any_ok, self._channel_result("MeoW", results)
