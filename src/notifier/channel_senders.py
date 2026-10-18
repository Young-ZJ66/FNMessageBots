"""渠道发送层：各推送平台的实际投递实现。

支持渠道：企业微信、钉钉、飞书（结构化优先、格式被拒自动回退纯文本）、通用 Webhook。
ChannelSendersMixin —— 依赖宿主（MultiPlatformNotifier）提供的
connection_pool、配置属性（wechat_webhook_url 等）与 markdown_enabled 开关；
回退与记忆逻辑复用 message_format 的判定工具。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .channel_params import (
    parse_json_object,
    render_webhook_request,
    validate_webhook,
)
from .message_format import (
    MultiPlatformMessage,
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

