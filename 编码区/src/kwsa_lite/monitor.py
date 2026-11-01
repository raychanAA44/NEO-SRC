# -*- coding: utf-8 -*-
"""中间层监视：把「一次真实请求」拆成 DNS / TCP / TLS 出口 / 首字节 四段。

用途是回答一个具体问题——**慢在哪一层、通没通**。所以探针不做假数据：

1. 先按渠道地址解析 DNS；
2. 再建 TCP 连接（走代理腿时连的是代理，标签会写明「经代理」）；
3. 然后用这条渠道的出口发一个真实请求，量到响应头为止（TTFB）；
4. 全程计时逐段返回，失败时把错误定位到具体那一段。

探针只打 ``/v1/models``（或渠道自己配的探针路径），不产生推理 token。
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .egress import Egress


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _hop(name: str, ms, ok: bool = True, note: str = "") -> dict:
    return {"name": name, "ms": None if ms is None else round(float(ms), 1),
            "ok": bool(ok), "note": note}


def probe_chain(base_url: str, path: str = "/v1/models", headers=None,
                egress: Egress | None = None, leg_id: str = "",
                timeout: float = 8.0) -> dict:
    """真实探一次链路，返回分段耗时。绝不抛异常。"""
    base = str(base_url or "").strip().rstrip("/")
    hops = []
    target = ""
    status = None
    error = ""
    total_start = time.perf_counter()
    try:
        parts = urlsplit(base)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not host:
            raise ValueError("上游地址缺少主机名")
        target = "%s:%s" % (host, port)

        # ① DNS
        t0 = time.perf_counter()
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            dns_ms = _ms(t0)
            address = infos[0][4][0] if infos else ""
            hops.append(_hop("DNS", dns_ms, True, address))
        except Exception as exc:  # noqa: BLE001
            hops.append(_hop("DNS", _ms(t0), False, str(exc)[:120]))
            raise

        # ② TCP（有代理腿时连的是代理）
        proxy_url = egress.resolve(leg_id) if egress is not None else ""
        dial_host, dial_port = host, port
        if proxy_url:
            pp = urlsplit(proxy_url)
            dial_host = pp.hostname or host
            dial_port = pp.port or (443 if pp.scheme == "https" else 80)
        t0 = time.perf_counter()
        sock = None
        try:
            sock = socket.create_connection((dial_host, dial_port), timeout=timeout)
            hops.append(_hop("TCP", _ms(t0), True,
                             ("经代理 %s" % proxy_url) if proxy_url else target))
        except Exception as exc:  # noqa: BLE001
            hops.append(_hop("TCP", _ms(t0), False, str(exc)[:120]))
            raise

        # ③ TLS（直连 https 才真的握手；有代理时交给 urllib 的 CONNECT 隧道）
        if parts.scheme == "https" and not proxy_url:
            t0 = time.perf_counter()
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ctx.wrap_socket(sock, server_hostname=host).close()
                hops.append(_hop("TLS", _ms(t0), True, "握手完成"))
            except Exception as exc:  # noqa: BLE001
                hops.append(_hop("TLS", _ms(t0), False, str(exc)[:120]))
                raise
        else:
            hops.append(_hop("TLS", None, True,
                             "代理隧道内完成" if proxy_url else "明文 http"))
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        # ④ 首字节（真实请求）
        url = _probe_url(base, path)
        req_headers = {"Accept": "application/json", "User-Agent": "kwsa-lite/monitor"}
        for key, value in (headers or {}).items():
            if str(key).lower() in ("authorization", "user-agent", "accept", "content-type"):
                req_headers[str(key)] = str(value)
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        t0 = time.perf_counter()
        opener = egress if egress is not None else Egress()
        with opener.open(req, timeout, leg_id) as res:
            status = int(getattr(res, "status", 200))
            ttfb = _ms(t0)
            body = res.read(64 * 1024)
        hops.append(_hop("TTFB", ttfb, True, "HTTP %s" % status))
        hops.append(_hop("TOTAL", _ms(total_start), 200 <= status < 400,
                         "%d bytes" % len(body)))
        return {
            "ok": 200 <= status < 400,
            "status": status,
            "target": target,
            "url": url,
            "egress": proxy_url or "direct",
            "hops": hops,
            "error": "",
            "checked_at": int(time.time()),
        }
    except urllib.error.HTTPError as exc:
        hops.append(_hop("TTFB", None, False, "HTTP %s" % exc.code))
        return {"ok": False, "status": int(exc.code), "target": target,
                "egress": (egress.resolve(leg_id) if egress is not None else "direct"),
                "hops": hops, "error": "HTTP %s" % exc.code,
                "checked_at": int(time.time())}
    except Exception as exc:  # noqa: BLE001 探针失败只报事实
        return {"ok": False, "status": status, "target": target,
                "egress": (egress.resolve(leg_id) if egress is not None else "direct"),
                "hops": hops, "error": str(exc)[:200],
                "checked_at": int(time.time())}


def _probe_url(base: str, path: str) -> str:
    tail = "/" + str(path or "/v1/models").lstrip("/")
    if base.endswith("/v1") and tail.startswith("/v1/"):
        return base + tail[3:]
    if base.endswith("/v1") and not tail.startswith("/v1/"):
        # 渠道探针路径是站点根路径（如 /health），不能拼在 /v1 后面。
        return base[:-3] + tail
    return base + tail


def percentiles(values, points=(50, 95)) -> dict:
    data = sorted(float(v) for v in (values or []) if v is not None)
    if not data:
        return {("p%d" % p): None for p in points}
    out = {}
    for point in points:
        index = min(len(data) - 1, max(0, int(round((point / 100.0) * (len(data) - 1)))))
        out["p%d" % point] = round(data[index], 1)
    return out


def summarize(samples) -> dict:
    """把网关自己记的样本聚成「路由 / 上游 / 首字节 / 总耗时」四列。"""
    rows = list(samples or [])
    ok_rows = [r for r in rows if r.get("ok")]
    return {
        "count": len(rows),
        "ok": len(ok_rows),
        "errors": len(rows) - len(ok_rows),
        "route": percentiles([r.get("route_ms") for r in ok_rows]),
        "upstream": percentiles([r.get("upstream_ms") for r in ok_rows]),
        "ttft": percentiles([r.get("ttft_ms") for r in ok_rows if r.get("ttft_ms")]),
        "total": percentiles([r.get("total_ms") for r in ok_rows]),
    }


__all__ = ["probe_chain", "percentiles", "summarize"]
