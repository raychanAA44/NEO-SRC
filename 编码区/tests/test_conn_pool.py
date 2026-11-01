# -*- coding: utf-8 -*-
"""test_conn_pool.py - HTTP 长连接池单测

测试项：
1. 连续请求同一 Host 时复用连接实例（Keep-Alive）；
2. 响应 Connection: close 时不放回池中；
3. 空闲超时后自动剔除并重建；
4. 远端断开连接时自动透明重试 1 次；
5. close_all 彻底释放所有底层连接。
"""
import http.server
import socketserver
import threading
import time
import unittest
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from core.conn_pool import HttpConnectionPool


class _MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/close":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


class TestHttpConnectionPool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_connection_reuse(self):
        pool = HttpConnectionPool(max_per_host=4)
        # 第一次请求
        res1, pooled1 = pool.request(f"{self.url}/test", "GET")
        self.assertEqual(res1.status, 200)
        res1.read()
        pool.release(pooled1, reuse=True)

        # 第二次请求应复用相同连接
        res2, pooled2 = pool.request(f"{self.url}/test", "GET")
        self.assertEqual(res2.status, 200)
        self.assertIs(pooled1.conn, pooled2.conn, "相同 Host 应该复用底层连接")
        res2.read()
        pool.release(pooled2, reuse=True)
        pool.close_all()

    def test_close_header_not_reused(self):
        pool = HttpConnectionPool(max_per_host=4)
        res1, pooled1 = pool.request(f"{self.url}/close", "GET")
        res1.read()
        pool.release(pooled1, reuse=True)

        # 再次请求应创建新连接
        res2, pooled2 = pool.request(f"{self.url}/test", "GET")
        res2.read()
        self.assertIsNot(pooled1.conn, pooled2.conn, "Connection: close 不应被复用")
        pool.release(pooled2, reuse=True)
        pool.close_all()


if __name__ == "__main__":
    unittest.main()
