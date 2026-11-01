#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_exe_packaging.py —— 便携 exe 封装：路径口径、构建参数与密钥不进包。

覆盖三个真实事故点（2026-10-26 首次封装）：
1. 冻结后 UI 目录解析成 exe 同级 `ui`（不存在）→ 控制台全站 404；
2. 冻结后数据写回源码仓而不是 exe 目录；
3. 打包把 `运维区/data`（密钥仓 / 账本）一起带进分发件。
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "编码区" / "src"))
sys.path.insert(0, str(ROOT_DIR / "运维区" / "scripts"))

from core import paths  # noqa: E402

import build_exe  # noqa: E402
import smoke_exe  # noqa: E402
import start_gateway  # noqa: E402


class TestSourceLayout(unittest.TestCase):
    def test_roots_point_at_repo(self):
        self.assertFalse(paths.is_frozen())
        self.assertEqual(paths.APP_ROOT, ROOT_DIR)
        self.assertEqual(paths.ASSETS_ROOT, ROOT_DIR)
        self.assertEqual(paths.DATA_DIR, ROOT_DIR / "运维区" / "data")
        self.assertEqual(paths.CONFIG_DIR, ROOT_DIR / "运维区" / "configs")

    def test_ui_dir_reachable(self):
        self.assertTrue((paths.UI_DIR / "index.html").is_file())

    def test_gateway_constants_follow_paths(self):
        # 账本/价目不再各自用 __file__ 推根，统一走 paths
        from core import local_ledger, pricing
        self.assertEqual(local_ledger.DEFAULT_DB, paths.DATA_DIR / "ledger.db")
        self.assertEqual(local_ledger.DEFAULT_PRICING, paths.CONFIG_DIR / "pricing.json")
        self.assertEqual(pricing.DEFAULT_PATH, paths.CONFIG_DIR / "pricing.json")


class _FrozenEnv:
    """把进程伪装成 PyInstaller onedir 冻结态（exe 在 NEO EXE/，运行时在 _internal/）。"""

    def __init__(self, base: Path):
        # resolve()：临时目录在 Windows 上可能拿到 8.3 短名，两边必须同口径
        self.exe = (base / "NEO EXE" / "HUBNEO.exe").resolve()
        self.exe.parent.mkdir(parents=True, exist_ok=True)
        self.exe.write_bytes(b"")
        self.meipass = base / "_internal"
        self.meipass.mkdir(parents=True, exist_ok=True)
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "frozen": getattr(sys, "frozen", None),
            "_MEIPASS": getattr(sys, "_MEIPASS", None),
            "executable": sys.executable,
        }
        sys.frozen = True
        sys._MEIPASS = str(self.meipass)
        sys.executable = str(self.exe)
        return self

    def __exit__(self, *exc_info):
        for key, value in self._saved.items():
            attr = "_MEIPASS" if key == "_MEIPASS" else key
            if value is None:
                if hasattr(sys, attr):
                    delattr(sys, attr)
            else:
                setattr(sys, attr, value)
        # 常量在导入时算好，退回源码态必须重载
        importlib.reload(paths)
        importlib.reload(start_gateway)
        return False


class TestFrozenLayout(unittest.TestCase):
    def test_roots_split_assets_from_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _FrozenEnv(Path(tmp)) as env:
                frozen = importlib.reload(paths)
                self.assertTrue(frozen.is_frozen())
                self.assertEqual(frozen.APP_ROOT, env.exe.parent)
                self.assertEqual(frozen.ASSETS_ROOT, env.meipass)
                self.assertEqual(frozen.DATA_DIR, env.exe.parent / "运维区" / "data")
                self.assertEqual(frozen.CONFIG_DIR, env.exe.parent / "运维区" / "configs")
                self.assertEqual(frozen.UI_DIR,
                                 env.meipass / "编码区" / "src" / "ui")

    def test_start_gateway_reads_config_next_to_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _FrozenEnv(Path(tmp)) as env:
                gateway = importlib.reload(start_gateway)
                self.assertEqual(gateway.ROOT_DIR, env.exe.parent)
                self.assertEqual(gateway.UPSTREAM_CONFIG,
                                 env.exe.parent / "运维区" / "configs" / "upstream.json")
                self.assertEqual(gateway.BUNDLE_DIR, env.meipass)
                # 配好外部目录后 ensure_runtime_dirs 会建出 data 与 configs
                configs = env.exe.parent / "运维区" / "configs"
                data = env.exe.parent / "运维区" / "data"
                self.assertFalse(configs.exists() or data.exists())
                frozen = importlib.reload(paths)
                created = frozen.ensure_runtime_dirs()
                self.assertEqual(len(created), 2)
                self.assertTrue(configs.is_dir() and data.is_dir())


