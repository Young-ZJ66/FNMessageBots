"""
通用 Webhook、MeoW 渠道的参数解析与校验（配置加载、Web 保存、发送共用）。
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, Optional, Tuple

WEBHOOK_METHODS = ("POST", "GET", "PUT")
WEBHOOK_CONTENT_TYPES = ("json", "form", "text")
WEBHOOK_DEFAULT_BODY = '{"title": "{title}", "content": "{content}"}'
WEBHOOK_PLACEHOLDERS = ("title", "content", "text", "time")

_CONTENT_TYPE_HEADERS = {
    "json": "application/json; charset=utf-8",
    "form": "application/x-www-form-urlencoded; charset=utf-8",
    "text": "text/plain; charset=utf-8",
}


def _is_http_url(v: str) -> bool:
    return v.startswith("http://") or v.startswith("https://")


def parse_json_object(raw: str, label: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"{label}配置不是合法 JSON：{e}"
    if not isinstance(obj, dict):
        return None, f"{label}配置须为 JSON 对象。"
    return obj, ""


# ---------- 通用 Webhook ----------

def parse_headers(raw: Any) -> Tuple[Dict[str, str], str]:
    """支持 {"K": "V"} 或每行一个「K: V」的文本。"""
    if not raw:
        return {}, ""
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}, ""
    out: Dict[str, str] = {}
    for line in str(raw).splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            return {}, f"请求头格式错误（应为「名称: 值」）：{line[:40]}"
        k, v = line.split(":", 1)
        if not k.strip():
            return {}, f"请求头名称为空：{line[:40]}"
        out[k.strip()] = v.strip()
    return out, ""


def webhook_fields(obj: Dict[str, Any]) -> Dict[str, Any]:
    method = str(obj.get("method") or "POST").strip().upper()
    content_type = str(obj.get("content_type") or "json").strip().lower()
    body = obj.get("body")
    body = str(body) if body is not None else ""
    if not body.strip() and method != "GET" and content_type == "json":
        body = WEBHOOK_DEFAULT_BODY
    return {
        "url": str(obj.get("url") or "").strip(),
        "method": method,
        "content_type": content_type,
        "headers": obj.get("headers") or "",
        "body": body,
    }


def _fill(template: str, values: Dict[str, str], encode) -> str:
    out = template
    for k in WEBHOOK_PLACEHOLDERS:
        out = out.replace("{" + k + "}", encode(values.get(k, "")))
    return out


def render_webhook_request(
    obj: Dict[str, Any], values: Dict[str, str]
) -> Tuple[Optional[Dict[str, Any]], str]:
    """按占位符生成 {method, url, headers, body}；失败返回 (None, 错误说明)。"""
    f = webhook_fields(obj)
    url = _fill(f["url"], values, lambda v: urllib.parse.quote(v, safe=""))
    headers, err = parse_headers(f["headers"])
    if err:
        return None, err
    body: Optional[str] = None
    if f["method"] != "GET" and f["body"].strip():
        ct = f["content_type"]
        if ct == "json":
            body = _fill(f["body"], values, lambda v: json.dumps(v, ensure_ascii=False)[1:-1])
            try:
                json.loads(body)
            except json.JSONDecodeError as e:
                return None, f"请求体替换占位符后不是合法 JSON：{e}"
        elif ct == "form":
            body = _fill(f["body"], values, lambda v: urllib.parse.quote_plus(v))
        else:
            body = _fill(f["body"], values, lambda v: v)
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = _CONTENT_TYPE_HEADERS[ct]
    return {"method": f["method"], "url": url, "headers": headers, "body": body}, ""


def validate_webhook(obj: Dict[str, Any]) -> str:
    f = webhook_fields(obj)
    if not _is_http_url(f["url"]):
        return "通用 Webhook 地址须为 http(s) 地址。"
    if f["method"] not in WEBHOOK_METHODS:
        return f"通用 Webhook 请求方式仅支持 {'/'.join(WEBHOOK_METHODS)}。"
    if f["content_type"] not in WEBHOOK_CONTENT_TYPES:
        return "通用 Webhook 内容类型仅支持 json / form / text。"
    sample = {k: f"示例\"{k}\"\n" for k in WEBHOOK_PLACEHOLDERS}
    _, err = render_webhook_request(obj, sample)
    return f"通用 Webhook {err}" if err else ""


def validate_channel_json(ch_type: str, raw: str) -> str:
    """校验通用 Webhook 的单条 JSON 配置，返回错误说明（空串表示通过）。"""
    if ch_type != "webhook":
        raise KeyError(f"未知渠道类型: {ch_type}")
    label = "通用 Webhook"
    obj, err = parse_json_object(raw, label)
    if err:
        return err
    assert obj is not None
    return validate_webhook(obj)
