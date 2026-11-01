"""出站代理：把 control.json 里的 proxy_url 应用到网关自己的上游请求。

urllib 的默认 opener 只在首次使用时读一次环境代理，之后改 env 不生效；
所以这里按 proxy_url 缓存一个带 ProxyHandler 的 opener，改配置即换 opener。
空串 = 直连（与改造前完全一致）。
"""
from __future__ import annotations

import threading
import urllib.request

_lock = threading.Lock()
_cache = {"url": None, "opener": None}


def opener_for(proxy_url: str = ""):
    """返回该代理对应的 opener；空串返回 None（调用方走 urllib 默认路径）。"""
    url = (proxy_url or "").strip()
    if not url:
        return None
    with _lock:
        if _cache["url"] != url:
            handler = urllib.request.ProxyHandler({"http": url, "https": url})
            _cache["opener"] = urllib.request.build_opener(handler)
            _cache["url"] = url
        return _cache["opener"]


def open_url(req, timeout, proxy_url: str = ""):
    """带代理调用；proxy_url 为空时行为与 urllib.request.urlopen 一致。"""
    opener = opener_for(proxy_url)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


__all__ = ["open_url", "opener_for"]