class TestBuildCommand(unittest.TestCase):
    def test_ui_assets_and_entry(self):
        cmd = build_exe.build_command(Path("stage"), Path("work"))
        self.assertIn("--onedir", cmd)
        self.assertEqual(cmd[cmd.index("--name") + 1], "HUBNEO")
        # 入口用 sys.path.insert 才能 import 到 server/core，构建必须显式 --paths
        self.assertEqual(Path(cmd[cmd.index("--paths") + 1]), build_exe.SRC_DIR)
        add_data = cmd[cmd.index("--add-data") + 1]
        self.assertTrue(add_data.endswith(f"{os.pathsep}编码区/src/ui"))
        self.assertEqual(Path(cmd[-1]), build_exe.ENTRY)
        self.assertIn("pymysql", cmd)


class TestConfigStaging(unittest.TestCase):
    def _src(self, tmp: Path) -> Path:
        src = tmp / "configs"
        src.mkdir()
        (src / "control.json").write_text("{}", encoding="utf-8")
        (src / "pricing.json").write_text("{}", encoding="utf-8")
        (src / "pricing.json.bak-20261024").write_text("{}", encoding="utf-8")
        return src

    def test_skips_backups_and_keeps_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._src(tmp)
            dst = tmp / "out"
            copied, skipped = build_exe.copy_configs(src, dst)
            self.assertEqual(sorted(copied), ["control.json", "pricing.json"])
            self.assertFalse((dst / "pricing.json.bak-20261024").exists())

            (dst / "control.json").write_text('{"edited": true}', encoding="utf-8")
            copied, skipped = build_exe.copy_configs(src, dst)
            self.assertEqual(copied, [])
            self.assertEqual(sorted(skipped), ["control.json", "pricing.json"])
            self.assertIn("edited", (dst / "control.json").read_text(encoding="utf-8"))

    def test_force_configs_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._src(tmp)
            dst = tmp / "out"
            build_exe.copy_configs(src, dst)
            (dst / "control.json").write_text('{"edited": true}', encoding="utf-8")
            copied, skipped = build_exe.copy_configs(src, dst, overwrite=True)
            self.assertEqual(sorted(copied), ["control.json", "pricing.json"])
            self.assertEqual(skipped, [])
            self.assertNotIn("edited", (dst / "control.json").read_text(encoding="utf-8"))


class TestSecretsGuard(unittest.TestCase):
    def test_blocks_bundled_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "_internal" / "编码区").mkdir(parents=True)
            build_exe.check_no_secrets(stage)          # 干净时放行
            (stage / "_internal" / "运维区" / "data").mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                build_exe.check_no_secrets(stage)


