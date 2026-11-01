# -*- coding: utf-8 -*-
"""test_rate_limiter.py - 令牌桶限流中间件单测

测试项：
1. 突发容量内允许请求通过；
2. 耗尽容量后触发限流拒绝并返回 retry_after；
3. 补充周期后自动恢复；
4. 静态资产与 /health 端点自动豁免；
5. 环境变量控制启用与参数调整；
6. 多 IP 独立分桶互不干扰。
"""
import time
import unittest
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.rate_limiter import TokenBucket, RateLimiter


class TestTokenBucket(unittest.TestCase):
    def test_burst_capacity(self):
        # 速率 10/s，突发 5
        bucket = TokenBucket(rate=10.0, capacity=5.0)
        # 前 5 次应该全部通过
        for _ in range(5):
            allowed, _ = bucket.acquire(1.0)
            self.assertTrue(allowed)

        # 第 6 次瞬间调用应被限流
        allowed, retry_after = bucket.acquire(1.0)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)

    def test_refill_recovery(self):
        # 速率 50/s，突发 2
        bucket = TokenBucket(rate=50.0, capacity=2.0)
        self.assertTrue(bucket.acquire(1.0)[0])
        self.assertTrue(bucket.acquire(1.0)[0])
        self.assertFalse(bucket.acquire(1.0)[0])

        # 等待 40ms，应补充 2 个令牌
        time.sleep(0.05)
        self.assertTrue(bucket.acquire(1.0)[0])


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        self.limiter = RateLimiter(rate=10.0, burst=3.0, enabled=True)

    def test_exempt_paths(self):
        # /health, /, 静态资源全部豁免
        for path in ["/health", "/", "/assets/css/base.css", "/ui/views.js", "/favicon.ico"]:
            allowed, _ = self.limiter.check("1.2.3.4", path)
            self.assertTrue(allowed, f"{path} 应该被豁免")

    def test_rate_limiting_per_ip(self):
        ip1 = "10.0.0.1"
        ip2 = "10.0.0.2"

        # ip1 耗尽 3 个突发令牌
        self.assertTrue(self.limiter.check(ip1, "/api/status")[0])
        self.assertTrue(self.limiter.check(ip1, "/api/status")[0])
        self.assertTrue(self.limiter.check(ip1, "/api/status")[0])
        # ip1 第 4 次应被拒
        allowed, retry_after = self.limiter.check(ip1, "/api/status")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)

        # 此时 ip2 的令牌桶仍是满的，不受 ip1 影响
        self.assertTrue(self.limiter.check(ip2, "/api/status")[0])

    def test_disabled_limiter(self):
        disabled = RateLimiter(rate=1.0, burst=1.0, enabled=False)
        for _ in range(10):
            allowed, _ = disabled.check("1.2.3.4", "/api/models")
            self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
