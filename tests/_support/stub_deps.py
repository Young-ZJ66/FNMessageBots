"""测试用外部依赖桩（requests / urllib3 / docker / psutil / flask / croniter）。

目的：让 `src` 下的真实模块能在没有第三方依赖、没有网络的环境里被导入并做单元测试。
**不模拟真实 HTTP 行为**：`requests.Session.request` 会把请求交给 `FAKE_HANDLER`
（测试自行设置），默认返回 200 空体。
"""

from __future__ import annotations

import functools
import sys
import types
from typing import Any, Callable, Dict, List, Optional

_STUB_MODULE_NAMES = (
    "requests",
    "urllib3",
    "docker",
    "psutil",
    "flask",
    "croniter",
)


class StubResponse:
    """最小可用的 requests.Response 替身。"""

    def __init__(self, status_code: int = 200, text: str = "", payload: Any = None):
        self.status_code = status_code
        self.text = text
        self.content = (text or "").encode("utf-8")
        self.headers: Dict[str, str] = {}
        self.reason = "OK" if status_code < 400 else "Error"
        self._payload = payload

    def json(self) -> Any:
        """与真实 requests 一致：优先显式 payload，否则按 JSON 解析文本。"""
        if self._payload is not None:
            return self._payload
        if not (self.text or "").strip():
            raise ValueError("Expecting value: empty body")
        import json as _json

        return _json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _HTTPError(f"{self.status_code} Error", response=self)


class _RequestException(Exception):
    def __init__(self, *args: Any, response: Any = None, request: Any = None):
        super().__init__(*args)
        self.response = response
        self.request = request


class _HTTPError(_RequestException):
    pass


class _ConnectionError(_RequestException):
    pass


class _Timeout(_RequestException):
    pass


class _TooManyRedirects(_RequestException):
    pass


class FakeRetry:
    def __init__(self, *args: Any, **kwargs: Any):
        self.kwargs = kwargs


class FakeHTTPAdapter:
    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs

    def mount(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeSession:
    """把 request/post/get 交给模块级 FAKE_HANDLER，便于断言实际发出的请求。"""

    def __init__(self):
        self.headers: Dict[str, str] = {}
        self.verify = True
        self.trust_env = True
        self.adapters: Dict[str, Any] = {}

    def mount(self, prefix: str, adapter: Any) -> None:
        self.adapters[prefix] = adapter

    def close(self) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        REQUEST_LOG.append({"method": method, "url": url, "kwargs": kwargs})
        return FAKE_HANDLER(method, url, kwargs)

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        REQUEST_LOG.append({"method": "GET", "url": url, "kwargs": kwargs})
        return FAKE_HANDLER("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> StubResponse:
        REQUEST_LOG.append({"method": "POST", "url": url, "kwargs": kwargs})
        return FAKE_HANDLER("POST", url, kwargs)


def _default_handler(method: str, url: str, kwargs: Dict[str, Any]) -> StubResponse:
    return StubResponse(200, text='{"errcode":0,"errmsg":"ok"}')


FAKE_HANDLER: Callable[[str, str, Dict[str, Any]], Any] = _default_handler
REQUEST_LOG: List[Dict[str, Any]] = []


def set_fake_handler(handler: Optional[Callable[[str, str, Dict[str, Any]], Any]] = None) -> None:
    global FAKE_HANDLER
    FAKE_HANDLER = handler or _default_handler


def reset_fake_handler() -> None:
    global FAKE_HANDLER
    FAKE_HANDLER = _default_handler
    REQUEST_LOG.clear()


def _make_stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__stub__ = True  # 标记，避免 __getattr__ 递归
    return mod


def install() -> None:
    """在导入 src 模块之前调用；已存在真实依赖时不覆盖。"""
    if getattr(sys.modules.get("requests"), "__stub__", False):
        return

    requests_mod = _make_stub("requests")
    requests_mod.Response = StubResponse
    requests_mod.Session = FakeSession
    requests_mod.Session  # noqa: B018  (保持显式)
    requests_mod.request = lambda method, url, **kw: FAKE_HANDLER(method, url, kw)
    requests_mod.get = lambda url, **kw: FAKE_HANDLER("GET", url, kw)
    requests_mod.post = lambda url, **kw: FAKE_HANDLER("POST", url, kw)
    exceptions = _make_stub("requests.exceptions")
    exceptions.RequestException = _RequestException
    exceptions.HTTPError = _HTTPError
    exceptions.ConnectionError = _ConnectionError
    exceptions.Timeout = _Timeout
    exceptions.TooManyRedirects = _TooManyRedirects
    requests_mod.exceptions = exceptions
    adapters = _make_stub("requests.adapters")
    adapters.HTTPAdapter = FakeHTTPAdapter
    requests_mod.adapters = adapters
    sessions = _make_stub("requests.sessions")
    sessions.Session = FakeSession
    requests_mod.sessions = sessions
    utils_mod = _make_stub("requests.utils")
    utils_mod.quote = lambda s, *a, **k: str(s)
    requests_mod.utils = utils_mod
    sys.modules["requests"] = requests_mod
    sys.modules["requests.exceptions"] = exceptions
    sys.modules["requests.adapters"] = adapters
    sys.modules["requests.sessions"] = sessions
    sys.modules["requests.utils"] = utils_mod

    urllib3_mod = _make_stub("urllib3")
    urllib3_util = _make_stub("urllib3.util")
    urllib3_retry = _make_stub("urllib3.util.retry")
    urllib3_retry.Retry = FakeRetry
    urllib3_util.retry = urllib3_retry
    urllib3_mod.util = urllib3_util
    urllib3_mod.__version__ = "0.0.0-stub"
    sys.modules["urllib3"] = urllib3_mod
    sys.modules["urllib3.util"] = urllib3_util
    sys.modules["urllib3.util.retry"] = urllib3_retry

    for name in ("docker", "psutil", "flask", "croniter"):
        if name in sys.modules:
            continue
        stub = _make_stub(name)
        if name == "flask":
            # PEP 562：web.ui_app 用 `from flask import Flask, ...` 导入，
            # 空桩会导致 ImportError；未知属性返回可调用桩即可满足导入期需求。
            stub.__getattr__ = _autostub_getattr
        # docker / psutil / croniter 保持空桩：
        # 相关模块以 try-import 区分「依赖缺失」，导入成功但属性缺失时走各自的降级路径。
        sys.modules[name] = stub


def _autostub_getattr(name: str) -> Callable[..., Any]:
    """未知属性一律返回可调用桩（同时可作为类型构造器使用）。"""

    def _factory(*args: Any, **kwargs: Any) -> types.SimpleNamespace:
        return types.SimpleNamespace(*args, **kwargs)

    _factory.__name__ = name
    return _factory
