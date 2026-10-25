"""飞牛 fnOS WebSocket 监控接口客户端：巡检硬盘温度/健康的第一优先数据源。

读系统自带接口的好处：不需要块设备权限（容器里没有 /dev/sdX、native 下
应用账号不在 disk 组都能用），且与飞牛「硬盘信息」界面同源。协议要点
（依据社区 fn_nas 等项目的公开梳理，独立实现）：

- 地址 {scheme}://{host}:{port}/websocket?type=main，ws 默认 5666、wss 默认 5667
- user.login 明文发送，响应携带 secret（base64）；其后每个请求体 =
  base64(HMAC-SHA256(b64decode(secret), 紧凑JSON)) + 紧凑JSON（签名在前）
- appcgi.resmon.disk → data.disk[]，每项含 name / temp（摄氏）
- stor.diskSmart {disk: sda} → smartctl 风格 JSON（temperature.current、
  power_on_time.hours、smart_status.passed）；休眠盘返回 {"diskStandby": 1}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Dict, Optional, Tuple

_WS_PATH = "/websocket?type=main"
_LOGIN_DID = "fnmb-patrol"  # 固定设备 ID，避免在 fnOS 设备列表里反复新增条目
_WS_DEFAULT_PORTS = {"ws": 5666, "wss": 5667}


class FnosWsError(Exception):
    """fnOS 监控接口错误（地址无效、连接失败、认证失败、响应异常）。"""


def build_ws_url(base: str) -> str:
    """把用户填写的地址规范化为 ws(s)://host:port/websocket?type=main。

    接受 host[:port] / ws://host[:port] / wss://host[:port]；端口缺省按
    scheme 补默认值。仅接受 ws/wss——这是内网管理接口，不接受其它协议。
    """
    raw = str(base or "").strip()
    if not raw:
        raise FnosWsError("飞牛监控接口地址为空")
    m = re.match(r"^(wss?)://([^/:?#]+)(?::(\d+))?(?:/.*)?$", raw, re.IGNORECASE)
    if m:
        scheme, host, port = m.group(1).lower(), m.group(2), m.group(3)
    else:
        m = re.match(r"^([^/:?#]+)(?::(\d+))?$", raw)
        if not m:
            raise FnosWsError(f"飞牛监控接口地址无效：{raw}（示例 ws://192.168.5.66:5666）")
        scheme, host, port = "ws", m.group(1), m.group(2)
    if not port:
        port = str(_WS_DEFAULT_PORTS[scheme])
    return f"{scheme}://{host}:{port}{_WS_PATH}"


def sign_payload(secret_b64: str, body: Dict[str, Any]) -> str:
    """按 fnOS 协议签名请求体：base64(HMAC-SHA256(secret, 紧凑JSON)) + 紧凑JSON。"""
    key = base64.b64decode(str(secret_b64 or ""))
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    sig = hmac.new(key, raw.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("ascii") + raw


def make_reqid(prefix: str, back_id: str, seq: int) -> str:
    """fnOS 请求 ID：随机前缀 + backId 前 8 位（不足补 0）+ 8 位十六进制序号。"""
    bid = re.sub(r"[^0-9a-fA-F]", "", str(back_id or ""))[:8].ljust(8, "0")
    return f"{prefix}{bid}{seq & 0xFFFFFFFF:08x}"


def parse_resmon_disk(resp: Dict[str, Any]) -> Dict[str, str]:
    """appcgi.resmon.disk 响应 → {设备名: 温度字符串摄氏}；无效项跳过。"""
    out: Dict[str, str] = {}
    data = (resp or {}).get("data")
    arr = data.get("disk") if isinstance(data, dict) else None
    if not isinstance(arr, list):
        return out
    for item in arr:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            c = float(item.get("temp"))
        except (TypeError, ValueError):
            continue
        if -20.0 <= c <= 120.0:
            out[name] = str(int(c)) if float(c).is_integer() else f"{c:.1f}"
    return out


def parse_disk_smart(resp: Dict[str, Any]) -> Dict[str, Any]:
    """stor.diskSmart 响应 → {standby, temp, power_on_hours, passed}。

    smartctl JSON 可能裸返，也可能包在 data/smart 里；休眠盘返回
    {"diskStandby": 1, "result": "succ"}，此时 standby=True 且无 SMART 字段。
    """
    obj: Any = resp or {}
    standby = bool(obj.get("diskStandby"))
    out: Dict[str, Any] = {"standby": standby, "temp": "", "power_on_hours": "", "passed": None}
    if standby:
        return out
    for wrapper in ("smart", "data"):
        inner = obj.get(wrapper)
        if isinstance(inner, dict) and (
            inner.get("temperature") or inner.get("smart_status")
            or inner.get("power_on_time") or inner.get("model_name")
        ):
            obj = inner
            break
    temp_obj = obj.get("temperature")
    if isinstance(temp_obj, dict):
        try:
            c = float(temp_obj.get("current"))
            if -20.0 <= c <= 120.0:
                out["temp"] = str(int(c)) if c.is_integer() else f"{c:.1f}"
        except (TypeError, ValueError):
            pass
    po = obj.get("power_on_time")
    if isinstance(po, dict) and po.get("hours") is not None:
        try:
            out["power_on_hours"] = str(int(float(po.get("hours"))))
        except (TypeError, ValueError):
            pass
    st = obj.get("smart_status")
    if isinstance(st, dict) and isinstance(st.get("passed"), bool):
        out["passed"] = st["passed"]
    return out


def _recv_matching(ws: Any, reqid: str, timeout: float, max_extra: int = 5) -> Dict[str, Any]:
    """收帧直到拿到本请求的响应；跳过服务端的主动推送帧。"""
    deadline = time.monotonic() + max(timeout, 0.5)
    for _ in range(max_extra + 1):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        ws.settimeout(left)
        frame = ws.recv()
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8", "replace")
        resp = json.loads(frame)
        if not isinstance(resp, dict):
            continue
        rid = resp.get("reqid")
        if rid is None or str(rid) == reqid:
            return resp
    raise FnosWsError("等待接口响应超时")


def _call(
    ws: Any,
    secret: str,
    prefix: str,
    back_id: str,
    seq: int,
    req: str,
    params: Optional[Dict[str, Any]],
    timeout: float,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"reqid": make_reqid(prefix, back_id, seq), "req": req}
    if params:
        body.update(params)
    if secret:
        payload = sign_payload(secret, body)
    else:
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    ws.send(payload)
    resp = _recv_matching(ws, str(body["reqid"]), timeout)
    result = resp.get("result")
    if result is not None and result != "succ":
        msg = resp.get("errmsg") or resp.get("errorMsg") or result
        raise FnosWsError(f"{req} 返回错误：{msg}（errno={resp.get('errno')}）")
    return resp


def fetch_disk_overview(
    base_url: str,
    user: str,
    password: str,
    timeout: float = 6.0,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """连接 fnOS 拉取全部物理盘的温度与健康总览。

    返回 ({设备名: {"temp": "41", "passed": True/False/None, "standby": bool,
    "power_on_hours": "12345"}}, "")；失败返回 ({}, 错误说明)。

    温度取 resmon.disk（一次覆盖全部盘），健康取 stor.diskSmart 的
    smart_status。休眠盘只记 standby 标记——调用方应避免再对其做任何
    会唤醒硬盘的探测（smartctl 查询会唤醒休眠盘）。
    """
    if not str(user or "").strip() or not str(password or ""):
        return {}, "未配置飞牛监控接口的账号或密码"
    try:
        url = build_ws_url(base_url)
    except FnosWsError as e:
        return {}, str(e)
    try:
        import websocket  # 懒加载：URL/签名/解析等纯函数在无依赖环境也可测试
    except ImportError:
        return {}, "缺少依赖 websocket-client，请更新安装包或手动 pip install"
    try:
        ws = websocket.create_connection(
            url,
            timeout=timeout,
            # fnOS 内网 wss 普遍是自签证书，这里不校验；本接口只读监控数据
            sslopt={"cert_reqs": 0} if url.startswith("wss") else None,
        )
    except Exception as e:
        return {}, f"连接失败（{url}）：{e}"

    prefix = secrets.token_hex(8)
    overview: Dict[str, Dict[str, Any]] = {}
    try:
        login = {
            "reqid": make_reqid(prefix, "", 1),
            "req": "user.login",
            "user": str(user).strip(),
            "password": str(password),
            "stay": 1,
            "deviceName": "FnMessageBot",
            "deviceType": "API",
            "did": _LOGIN_DID,
        }
        ws.send(json.dumps(login, separators=(",", ":"), ensure_ascii=False))
        resp = _recv_matching(ws, str(login["reqid"]), timeout)
        result = resp.get("result")
        if result is not None and result != "succ":
            msg = resp.get("errmsg") or resp.get("errorMsg") or result
            return {}, f"飞牛监控接口登录失败：{msg}（errno={resp.get('errno')}）"
        secret = str(resp.get("secret") or "")
        back_id = str(resp.get("backId") or "")
        if not secret:
            return {}, "飞牛监控接口登录响应缺少 secret"

        disk_resp = _call(ws, secret, prefix, back_id, 2, "appcgi.resmon.disk", None, timeout)
        temps = parse_resmon_disk(disk_resp)
        seq = 2
        for name in sorted(temps):
            seq += 1
            info: Dict[str, Any] = {
                "temp": temps[name],
                "passed": None,
                "standby": False,
                "power_on_hours": "",
            }
            try:
                sm = _call(ws, secret, prefix, back_id, seq, "stor.diskSmart", {"disk": name}, timeout)
            except FnosWsError:
                overview[name] = info  # 单盘 SMART 失败不否定整轮结果（温度已拿到）
                continue
            parsed = parse_disk_smart(sm)
            info["standby"] = bool(parsed["standby"])
            info["passed"] = parsed["passed"]
            info["power_on_hours"] = parsed["power_on_hours"]
            if parsed["temp"]:
                info["temp"] = parsed["temp"]
            overview[name] = info
        return overview, ""
    except FnosWsError as e:
        return overview, str(e)  # 已拿到的部分仍然有效
    except Exception as e:
        return overview, f"飞牛监控接口调用异常：{e}"
    finally:
        try:
            ws.close()
        except Exception:
            pass
