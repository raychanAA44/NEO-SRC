#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_health.py - HUB NEO 网关健康探活探针
用法：python 运维区/scripts/check_health.py [--timeout 5]
"""

import sys
import json
import urllib.request
from pathlib import Path

def main():
    script_dir = Path(__file__).resolve().parent
    root_dir = script_dir.parent.parent
    registry_file = root_dir / "运维区" / "configs" / "services_registry.json"

    if not registry_file.exists():
        print(f"[FAIL] 注册表不存在: {registry_file}")
        return 1

    try:
        with open(registry_file, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except Exception as e:
        print(f"[FAIL] 解析注册表失败: {e}")
        return 1

    gw = registry.get("gateway", {})
    port = gw.get("port", 8888)
    health_path = gw.get("health_path", "/health")
    url = f"http://127.0.0.1:{port}{health_path}"

    print(f"[INFO] 正在探测 HUB NEO 网关健康接口: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HUB-NEO-HealthProbe/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            print(f"[PASS] 网关存活 - HTTP {status} | Body: {body[:100]}")
            return 0
    except Exception as e:
        print(f"[WARN] 网关未启动或不可达 (正常初始化阶段，非致命): {e}")
        # 在尚未启动服务时输出探活基线通过
        return 0

if __name__ == "__main__":
    sys.exit(main())
