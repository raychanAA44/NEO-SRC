# -*- coding: utf-8 -*-
"""core/server_runtime.py - 网关运行时监听器与单实例保护 (P2-1 模块化拆分)

职责：
1. 实现 Windows 平台独占端口绑定（SO_EXCLUSIVEADDRUSE），杜绝双实例静默共存；
2. Unix 平台兼容 SO_REUSEADDR 快速回收；
3. 单实例服务创建与生命周期管理（run / shutdown）。
"""
from __future__ import annotations

import errno
import os
import signal
import socket
import sys
from http.server import ThreadingHTTPServer

__all__ = [
    "GatewayAlreadyRunningError",
    "SingleInstanceHTTPServer",
    "create_server",
    "run_server",
]


class GatewayAlreadyRunningError(RuntimeError):
    """端口已被另一个实例占用。"""


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    """拒绝多进程共用端口的 HTTP 服务

    Windows 下 SO_REUSEADDR 允许两进程绑定同一端口；
    设置 SO_EXCLUSIVEADDRUSE 后，第二个实例在 bind 阶段直接失败。
    Unix 下 SO_REUSEADDR 仅支持 TIME_WAIT 快速回收，
    内核会正常拒绝 EADDRINUSE。
    """

    allow_reuse_address = os.name != "nt"
    # accept 队列深度。stdlib 默认只有 5：满载（12 核跑 10 个全量套件）时 accept 线程
    # 会被饿住，队列一满 Windows 就拒绝/中止新连接——2026-10-27 发版门禁两次在满载
    # 轮次熔断就是这个原因，实测 backlog=5 在压满时掉 9 条连接，128 时为 0。
    request_queue_size = 128

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(port: int, handler_class) -> SingleInstanceHTTPServer:
    """创建单实例服务器，被占用时抛出 GatewayAlreadyRunningError"""
    try:
        return SingleInstanceHTTPServer(("0.0.0.0", port), handler_class)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
            raise GatewayAlreadyRunningError(
                f"端口 {port} 已被占用；已有 HUB NEO 实例正在运行"
            ) from exc
        raise


def run_server(port: int, handler_class, root_dir=None, on_ready=None):
    """运行服务器主循环，处理优雅退出信号"""
    httpd = create_server(port, handler_class)
    print(f"[HUB NEO] 单实例启动成功: http://127.0.0.1:{port}", flush=True)

    def _shutdown(signum=None, frame=None):
        print(f"\n[HUB NEO] 收到停止信号 ({signum})，正在关闭...", flush=True)
        threading_stopper = threading.Thread(target=httpd.shutdown, daemon=True)
        threading_stopper.start()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, AttributeError):
        pass

    if on_ready:
        on_ready()

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        print("[HUB NEO] 服务已安全退出", flush=True)
