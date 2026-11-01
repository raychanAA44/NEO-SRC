# -*- coding: utf-8 -*-
"""core/rate_limiter.py - 纯标准库轻量令牌桶限流中间件

职责：
1. 实现防爆破/防刷限流闸门（默认 ≤100 req/s，突发容量 150）；
2. 线程安全（threading.Lock），按 IP 维护令牌桶；
3. 支持惰性过期清理（自动剔除 120s 无活动 IP，防止内存泄漏）；
4. 静态资源（/assets/*、.css、.js 等）与 /health 探针端点自动白名单豁免；
5. 超限返回标准 HTTP 429 与 Retry-After 头；
6. 支持 HUB_RATE_LIMIT_ENABLED、HUB_RATE_LIMIT_RPS、HUB_RATE_LIMIT_BURST 环境变量控制。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, Tuple

__all__ = ["TokenBucket", "RateLimiter"]


class TokenBucket:
    """单个令牌桶实现（线程安全）"""

    def __init__(self, rate: float, capacity: float):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> Tuple[bool, float]:
        """尝试获取 tokens。

        返回 (allowed: bool, retry_after: float)。
        若允许，retry_after 为 0.0；若拒绝，retry_after 为恢复所需秒数。
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_updated
            self.last_updated = now

            # 补充令牌
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True, 0.0

            needed = tokens - self.tokens
            retry_after = needed / self.rate if self.rate > 0 else 1.0
            return False, max(0.1, round(retry_after, 2))


class RateLimiter:
    """按客户端 IP 分桶的全局限流器"""

    def __init__(
        self,
        rate: float | None = None,
        burst: float | None = None,
        enabled: bool | None = None,
    ):
        env_enabled = os.environ.get("HUB_RATE_LIMIT_ENABLED", "1").strip().lower()
        self.enabled = (
            enabled if enabled is not None else env_enabled not in ("0", "false", "off", "no")
        )
        self.rate = float(
            rate if rate is not None else (os.environ.get("HUB_RATE_LIMIT_RPS") or 100.0)
        )
        self.burst = float(
            burst if burst is not None else (os.environ.get("HUB_RATE_LIMIT_BURST") or 150.0)
        )
        self._buckets: Dict[str, Tuple[TokenBucket, float]] = {}  # ip -> (bucket, last_access)
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def is_exempt(self, path: str, headers: dict | None = None) -> bool:
        """检查路径或请求头是否豁免限流"""
        clean_path = (path or "").split("?")[0].rstrip("/")
        if clean_path in ("", "/health"):
            return True
        if clean_path.startswith("/assets/"):
            return True
        for ext in (".css", ".js", ".png", ".jpg", ".jpeg", ".ico", ".svg", ".woff2"):
            if clean_path.endswith(ext):
                return True
        if headers:
            # 内部循环调用或探针豁免
            if headers.get("internal_refiner") == "1":
                return True
            if headers.get("x-hub-probe") == "1":
                return True
        return False

    def check(self, ip: str, path: str, headers: dict | None = None) -> Tuple[bool, float]:
        """检查指定 IP 对路径的请求是否放行。

        返回 (allowed: bool, retry_after: float)。
        """
        if not self.enabled:
            return True, 0.0

        if self.is_exempt(path, headers):
            return True, 0.0

        client_ip = (ip or "127.0.0.1").strip()
        now = time.monotonic()

        with self._lock:
            # 每 60 秒清理一次空闲超过 120 秒的 IP
            if now - self._last_cleanup > 60:
                self._cleanup_idle_buckets(now)
                self._last_cleanup = now

            if client_ip not in self._buckets:
                bucket = TokenBucket(rate=self.rate, capacity=self.burst)
                self._buckets[client_ip] = (bucket, now)
            else:
                bucket, _ = self._buckets[client_ip]
                self._buckets[client_ip] = (bucket, now)

        return bucket.acquire(1.0)

    def _cleanup_idle_buckets(self, now: float):
        """清理长时间无活动的水桶（需在持锁下调用）"""
        idle_threshold = 120.0
        stale_ips = [
            ip for ip, (_, last_seen) in self._buckets.items()
            if now - last_seen > idle_threshold
        ]
        for ip in stale_ips:
            del self._buckets[ip]

    def reset(self):
        """重置所有水桶状态（测试用）"""
        with self._lock:
            self._buckets.clear()

    def set_rate(self, rate: float, burst: float | None = None) -> dict:
        """在线调速：改速率并清空旧桶，避免旧桶按旧容量继续放行。"""
        new_rate = float(rate)
        if new_rate <= 0:
            raise ValueError("限流速率必须大于 0")
        new_burst = float(burst) if burst is not None else max(new_rate, self.burst)
        with self._lock:
            self.rate = new_rate
            self.burst = new_burst
            self._buckets.clear()
        return {"rate": self.rate, "burst": self.burst}
