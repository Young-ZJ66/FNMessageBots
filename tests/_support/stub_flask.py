"""可用的假 Flask：让 `web/ui_app.py` 的 `create_app()` 能在无 flask/jinja2 环境下真正跑起来。

为什么需要它：本项目测试套件保持**纯标准库**（不引入 flask/jinja2）。但 Web 层恰恰是
最容易在重构中悄悄坏掉的部分（路由消失、鉴权闸门失效、密钥打码泄漏），没有它就只能靠肉眼。

设计取舍
- **不重新实现 Jinja**：`render_template` 只把「模板名 + 上下文」记进 `RENDER_LOG`，
  返回占位字符串。模板本身用 `tests/_support/tpl_analysis.py` 做静态分析。
  这样「模板引用了某个变量」和「视图是否提供了它」在两侧都能独立验证，
  无需一个半吊子的模板引擎。
- `request` 是模块级代理，指向当前正在派发的请求（测试串行执行，足够）。
- `abort()` 抛异常，由测试客户端转成对应状态码，语义与真实 Flask 一致。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

# 每个元素：{"template": str, "context": dict}
RENDER_LOG: List[Dict[str, Any]] = []


def reset_render_log() -> None:
    RENDER_LOG.clear()


class HTTPException(Exception):
    def __init__(self, code: int, description: str = ""):
        super().__init__(description or f"{code}")
        self.code = code
        self.description = description


class MultiDict:
    """request.args / request.form 的最小替身。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._d: Dict[str, List[str]] = {}
        for k, v in (data or {}).items():
            if v is None:
                continue
            self._d[k] = list(v) if isinstance(v, (list, tuple)) else [str(v)]

    def get(self, key: str, default: Any = None) -> Any:
        vals = self._d.get(key)
        return vals[0] if vals else default

    def getlist(self, key: str) -> List[str]:
        return list(self._d.get(key, []))

    def __getitem__(self, key: str) -> str:
        return self._d[key][0]

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def to_dict(self) -> Dict[str, str]:
        return {k: v[0] for k, v in self._d.items()}


class Request:
    def __init__(
        self,
        method: str = "GET",
        path: str = "/",
        args: Optional[Dict[str, Any]] = None,
        cookies: Optional[Dict[str, str]] = None,
        environ: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        has_json: bool = False,
        remote_addr: str = "127.0.0.1",
        script_root: str = "",
    ):
        self.method = method.upper()
        self.path = path
        self.args = MultiDict(args)
        # MultiDict：会话读取用 getlist 处理「根路径 + 网关前缀」同名多 cookie
        self.cookies = MultiDict({k: v for k, v in (cookies or {}).items()})
        self.environ = dict(environ or {})
        self.remote_addr = remote_addr
        self.script_root = script_root
        self.is_json = has_json
        self._json = json_body

    def get_json(self, silent: bool = False, force: bool = False) -> Any:
        return self._json

    @property
    def query_string(self) -> bytes:
        """原始查询串（bytes），与 Flask 一致。

        测试客户端会把 URL 里的查询解析进 args，这里按其重建即可：
        本桩只用于断言路由与重定向行为，不追求与原始字节序列完全一致
        （参数顺序可能变化），但语义等价。
        """
        return urlencode(self.args.to_dict()).encode("utf-8")


class _RequestProxy:
    """模块级 `request`：转发到当前请求。"""

    def __init__(self) -> None:
        self._current = Request()

    def _bind(self, req: Request) -> None:
        self._current = req

    def __getattr__(self, name: str) -> Any:
        return getattr(self._current, name)


request = _RequestProxy()


class _GProxy:
    """模块级 `g`：请求期间的临时变量命名空间（会话 cookie 刷新标记等）。"""

    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        return self._store.get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._store[name] = value


g = _GProxy()


class Response:
    def __init__(self, body: Any = "", status: int = 200,
                 headers: Optional[Dict[str, str]] = None, mimetype: Optional[str] = None):
        if isinstance(body, bytes):
            self.data = body
        else:
            self.data = str(body).encode("utf-8")
        self.status_code = status
        self.headers: Dict[str, str] = dict(headers or {})
        if mimetype:
            self.headers.setdefault("Content-Type", mimetype)

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", "replace")

    def get_json(self) -> Any:
        return json.loads(self.text)


