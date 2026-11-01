#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kwsa_lite_tls_proxy.py — KWSA lite 内网 TLS 终止反代（纯标准库）。

拓扑：
    LAN 客户端 --HTTPS 8443--> 本脚本 --HTTP 127.0.0.1:8798--> KWSA lite

只做一件事：让内网只能通过 HTTPS 到达面板与数据面，应用本身继续锁回环。
- 固定上游，不做开放代理；只接受普通 HTTP 请求，拒绝 CONNECT；
- 覆盖客户端传入的 X-Forwarded-*，只写入真实 TCP 对端；
- 保留原始 Host（面板同源校验依赖它），补 X-Forwarded-Proto=https；
- 响应逐块转发，SSE 不缓冲；上游超时/连不上回 504/502。

证书不自动生成：用企业证书或本机自签证书，通过 --cert/--key 指定。
示例：
    python 运维区/scripts/kwsa_lite_tls_proxy.py \
      --listen-host 192.0.2.11 --listen-port 8443 \
      --cert 运维区/certs/kwsa_lite_lan.crt \
      --key 运维区/certs/kwsa_lite_lan.key
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})
STRIP_REQUEST = frozenset({
    "host", "content-length", "x-forwarded-for", "x-real-ip",
    "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port",
})


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 8 * 3600))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KWSA-lite-tls"
    sys_version = ""

    upstream_host = "127.0.0.1"
    upstream_port = 8798
    timeout_s = 180.0
    max_body = 32 * 1024 * 1024

    def log_message(self, fmt, *args):
        pass

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "127.0.0.1"

    def _proxy_log(self, status, started, nbytes, path):
        try:
            sys.stderr.write(json.dumps({
                "timestamp": _iso(),
                "proxy": "kwsa-lite-tls",
                "client_ip": self._client_ip(),
                "method": self.command,
                "path": path,
                "status": int(status or 0),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes": int(nbytes or 0),
            }, ensure_ascii=False) + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

    def _fail(self, status: int, code: str, message: str, started: float, path: str):
        if getattr(self, "_response_started", False):
            self.close_connection = True
            return
        payload = json.dumps({"error": {"code": code, "message": message}},
                             ensure_ascii=False).encode("utf-8")
        try:
            self._response_started = True
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(payload)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except OSError:
            self.close_connection = True
        self._proxy_log(status, started, len(payload) if self.command != "HEAD" else 0, path)

    def _forward(self):
        self._response_started = False
        started = time.perf_counter()
        parsed = urlsplit(self.path)
        path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        if self.headers.get("transfer-encoding"):
            self._fail(411, "length_required",
                       "chunked request bodies are not accepted by this proxy", started, path)
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            self._fail(400, "bad_content_length", "invalid content-length", started, path)
            return
        if length < 0 or length > self.max_body:
            self._fail(413, "payload_too_large",
                       "request body too large (max %d bytes)" % self.max_body, started, path)
            return
        body = self.rfile.read(length) if length else None
        if length and body is not None and len(body) != length:
            self._fail(400, "short_body", "request body ended early", started, path)
            return

        original_host = (self.headers.get("host") or "").strip()
        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_HEADERS or lower in STRIP_REQUEST:
                continue
            headers[key] = value
        if original_host:
            headers["Host"] = original_host
        headers["X-Forwarded-For"] = self._client_ip()
        headers["X-Real-IP"] = self._client_ip()
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = original_host or self.server.server_name
        headers["X-Forwarded-Port"] = str(self.server.server_address[1])
        incoming_rid = (self.headers.get("x-request-id") or "").strip()
        if incoming_rid:
            headers["X-Request-Id"] = incoming_rid

        conn = None
        try:
            conn = http.client.HTTPConnection(
                self.upstream_host, self.upstream_port, timeout=self.timeout_s)
            conn.request(self.command, path, body=body, headers=headers)
            res = conn.getresponse()
            status = int(res.status)
            self._response_started = True
            self.send_response(status, res.reason)
            response_length = res.getheader("content-length")
            for key, value in res.getheaders():
                lower = key.lower()
                if lower in HOP_HEADERS or lower in ("content-length", "server", "date"):
                    continue
                self.send_header(key, value)

            no_body = (self.command == "HEAD" or status in (204, 304)
                       or 100 <= status < 200)
            if no_body:
                if response_length is not None:
                    self.send_header("content-length", response_length)
                self.end_headers()
                sent = 0
            elif response_length is not None:
                self.send_header("content-length", response_length)
                self.end_headers()
                sent = 0
                remaining = int(response_length)
                while remaining > 0:
                    chunk = res.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
                self.wfile.flush()
            else:
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                sent = 0
                while True:
                    chunk = res.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                    sent += len(chunk)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            self._proxy_log(status, started, sent, path)
        except (socket.timeout, TimeoutError):
            self._fail(504, "gateway_timeout", "upstream timed out", started, path)
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            if not getattr(self, "wfile", None):
                return
            self._fail(502, "bad_gateway", "upstream unavailable: %s" % str(exc)[:120],
                       started, path)
        except Exception as exc:  # noqa: BLE001
            self._fail(502, "proxy_error", "proxy error: %s" % str(exc)[:120],
                       started, path)
        finally:
            if conn is not None:
                conn.close()

    def do_GET(self):
        self._forward()

    def do_HEAD(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_PATCH(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_OPTIONS(self):
        self._forward()

    def do_CONNECT(self):
        self._fail(405, "method_not_allowed", "CONNECT is not supported", time.perf_counter(),
                   self.path)


def build_server(args):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(args.cert, args.key)
    Handler.upstream_host = args.upstream_host
    Handler.upstream_port = args.upstream_port
    Handler.timeout_s = args.timeout
    Handler.max_body = args.max_body
    httpd = ThreadingHTTPServer((args.listen_host, args.listen_port), Handler)
    httpd.daemon_threads = True
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(description="KWSA lite LAN TLS reverse proxy")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8443)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8798)
    parser.add_argument("--cert", required=True, help="PEM certificate chain")
    parser.add_argument("--key", required=True, help="PEM private key")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-body", type=int, default=32 * 1024 * 1024)
    args = parser.parse_args(argv)
    for path in (args.cert, args.key):
        if not os.path.isfile(path):
            print("[KWSA lite TLS] missing file: %s" % path, file=sys.stderr)
            return 2
    for port in (args.listen_port, args.upstream_port):
        if port in (8791, 8792, 8795):
            print("[KWSA lite TLS] port %s is forbidden" % port, file=sys.stderr)
            return 2
    try:
        httpd = build_server(args)
    except (OSError, ssl.SSLError) as exc:
        print("[KWSA lite TLS] start failed: %s" % exc, file=sys.stderr)
        return 3
    print("[KWSA lite TLS] listening https://%s:%d -> http://%s:%d"
          % (args.listen_host, args.listen_port, args.upstream_host, args.upstream_port),
          flush=True)
    print("[KWSA lite TLS] TLS >=1.2 | fixed upstream | CONNECT disabled", flush=True)

    def _stop(signum=None, frame=None):  # noqa: ARG001
        print("\n[KWSA lite TLS] signal %s, shutting down..." % signum, flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, AttributeError, OSError):
            pass
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        print("[KWSA lite TLS] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