def _fake_delivery(base: Path, ui: bool = True) -> Path:
    """造一个最小交付目录；ui=False 用来复现“全站 404”那类结构缺失。"""
    delivery = base / "NEO EXE"
    (delivery / "_internal").mkdir(parents=True)
    if ui:
        ui_dir = delivery / "_internal" / "编码区" / "src" / "ui"
        (ui_dir / "assets" / "js").mkdir(parents=True)
        (ui_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        (ui_dir / "assets" / "js" / "app.js").write_text("// app\n" * 40,
                                                           encoding="utf-8")
    configs = delivery / "运维区" / "configs"
    configs.mkdir(parents=True)
    for name in ("model_upstreams.json", "control.json",
                 "model_groups.json", "pricing.json", "services_registry.json"):
        (configs / name).write_text("{}", encoding="utf-8")
    (configs / "upstream.json").write_text(
        '{"base_url": "https://example.invalid",'
        ' "ledger": {"backend": "mysql", "host": "127.0.0.1", "table": "usage"}}',
        encoding="utf-8")
    data = delivery / "运维区" / "data"
    data.mkdir()
    (data / "vendor_creds.key").write_bytes(b"secret")
    (data / "ledger.db").write_bytes(b"db")
    (delivery / "HUBNEO.exe").write_bytes(b"MZ")
    build_exe.write_launcher(delivery)
    build_exe.write_stopper(delivery)
    build_exe.isolate_ledger(delivery)
    build_exe.copy_manual(delivery)
    return delivery


class TestDoubleClickEntry(unittest.TestCase):
    def test_launcher_written_as_gbk_with_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_exe.write_launcher(Path(tmp))
            self.assertEqual(path.name, "启动 HUB NEO.cmd")
            raw = path.read_bytes()
            # cmd.exe 按系统 OEM 代码页读批处理；UTF-8 落盘会让中文变乱码
            text = raw.decode("gbk")
            self.assertIn("chcp 936", text)
            self.assertIn("HUBNEO.exe", text)
            self.assertIn("pause", text)
            self.assertIn(b"\r\n", raw)
            with self.assertRaises(UnicodeDecodeError):
                raw.decode("utf-8")

    def test_stopper_calls_stop_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_exe.write_stopper(Path(tmp))
            self.assertEqual(path.name, "停止 HUB NEO.cmd")
            text = path.read_bytes().decode("gbk")
            self.assertIn("chcp 936", text)
            self.assertIn("--stop", text)
            self.assertIn("HUBNEO.exe", text)
            self.assertIn("pause", text)

    def test_manual_shipped_and_documents_tree(self):
        self.assertTrue(build_exe.MANUAL_SRC.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            target = build_exe.copy_manual(Path(tmp))
            self.assertEqual(target.name, "使用说明.md")
            text = target.read_text(encoding="utf-8")
            for keyword in ("目录结构", "运维区/data", "排障", "升级"):
                self.assertIn(keyword, text)


class TestAutoOpenBrowser(unittest.TestCase):
    """双击 exe 自动开控制台：必须等端口真能连上再开，且不真的弹窗（替身）。"""

    def test_opens_only_after_port_listening(self):
        import socket
        import threading
        opened = []
        original = start_gateway.webbrowser.open
        start_gateway.webbrowser.open = lambda url: opened.append(url) or True
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                port = sock.getsockname()[1]
                self.assertTrue(start_gateway.open_browser_when_ready(port, timeout=3))
                self.assertEqual(opened, [f"http://127.0.0.1:{port}/"])
            self.assertFalse(start_gateway.open_browser_when_ready(port, timeout=0.5))
        finally:
            start_gateway.webbrowser.open = original
        self.assertEqual(len(opened), 1)

    def test_flag_parsing(self):
        self.assertTrue(start_gateway._env_flag("HUB_OPEN_BROWSER", True))
        os.environ["HUB_OPEN_BROWSER"] = "0"
        try:
            self.assertFalse(start_gateway._env_flag("HUB_OPEN_BROWSER", True))
        finally:
            os.environ.pop("HUB_OPEN_BROWSER", None)


class TestLedgerIsolation(unittest.TestCase):
    """便携包默认分表记账，不能和源码版抢同一张 usage 表。"""

    def test_default_table_differs_from_source(self):
        import json
        source = json.loads((ROOT_DIR / "运维区" / "configs" / "upstream.json")
                            .read_text(encoding="utf-8"))
        self.assertNotEqual(build_exe.DEFAULT_LEDGER_TABLE,
                            source["ledger"]["table"])

    def test_isolate_rewrites_only_table(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            delivery = _fake_delivery(Path(tmp))
            path = (delivery / "运维区" / "configs" / "upstream.json")
            path.write_text(json.dumps({"base_url": "https://x",
                                        "ledger": {"backend": "mysql",
                                                   "table": "usage",
                                                   "host": "127.0.0.1"}},
                                       ensure_ascii=False), encoding="utf-8")
            build_exe.isolate_ledger(delivery, "usage_portable")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["ledger"]["table"], "usage_portable")
            # 其余字段一个字不能少：这个文件是运行配置，不是只有账本表
            self.assertEqual(data["base_url"], "https://x")
            self.assertEqual(data["ledger"]["backend"], "mysql")
            self.assertEqual(data["ledger"]["host"], "127.0.0.1")

    def test_missing_upstream_json_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                build_exe.isolate_ledger(Path(tmp))


class TestStopSwitch(unittest.TestCase):
    def test_stop_reports_not_running(self):
        port = smoke_exe.free_port(8896)
        self.assertEqual(start_gateway.stop_running(port, timeout=0.5), 3)

    def test_listener_pid_unknown_port_is_none(self):
        port = smoke_exe.free_port(8895)
        self.assertIsNone(start_gateway._listener_pid(port))


class TestSmokeLayout(unittest.TestCase):
    def test_complete_delivery_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            delivery = _fake_delivery(Path(tmp))
            results = smoke_exe.check_layout(delivery)
            self.assertTrue(all(ok for ok, _, _ in results),
                            [r for r in results if not r[0]])

    def test_missing_ui_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            delivery = _fake_delivery(Path(tmp), ui=False)
            failed = [name for ok, name, _ in smoke_exe.check_layout(delivery) if not ok]
            self.assertIn("控制台首页资源", failed)

    def test_secrets_in_bundle_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            delivery = _fake_delivery(Path(tmp))
            (delivery / "_internal" / "运维区" / "data").mkdir(parents=True)
            failed = [name for ok, name, _ in smoke_exe.check_layout(delivery) if not ok]
            self.assertIn("包内无 运维区/data", failed)

    def test_free_port_is_listening(self):
        import socket
        port = smoke_exe.free_port()
        self.assertGreater(port, 0)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))     # 拿到就能绑，没和别的东西撞上


