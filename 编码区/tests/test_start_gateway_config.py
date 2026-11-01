#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_start_gateway_config.py - 启动脚本外部上游/token 配置回归。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent
sys.path.insert(0, str(ROOT_DIR / "运维区" / "scripts"))
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))

import start_gateway


class TestStartGatewayConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.token_file = self.dir / "secrets.json"
        self.token_file.write_text(
            json.dumps({"token_cloud": "cloud-token", "token_ng": "ng-token"}),
            encoding="utf-8",
        )
        self.config_file = self.dir / "upstream.json"
        self._write_config(str(self.token_file))

    def _write_config(self, token_file, base_url="https://gateway.example.invalid"):
        self.config_file.write_text(
            json.dumps({
                "base_url": base_url,
                "fallback_url": "http://127.0.0.1:8795",
                "probe_interval_s": 30,
                "probe_timeout_s": 4,
                "token_source": {
                    "file": token_file,
                    "key": "token_cloud",
                    "fallback_key": "token_ng",
                },
            }),
            encoding="utf-8",
        )

    def test_loads_upstream_and_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            start_gateway.load_upstream_config(self.config_file)

            self.assertEqual(os.environ["HUB_UPSTREAM"], "https://gateway.example.invalid")
            self.assertEqual(os.environ["HUB_UPSTREAM_CLOUD"], "https://gateway.example.invalid")
            self.assertEqual(os.environ["HUB_UPSTREAM_LOCAL"], "http://127.0.0.1:8795")
            self.assertEqual(os.environ["HUB_UPSTREAM_PROBE_INTERVAL"], "30")
            self.assertEqual(os.environ["HUB_UPSTREAM_PROBE_TIMEOUT"], "4")
            self.assertEqual(os.environ["HUB_ADMIN_TOKEN"], "cloud-token")
            self.assertEqual(os.environ["HUB_LOCAL_ADMIN_TOKEN"], "ng-token")

    def test_environment_overrides_config(self):
        with mock.patch.dict(os.environ, {
            "HUB_UPSTREAM": "http://127.0.0.1:9999",
            "HUB_UPSTREAM_LOCAL": "http://127.0.0.1:9998",
            "HUB_ADMIN_TOKEN": "env-token",
        }, clear=True):
            start_gateway.load_upstream_config(self.config_file)

            self.assertEqual(os.environ["HUB_UPSTREAM"], "http://127.0.0.1:9999")
            self.assertEqual(os.environ["HUB_UPSTREAM_LOCAL"], "http://127.0.0.1:9998")
            self.assertEqual(os.environ["HUB_ADMIN_TOKEN"], "env-token")
            self.assertEqual(os.environ["HUB_LOCAL_ADMIN_TOKEN"], "ng-token")

    def test_missing_token_file_does_not_set_token(self):
        self._write_config(str(self.dir / "missing.json"))
        with mock.patch.dict(os.environ, {}, clear=True):
            start_gateway.load_upstream_config(self.config_file)

            self.assertNotIn("HUB_ADMIN_TOKEN", os.environ)

    def test_missing_config_is_noop(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            start_gateway.load_upstream_config(self.dir / "missing.json")

            self.assertNotIn("HUB_UPSTREAM", os.environ)


if __name__ == "__main__":
    unittest.main()
