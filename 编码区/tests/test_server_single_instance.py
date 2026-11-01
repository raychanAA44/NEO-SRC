#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_server_single_instance.py - HUB NEO 网关单实例启动保护回归。

覆盖：
- 已监听端口上的第二个 SingleInstanceHTTPServer 必须绑定失败；
- 旧式 SO_REUSEADDR 监听器存在时，新实例也不得抢占同一端口；
- 服务关闭后可立即在同一端口重启；
- 端口被占用时启动脚本以非零退出码中止，并输出明确原因。
"""

import errno
import os
import socket
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(SRC_DIR))

import server


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        self.blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.blocker.bind(("127.0.0.1", 0))
        self.blocker.listen(1)
        self.port = self.blocker.getsockname()[1]
        self.addCleanup(self.blocker.close)

    def test_second_bind_on_live_port_is_rejected(self):
        first = server.SingleInstanceHTTPServer(
            ("127.0.0.1", 0), server.HubNeoHandler)
        self.addCleanup(first.server_close)
        threading.Thread(
            target=first.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()
        self.addCleanup(first.shutdown)
        port = first.server_address[1]

        with self.assertRaises(OSError) as ctx:
            server.SingleInstanceHTTPServer(
                ("127.0.0.1", port), server.HubNeoHandler)

        self.assertEqual(ctx.exception.errno, errno.EADDRINUSE)

    def test_exclusive_bind_rejects_legacy_reuse_listener(self):
        legacy = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.HubNeoHandler)
        self.addCleanup(legacy.server_close)
        port = legacy.server_address[1]

        with self.assertRaises(OSError) as ctx:
            server.SingleInstanceHTTPServer(
                ("127.0.0.1", port), server.HubNeoHandler)

        self.assertEqual(ctx.exception.errno, errno.EADDRINUSE)

    def test_legacy_reuse_listener_cannot_join_exclusive_port(self):
        first = server.SingleInstanceHTTPServer(
            ("127.0.0.1", 0), server.HubNeoHandler)
        self.addCleanup(first.server_close)
        port = first.server_address[1]

        with self.assertRaises(OSError) as ctx:
            ThreadingHTTPServer(("127.0.0.1", port), server.HubNeoHandler)

        self.assertIn(ctx.exception.errno, (errno.EADDRINUSE, errno.EACCES))

    def test_port_can_be_rebound_after_close(self):
        first = server.SingleInstanceHTTPServer(
            ("127.0.0.1", 0), server.HubNeoHandler)
        port = first.server_address[1]
        first.server_close()

        second = server.SingleInstanceHTTPServer(
            ("127.0.0.1", port), server.HubNeoHandler)
        self.addCleanup(second.server_close)
        self.assertEqual(second.server_address[1], port)

    def test_create_server_reports_occupied_port(self):
        with self.assertRaises(server.GatewayAlreadyRunningError) as ctx:
            server._create_server(self.port)

        message = str(ctx.exception)
        self.assertIn(str(self.port), message)
        self.assertIn("已被占用", message)

    def test_start_script_exits_nonzero_when_port_busy(self):
        env = dict(os.environ)
        env["HUB_NEO_PORT"] = str(self.port)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("HUB_ADMIN_TOKEN", None)

        proc = subprocess.run(
            [sys.executable, str(ROOT_DIR / "运维区" / "scripts" / "start_gateway.py")],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        output = (proc.stdout or "") + (proc.stderr or "")

        self.assertNotEqual(proc.returncode, 0, output)
        self.assertIn("已被占用", output)
        self.assertIn(str(self.port), output)


if __name__ == "__main__":
    unittest.main()