class TestArchive(unittest.TestCase):
    def test_members_exclude_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            delivery = _fake_delivery(Path(tmp))
            members = build_exe.archive_members(delivery)
            rel = [p.relative_to(delivery).as_posix() for p in members]
            self.assertIn("HUBNEO.exe", rel)
            self.assertIn("_internal/编码区/src/ui/index.html", rel)
            self.assertFalse([r for r in rel if r.startswith("运维区/data/")], rel)

    def test_zip_checksums_and_manifest(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            delivery = _fake_delivery(tmp)
            paths = build_exe.archive(delivery, smoke_ok=True, date="20260101",
                                      archive_root=tmp / "releases",
                                      checksum_root=tmp / "checksums")
            self.assertTrue(paths["zip"].is_file())
            self.assertEqual(paths["zip"].parent.name, "NEO_EXE_20260101")
            with zipfile.ZipFile(paths["zip"]) as archive:
                names = archive.namelist()
            # 解压后是「NEO EXE/」目录，不会把壳文件散一地
            self.assertIn("NEO EXE/HUBNEO.exe", names)
            self.assertFalse([n for n in names if "/运维区/data/" in n], names)

            checksums = paths["checksums"].read_text(encoding="utf-8")
            self.assertIn("HUBNEO_portable_20260101.zip", checksums)
            self.assertIn("HUBNEO.exe", checksums)
            self.assertIn("启动 HUB NEO.cmd", checksums)

            manifest = paths["manifest"].read_text(encoding="utf-8")
            self.assertIn("PASS", manifest)
            self.assertIn("运维区/data", manifest)


class TestDataDirOverride(unittest.TestCase):
    """冒烟靠 HUB_DATA_DIR 把数据改道到临时目录；必须真生效。"""

    def test_env_redirects_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HUB_DATA_DIR"] = tmp
            try:
                redirected = importlib.reload(paths)
                self.assertEqual(redirected.DATA_DIR, Path(tmp).resolve())
            finally:
                os.environ.pop("HUB_DATA_DIR", None)
                importlib.reload(paths)
        self.assertEqual(paths.DATA_DIR, ROOT_DIR / "运维区" / "data")


if __name__ == "__main__":
    unittest.main()