def jsonify(*args: Any, **kwargs: Any) -> Response:
    payload = args[0] if len(args) == 1 else (list(args) or kwargs)
    return Response(json.dumps(payload, ensure_ascii=False),
                    mimetype="application/json; charset=utf-8")


def redirect(location: str, code: int = 302) -> Response:
    return Response("", status=code, headers={"Location": location})


def abort(code: int, *args: Any, **kwargs: Any) -> None:
    raise HTTPException(int(code))


def send_from_directory(directory: str, path: str, **kwargs: Any) -> Response:
    """按真实 Flask 的语义防目录穿越，不做符号链接跟踪。"""
    base = Path(directory).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(404)
    ctype = "text/plain; charset=utf-8"
    suffix = target.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"):
        ctype = "image/png" if suffix == ".png" else "image/jpeg"
    elif suffix == ".svg":
        ctype = "image/svg+xml"
    elif suffix == ".css":
        ctype = "text/css; charset=utf-8"
    elif suffix == ".js":
        ctype = "text/javascript; charset=utf-8"
    return Response(target.read_bytes(), mimetype=ctype)


def render_template(template_name: str, **context: Any) -> str:
    """记录（模板名，上下文）后返回占位串——模板本身由静态分析验证。"""
    RENDER_LOG.append({"template": template_name, "context": dict(context)})
    return f"[rendered:{template_name}]"


_CONVERTERS = {
    "int": (r"\d+", int),
    "float": (r"[0-9.]+", float),
    "path": (r".+", str),
    "string": (r"[^/]+", str),
    "default": (r"[^/]+", str),
}


def _compile_rule(rule: str) -> Tuple[re.Pattern, List[str]]:
    names: List[str] = []
    pattern = "^"
    for part in re.split(r"(<[^>]+>)", rule):
        if part.startswith("<") and part.endswith(">"):
            inner = part[1:-1]
            if ":" in inner:
                conv, name = inner.split(":", 1)
            else:
                conv, name = "default", inner
            rx, _ = _CONVERTERS.get(conv, _CONVERTERS["default"])
            names.append(name)
            pattern += f"(?P<{name}>{rx})"
        else:
            pattern += re.escape(part)
    return re.compile(pattern + "$"), names


class _View:
    __slots__ = ("rule", "methods", "func", "regex", "names", "endpoint")

    def __init__(self, rule: str, methods: List[str], func: Callable[..., Any]) -> None:
        self.rule = rule
        self.methods = [m.upper() for m in methods]
        self.func = func
        self.regex, self.names = _compile_rule(rule)
        self.endpoint = getattr(func, "__name__", "view")


class TestClient:
    def __init__(self, app: "Flask") -> None:
        self.app = app

    def open(self, path: str, method: str = "GET", json_body: Any = None,
             has_json: bool = False, query: Optional[Dict[str, Any]] = None,
             cookies: Optional[Dict[str, str]] = None,
             environ: Optional[Dict[str, Any]] = None,
             remote_addr: str = "127.0.0.1",
             script_root: str = "") -> Response:
        path_only, _, qs = path.partition("?")
        args: Dict[str, Any] = {}
        if qs:
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    args[k] = v
        args.update(query or {})
        req = Request(method=method, path=path_only, args=args, cookies=cookies,
                      environ=environ, json_body=json_body, has_json=has_json,
                      remote_addr=remote_addr, script_root=script_root)
        request._bind(req)

        try:
            view, kwargs = self.app._match(path_only, req.method)
            # 关键：before_request 必须对**所有**请求先跑（真实 Flask 亦然），
            # 否则鉴权闸门会被绕过，鉴权测试会假通过。
            result = self.app._handle_before_request()
            if result is None:
                if view is None:
                    raise HTTPException(404)
                result = view.func(**kwargs)
            resp = _normalize(result)
        except HTTPException as e:
            resp = Response(f"{e.code}", status=e.code)
        for fn in self.app.after_request_funcs:
            rv = fn(resp)
            if rv is not None:
                resp = rv
        return resp

    def get(self, path: str, **kw: Any) -> Response:
        return self.open(path, method="GET", **kw)

    def post(self, path: str, **kw: Any) -> Response:
        kw.setdefault("has_json", kw.get("json_body") is not None)
        return self.open(path, method="POST", **kw)


