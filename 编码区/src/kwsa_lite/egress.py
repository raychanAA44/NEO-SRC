# -*- coding: utf-8 -*-
"""出口分路（代理腿）。

一张表描述「请求从哪个出口出去」：

- ``direct``：不走代理，与不带 egress 时的行为完全一致；
- 具名腿：``http://user:pass@host:port`` 或 ``socks5://host:port``（后者需要环境里有
  socks 支持，标准库不自带，配置校验阶段会拒绝，避免运行期静默回落成直连）。

urllib 的默认 opener 只在首次使用时读一次环境代理，之后改环境变量不生效；这里按腿
缓存 opener，改配置即换 opener。腿被停用或删除时立刻回落到直连，不会拿旧代理继续跑。
"""

from __future__ import annotations

import threading
import urllib.request

DIRECT = ""


class EgressError(ValueError):
    """出口腿配置不合法。"""


def normalize(url: str) -> str:
    return str(url or "").strip()


def validate(url: str) -> str:
    """只接受 http(s) 代理；空串代表直连。"""
    value = normalize(url)
    if not value:
        return DIRECT
    if not value.startswith(("http://", "https://")):
        raise EgressError("代理腿只接受 http:// 或 https:// 地址（标准库不带 socks 支持）")
    return value


class Egress:
    """腿 id -> 代理地址；线程安全，按 URL 缓存 opener。"""

    def __init__(self, legs=None, default: str = DIRECT):
        self._lock = threading.Lock()
        self._cache = {}
        self.legs = {}
        self.default = DIRECT
        self.reload(legs, default)

    def reload(self, legs=None, default: str = DIRECT) -> None:
        table = {}
        for leg in (legs or []):
            if not isinstance(leg, dict):
                continue
            leg_id = str(leg.get("id") or "").strip()
            if not leg_id or leg_id == "direct":
                continue
            try:
                url = validate(leg.get("url"))
            except EgressError:
                continue
            if not url:
                continue
            table[leg_id] = {
                "id": leg_id,
                "name": str(leg.get("name") or leg_id),
                "url": url,
                "enabled": bool(leg.get("enabled", True)),
                "note": str(leg.get("note") or ""),
            }
        with self._lock:
            self.legs = table
            self.default = str(default or "").strip() if str(default or "").strip() in table else DIRECT
            self._cache = {}

    def resolve(self, leg_id: str = "") -> str:
        """渠道指定的腿优先；它不可用时按全局默认腿；都没有则直连。"""
        wanted = str(leg_id or "").strip()
        with self._lock:
            if wanted in self.legs and self.legs[wanted]["enabled"]:
                return self.legs[wanted]["url"]
            if wanted:
                return DIRECT
            if self.default and self.legs.get(self.default, {}).get("enabled"):
                return self.legs[self.default]["url"]
            return DIRECT

    def _opener(self, url: str):
        if not url:
            return None
        with self._lock:
            cached = self._cache.get(url)
            if cached is None:
                handler = urllib.request.ProxyHandler({"http": url, "https": url})
                cached = urllib.request.build_opener(handler)
                self._cache[url] = cached
            return cached

    def open(self, req, timeout: float, leg_id: str = ""):
        """按腿发起请求；直连时与 ``urllib.request.urlopen`` 等价。"""
        opener = self._opener(self.resolve(leg_id))
        if opener is None:
            return urllib.request.urlopen(req, timeout=timeout)
        return opener.open(req, timeout=timeout)

    def public(self) -> dict:
        with self._lock:
            return {
                "default": self.default,
                "legs": [dict(row) for row in self.legs.values()],
            }


__all__ = ["DIRECT", "Egress", "EgressError", "normalize", "validate"]
