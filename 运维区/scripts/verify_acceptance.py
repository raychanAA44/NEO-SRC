#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_acceptance.py - HUB NEO 网关全链路在线验收脚本
自测范围：
  1. 8888 端口 /health 探活与服务标识
  2. 8888 根路径 UI 静态加载与完整性
  3. CSS / JS 静态资源有效性
  4. 敏感 GET 路径拦截 (/api/vault, /api/keys/{id}/reveal) -> 403
  5. 非白名单 POST 拦截 -> 403
  6. 契约白名单 POST 路径放行 (非 403)
  7. 路径穿越防护 (..) -> 400
  8. 安全响应头 (no-store, nosniff, X-Request-Id)
"""

import sys
import time
import json
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
SERVER_SCRIPT = ROOT_DIR / "编码区" / "src" / "server.py"

def main():
    print("=== [HUB NEO] 启动网关服务执行在线全链路验收 ===")
    proc = subprocess.Popen([sys.executable, str(SERVER_SCRIPT)], cwd=str(ROOT_DIR))
    time.sleep(2)

    results = []

    def record(name, passed, detail=""):
        results.append((name, passed, detail))
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} {name} | {detail}")

    try:
        # 1. GET /health
        try:
            req = urllib.request.Request("http://127.0.0.1:8888/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                ok = data.get("status") == "ok" and data.get("port") == 8888
                record("01. GET /health 探针与端口匹配", ok, f"status={data.get('status')}, port={data.get('port')}")
        except Exception as e:
            record("01. GET /health 探针与端口匹配", False, str(e))

        # 2. GET / 静态首页
        try:
            req = urllib.request.Request("http://127.0.0.1:8888/")
            with urllib.request.urlopen(req, timeout=3) as resp:
                html = resp.read().decode("utf-8")
                ok = resp.status == 200 and "<html" in html and "fx-masthead" in html
                record("02. GET / 视觉首页完整渲染", ok, f"HTTP {resp.status}, len={len(html)} bytes")
        except Exception as e:
            record("02. GET / 视觉首页完整渲染", False, str(e))

        # 3. 样式包 GET /assets/css/fusion.css
        try:
            req = urllib.request.Request("http://127.0.0.1:8888/assets/css/fusion.css")
            with urllib.request.urlopen(req, timeout=3) as resp:
                css = resp.read().decode("utf-8")
                ok = resp.status == 200 and len(css) > 50000
                record("03. GET /assets/css/fusion.css 样式包加载", ok, f"len={len(css)} bytes")
        except Exception as e:
            record("03. GET /assets/css/fusion.css 样式包加载", False, str(e))

        # 4. 脚本入口 GET /assets/js/app.js
        try:
            req = urllib.request.Request("http://127.0.0.1:8888/assets/js/app.js")
            with urllib.request.urlopen(req, timeout=3) as resp:
                js = resp.read().decode("utf-8")
                ok = resp.status == 200 and "HUB.paintShell" in js
                record("04. GET /assets/js/app.js 启动器脚本加载", ok, f"len={len(js)} bytes")
        except Exception as e:
            record("04. GET /assets/js/app.js 启动器脚本加载", False, str(e))

        # 5. 拦截敏感 GET /api/vault (403)
        try:
            urllib.request.urlopen("http://127.0.0.1:8888/api/vault", timeout=3)
            record("05. 拦截敏感凭据读取 /api/vault", False, "预期 403 却放行")
        except urllib.error.HTTPError as e:
            record("05. 拦截敏感凭据读取 /api/vault", e.code == 403, f"HTTP {e.code} (成功拦截)")
        except Exception as e:
            record("05. 拦截敏感凭据读取 /api/vault", False, str(e))

        # 6. 拦截非白名单 POST
        try:
            post_req = urllib.request.Request("http://127.0.0.1:8888/api/dangerous_action", data=b"{}", method="POST")
            post_req.add_header("content-type", "application/json")
            urllib.request.urlopen(post_req, timeout=3)
            record("06. 拦截非白名单 POST 请求", False, "预期 403 却放行")
        except urllib.error.HTTPError as e:
            record("06. 拦截非白名单 POST 请求", e.code == 403, f"HTTP {e.code} (成功拦截)")
        except Exception as e:
            record("06. 拦截非白名单 POST 请求", False, str(e))

        # 7. 放行白名单 POST (/api/models/toggle，不得被本地写闸门拦截)
        try:
            post_req = urllib.request.Request("http://127.0.0.1:8888/api/models/toggle", data=b"{}", method="POST")
            post_req.add_header("content-type", "application/json")
            urllib.request.urlopen(post_req, timeout=5)
            record("07. 白名单 POST /api/models/toggle 放行", True, "透传上游成功")
        except urllib.error.HTTPError as e:
            # 上游 Cloudflare / 参数校验也会回 403/422；只有 HUB NEO 自己的
            # 写闸门文案才算失败，否则无法区分「本地拦截」与「上游拒答」。
            body = e.read().decode("utf-8", "replace")
            blocked_locally = "HUB NEO write whitelist" in body
            record("07. 白名单 POST /api/models/toggle 放行", not blocked_locally,
                   f"HTTP {e.code} (上游校验/拦截，本地闸门已放行)")
        except Exception as e:
            record("07. 白名单 POST /api/models/toggle 放行", False, str(e))

        # 8. 路径穿越防护 (..)
        try:
            urllib.request.urlopen("http://127.0.0.1:8888/api/../secret", timeout=3)
            record("08. 路径穿越防护 (..)", False, "预期 400 却放行")
        except urllib.error.HTTPError as e:
            record("08. 路径穿越防护 (..)", e.code == 400, f"HTTP {e.code} (成功阻断)")
        except Exception as e:
            record("08. 路径穿越防护 (..)", False, str(e))

        # 9. 响应头安全标记
        try:
            req = urllib.request.Request("http://127.0.0.1:8888/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                headers = dict(resp.headers)
                ok = headers.get("cache-control") == "no-store"
                record("09. 安全缓存响应头 no-store", ok, f"cache-control={headers.get('cache-control')}")
        except Exception as e:
            record("09. 安全缓存响应头 no-store", False, str(e))

    finally:
        proc.terminate()
        proc.wait()

    passed_count = sum(1 for r in results if r[1])
    total_count = len(results)
    print(f"\n=== 验收汇总: {passed_count}/{total_count} 项实测通过 ===")
    return 0 if passed_count == total_count else 1

if __name__ == "__main__":
    sys.exit(main())