def _normalize(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, tuple):
        if not result:
            return Response("")
        # (Response, status) / (body, status) / (body, status, headers)
        head = result[0]
        status = 200
        headers: Optional[Dict[str, str]] = None
        if len(result) >= 2 and isinstance(result[1], int):
            status = result[1]
        if len(result) >= 3 and isinstance(result[2], dict):
            headers = result[2]
        if isinstance(head, Response):
            head.status_code = status
            if headers:
                head.headers.update(headers)
            return head
        return Response(head, status=status, headers=headers)
    if isinstance(result, (str, bytes)):
        return Response(result, mimetype="text/html; charset=utf-8")
    if result is None:
        return Response("", status=204)
    return Response(str(result))


class Flask:
    """够用的 Flask 替身：够 ui_app 跑起来并让测试能派发请求。"""

    def __init__(self, import_name: str, **kwargs: Any) -> None:
        self.import_name = import_name
        self.config: Dict[str, Any] = {}
        self.secret_key: Any = None
        self.template_folder = kwargs.get("template_folder")
        self.static_folder = kwargs.get("static_folder")
        self.views: List[_View] = []
        self.before_request_funcs: List[Callable[..., Any]] = []
        self.after_request_funcs: List[Callable[..., Any]] = []
        self.teardown_request_funcs: List[Callable[..., Any]] = []
        self.url_map = self  # 兼容少量代码读 app.url_map

    # ── 注册 ──
    def _add(self, rule: str, methods: List[str], func: Callable[..., Any]) -> Callable[..., Any]:
        self.views.append(_View(rule, methods, func))
        return func

    def get(self, rule: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda f: self._add(rule, ["GET"], f)

    def post(self, rule: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda f: self._add(rule, ["POST"], f)

    def put(self, rule: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return lambda f: self._add(rule, ["PUT"], f)

    def route(self, rule: str, methods: Optional[List[str]] = None, **kw: Any):
        return lambda f: self._add(rule, methods or ["GET"], f)

    def add_url_rule(self, rule: str, endpoint: str = "", view_func: Optional[Callable[..., Any]] = None,
                     methods: Optional[List[str]] = None, **kw: Any) -> None:
        if view_func is not None:
            self._add(rule, methods or ["GET"], view_func)

    def before_request(self, f: Callable[..., Any]) -> Callable[..., Any]:
        self.before_request_funcs.append(f)
        return f

    def after_request(self, f: Callable[..., Any]) -> Callable[..., Any]:
        self.after_request_funcs.append(f)
        return f

    def teardown_request(self, f: Callable[..., Any]) -> Callable[..., Any]:
        self.teardown_request_funcs.append(f)
        return f

    def register_blueprint(self, bp: Any, **kw: Any) -> None:
        return None

    # ── 派发 ──
    def _match(self, path: str, method: str) -> Tuple[Optional[_View], Dict[str, Any]]:
        path_matched = False
        for v in self.views:
            m = v.regex.match(path)
            if not m:
                continue
            path_matched = True
            if method.upper() not in v.methods:
                continue
            kwargs: Dict[str, Any] = {}
            for name in v.names:
                kwargs[name] = _coerce(v, name, m.group(name))
            return v, kwargs
        if path_matched:
            # 路径存在但方法不对：与真实 Flask 一致返回 405，而不是 404
            raise HTTPException(405)
        return None, {}

    def _handle_before_request(self) -> Any:
        for fn in self.before_request_funcs:
            rv = fn()
            if rv is not None:
                return rv
        return None

    def test_client(self) -> TestClient:
        return TestClient(self)

    # 兼容 `app.run(...)`
    def run(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __call__(self, environ: Any, start_response: Any):  # pragma: no cover
        raise NotImplementedError("假 Flask 不支持 WSGI 直调，请用 test_client()")


def _coerce(view: _View, name: str, raw: str) -> Any:
    """按规则里的转换器把 URL 片段转成对应类型。"""
    rule = view.rule
    m = re.search(r"<([^:>]+):%s>" % re.escape(name), rule)
    conv = m.group(1) if m else "default"
    if conv == "int":
        try:
            return int(raw)
        except ValueError:
            return raw
    if conv == "float":
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def url_for(endpoint: str, **values: Any) -> str:
    return "/" + str(values.get("filename", ""))
