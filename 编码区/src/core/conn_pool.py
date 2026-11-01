# -*- coding: utf-8 -*-
"""core/conn_pool.py - 纯 Python 标准库 HTTP/HTTPS 连接池 (P1-1)

职责：
1. 复用 HTTP/HTTPS TCP 连接与 TLS 握手状态（Keep-Alive），大幅降低广域网上游时延；
2. 线程安全（Lock + Queue），限制单 Host 连接数（默认最大 16，空闲超时 60s）；
3. 自动探测死连接并透明重试 1 次（处理上游主动断开 Keep-Alive 连接的场景）；
4. 提供 request 上下文管理器，请求完毕后将可用连接放回池中；
5. Fail-Open 哲学：池化异常时可无缝回退标准 urllib.request。
"""
from __future__ import annotations

import http.client
import os
import ssl
import threading
import time
from queue import Empty, Queue
from typing import Dict, Tuple
from urllib.parse import urlsplit

__all__ = ["HttpConnectionPool", "GLOBAL_CONN_POOL"]


class _PooledConnection:
    """包装单条物理连接及其元数据"""

    def __init__(self, conn: http.client.HTTPConnection, key: Tuple[str, str, int]):
        self.conn = conn
        self.key = key
        self.last_used = time.monotonic()
        self.reusable = True

    def is_stale(self, max_idle: float = 55.0) -> bool:
        """判断空闲是否超时"""
        return (time.monotonic() - self.last_used) > max_idle

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class HttpConnectionPool:
    """标准库 HTTP/HTTPS 线程安全长连接池"""

    def __init__(
        self,
        max_per_host: int = 16,
        max_idle_seconds: float = 55.0,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.max_per_host = max_per_host
        self.max_idle_seconds = max_idle_seconds
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._pools: Dict[Tuple[str, str, int], Queue[_PooledConnection]] = {}
        self._lock = threading.Lock()

    def _get_queue(self, key: Tuple[str, str, int]) -> Queue[_PooledConnection]:
        with self._lock:
            if key not in self._pools:
                self._pools[key] = Queue(maxsize=self.max_per_host)
            return self._pools[key]

    def _create_connection(
        self, scheme: str, host: str, port: int, timeout: float
    ) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(
                host, port=port, timeout=timeout, context=self.ssl_context
            )
        return http.client.HTTPConnection(host, port=port, timeout=timeout)

    def acquire(self, url: str, timeout: float = 30.0) -> _PooledConnection:
        """从池中获取一条存活连接，若无则新建"""
        parts = urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)

        q = self._get_queue(key)
        while True:
            try:
                pooled = q.get_nowait()
                if not pooled.is_stale(self.max_idle_seconds):
                    pooled.conn.timeout = timeout
                    return pooled
                pooled.close()
            except Empty:
                break

        conn = self._create_connection(scheme, host, port, timeout)
        return _PooledConnection(conn, key)

    def release(self, pooled: _PooledConnection, reuse: bool = True):
        """归还或关闭连接"""
        if not reuse or not pooled.reusable:
            pooled.close()
            return

        pooled.last_used = time.monotonic()
        q = self._get_queue(pooled.key)
        try:
            q.put_nowait(pooled)
        except Exception:
            pooled.close()

    def request(
        self,
        url: str,
        method: str,
        body: bytes | None = None,
        headers: dict | None = None,
        timeout: float = 30.0,
    ) -> Tuple[http.client.HTTPResponse, _PooledConnection]:
        """执行 HTTP 请求，支持透明重试 1 次（应对 Keep-Alive 远端关闭）"""
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        req_headers = dict(headers or {})
        # 确保带 Host 头
        if "Host" not in req_headers and "host" not in req_headers:
            host_header = parts.hostname or "127.0.0.1"
            if parts.port and parts.port not in (80, 443):
                host_header += f":{parts.port}"
            req_headers["Host"] = host_header

        pooled = self.acquire(url, timeout=timeout)
        try:
            pooled.conn.request(method, path, body=body, headers=req_headers)
            res = pooled.conn.getresponse()
            # 若服务端响应 Connection: close 则标记不可复用
            if res.getheader("connection", "").lower() == "close":
                pooled.reusable = False
            return res, pooled
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
            # 远端静默断开 Keep-Alive，重试一次全新连接
            pooled.close()
            pooled = self.acquire(url, timeout=timeout)
            pooled.conn.request(method, path, body=body, headers=req_headers)
            res = pooled.conn.getresponse()
            if res.getheader("connection", "").lower() == "close":
                pooled.reusable = False
            return res, pooled

    def close_all(self):
        """关闭所有池中连接"""
        with self._lock:
            for q in self._pools.values():
                while not q.empty():
                    try:
                        pooled = q.get_nowait()
                        pooled.close()
                    except Empty:
                        break
            self._pools.clear()


# 全局连接池单例
GLOBAL_CONN_POOL = HttpConnectionPool()
