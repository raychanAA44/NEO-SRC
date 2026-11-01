#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/vendor_session.py —— 浏览器会话只读探针（Firefox + Chromium 系）。

三不原则：**只读、不写回、不刷新**。
- 只读：`file:...?mode=ro&immutable=1` 直接开；打不开就把 `cookies.sqlite` 复制到临时目录读副本，
  源文件一个字节都不动。
- 不写回：不存在任何 UPDATE / INSERT / DELETE 通道。
- 拿不到就抛 `ProfileUnavailable`，绝不猜、不伪造令牌。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path

COOKIE_DB_NAME = "cookies.sqlite"
# Chromium 系（Chrome / Edge / Brave）的 cookie 库与解密密钥相对路径
CHROMIUM_BROWSERS = (("Google", "Chrome", "User Data"),
                     ("Microsoft", "Edge", "User Data"),
                     ("BraveSoftware", "Brave-Browser", "User Data"))


class ProfileUnavailable(RuntimeError):
    """本机没有可读的浏览器 profile / 没有该域名的 cookie。"""


def profile_root(root=None) -> Path:
    """Firefox profile 根目录：显式路径 > `%APPDATA%\\Mozilla\\Firefox`。"""
    if root:
        return Path(root)
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Mozilla" / "Firefox"


def find_cookie_dbs(root=None) -> list[Path]:
    """列出可读的 cookies.sqlite（只挑文件存在的）。"""
    base = profile_root(root)
    if not base.is_dir():
        return []
    found = []
    try:
        for path in base.rglob(COOKIE_DB_NAME):
            if path.is_file():
                found.append(path)
    except OSError:
        return []
    return found


def sqlite_ro_uri(path: Path) -> str:
    """→ `file:///C:/...?mode=ro&immutable=1`。

    Windows 绝对路径必须是三斜杠形式，且空格/中文要转义，否则 sqlite 报
    `unable to open database file`（这与权限无关，纯 URI 语法问题）。
    """
    raw = path.resolve().as_posix()
    if len(raw) > 1 and raw[1] == ":":
        raw = "/" + raw
    return "file://%s?mode=ro&immutable=1" % urllib.parse.quote(raw, safe="/:")


def _open_readonly(path: Path):
    """只读打开；live 库被锁时复制副本再读（副本在临时目录，读完即删）。"""
    uri = sqlite_ro_uri(path)
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        con.execute("SELECT 1 FROM moz_cookies LIMIT 1").fetchall()
        return con, None
    except sqlite3.Error:
        pass
    folder = Path(tempfile.mkdtemp(prefix="neo_ff_sess_"))
    copy = folder / COOKIE_DB_NAME
    try:
        shutil.copy2(str(path), str(copy))
        return sqlite3.connect(str(copy), timeout=2), folder
    except (OSError, sqlite3.Error) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise ProfileUnavailable("cookies.sqlite 不可读: %s" % exc) from exc


def _read_firefox_cookies(host_contains: str, names=(), root=None) -> dict:
    """按域名子串读取 Firefox cookie（`{name: value}`）；读不到抛 ProfileUnavailable。"""
    dbs = find_cookie_dbs(root)
    if not dbs:
        raise ProfileUnavailable("本机未找到 Firefox profile / cookies.sqlite")
    host = str(host_contains or "").strip().lower()
    if not host:
        raise ProfileUnavailable("host_contains 必填")
    wanted = {str(n).lower() for n in (names or ()) if str(n).strip()}
    for db in dbs:
        con, tmp_dir = None, None
        try:
            con, tmp_dir = _open_readonly(db)
            sql = ("SELECT name, value, host FROM moz_cookies "
                   "WHERE lower(host) LIKE ? ORDER BY lastAccessed DESC")
            rows = con.execute(sql, ("%" + host + "%",)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        out = {}
        for name, value, _cookie_host in rows:
            key = str(name or "")
            if wanted and key.lower() not in wanted:
                continue
            if key and key not in out:
                out[key] = str(value or "")
        if out:
            return out
    raise ProfileUnavailable("未找到 %s 的会话 cookie（可能未登录或使用了别的浏览器）" % host)


def read_cookies(host_contains: str, names=(), root=None) -> dict:
    """跨浏览器只读取 cookie：先 Firefox，再 Chrome / Edge / Brave。

    `root` 显式给定时只读那个 profile（测试/指定路径用），不去扫本机浏览器。
    """
    firefox_error = None
    try:
        return _read_firefox_cookies(host_contains, names, root)
    except ProfileUnavailable as exc:
        firefox_error = exc
    if root is not None:
        raise firefox_error
    try:
        return read_cookies_chromium(host_contains, names)
    except ProfileUnavailable as exc:
        raise ProfileUnavailable("%s；%s" % (firefox_error, exc)) from exc


def session_token(host_contains: str, names=(), root=None) -> str:
    """取第一条非空会话令牌；没有则抛 ProfileUnavailable。"""
    cookies = read_cookies(host_contains, names=names, root=root)
    for value in cookies.values():
        if value:
            return value
    raise ProfileUnavailable("cookie 存在但值为空")


# ── Chromium 系：cookies 库 + DPAPI 取解密密钥 ─────────────────────
# Chrome / Edge / Brave 的 cookie 值是 `v10|v11 + nonce(12) + 密文+tag`，密钥由
# Local State 的 `os_crypt.encrypted_key` 经 Windows DPAPI 解出。全程只读副本。


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Windows DPAPI 解密（ctypes 直调 crypt32，不额外装包）。"""
    if os.name != "nt":
        raise ProfileUnavailable("DPAPI 只在 Windows 可用")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    src = _DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    dst = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst))
    if not ok:
        raise ProfileUnavailable("DPAPI 解密失败（密钥属于别的用户或已损坏）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _copy_shared(src: Path, dst: Path) -> None:
    """用共享读方式拷一份（浏览器运行中独占 cookie 库时 Python 直拷会 WinError 32）。"""
    if os.name != "nt":
        shutil.copy2(str(src), str(dst))
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p]
    kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    # 0x80000000=GENERIC_READ, 0x7=READ|WRITE|DELETE 共享, 3=OPEN_EXISTING, 0x80=NORMAL
    handle = kernel32.CreateFileW(str(src), 0x80000000, 0x7, None, 3, 0x80, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        shutil.copy2(str(src), str(dst))
        return
    try:
        size_high = ctypes.c_uint32(0)
        kernel32.GetFileSize.restype = ctypes.c_uint32
        size_low = kernel32.GetFileSize(ctypes.c_void_p(handle),
                                        ctypes.byref(size_high))
        left = (int(size_high.value) << 32) | int(size_low or 0)
        chunk = ctypes.create_string_buffer(1 << 20)
        read = ctypes.c_uint32(0)
        with open(dst, "wb") as out:
            while left > 0:
                want = min(left, 1 << 20)
                if not kernel32.ReadFile(ctypes.c_void_p(handle), chunk, want,
                                         ctypes.byref(read), None):
                    raise ProfileUnavailable("共享读 cookie 库失败")
                if not read.value:
                    break
                out.write(chunk.raw[:read.value])
                left -= read.value
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def chromium_root(root=None) -> Path:
    """Chromium 系用户数据根：显式路径 > `%LOCALAPPDATA%`。"""
    if root:
        return Path(root)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local)


def find_chromium_cookie_dbs(root=None) -> list:
    """列出 `<浏览器>/User Data/*/Network/Cookies` 里存在的库。"""
    base = chromium_root(root)
    found = []
    for parts in CHROMIUM_BROWSERS:
        user_data = base.joinpath(*parts)
        if not user_data.is_dir():
            continue
        try:
            for path in user_data.glob("*/Network/Cookies"):
                if path.is_file():
                    found.append(path)
        except OSError:
            continue
    return found


def _chromium_key(root=None) -> bytes:
    """→ AES-256 密钥（`encrypted_key` 去 `DPAPI` 前缀后 DPAPI 解密）。"""
    base = chromium_root(root)
    for parts in CHROMIUM_BROWSERS:
        state = base.joinpath(*parts) / "Local State"
        if not state.is_file():
            continue
        try:
            doc = json.loads(state.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        raw = base64.b64decode(str((doc.get("os_crypt") or {}).get("encrypted_key") or ""))
        if raw.startswith(b"DPAPI"):
            raw = raw[5:]
        if not raw:
            continue
        return _dpapi_unprotect(raw)
    raise ProfileUnavailable("未找到 Chromium 系 Local State（拿不到 cookie 解密密钥）")


def _chromium_value(blob: bytes, key: bytes) -> str:
    """解密单条 `encrypted_value`；v20（应用绑定加密）如实报不可解。"""
    if not blob:
        return ""
    if blob[:3] in (b"v10", b"v11"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:          # pragma: no cover  环境缺包
            raise ProfileUnavailable("缺 cryptography，解不开 Chromium cookie") from exc
        plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
        return plain.decode("utf-8", "replace")
    if blob[:3] == b"v20":
        raise ProfileUnavailable("Chrome 127+ 应用绑定加密（v20）无法离线解密")
    return _dpapi_unprotect(blob).decode("utf-8", "replace")


def read_cookies_chromium(host_contains: str, names=(), root=None) -> dict:
    """按域名子串从 Chromium 系读 cookie（`{name: value}`）；读不到抛 ProfileUnavailable。"""
    host = str(host_contains or "").strip().lower()
    if not host:
        raise ProfileUnavailable("host_contains 必填")
    dbs = find_chromium_cookie_dbs(root)
    if not dbs:
        raise ProfileUnavailable("本机未找到 Chrome / Edge / Brave 的 cookie 库")
    wanted = {str(n).lower() for n in (names or ()) if str(n).strip()}
    key, key_error = None, None
    errors = []
    for db in dbs:
        con, tmp_dir = None, None
        try:
            uri = sqlite_ro_uri(db)
            try:
                con = sqlite3.connect(uri, uri=True, timeout=2)
                con.execute("SELECT 1 FROM cookies LIMIT 1").fetchall()
            except sqlite3.Error:
                if con is not None:
                    con.close()
                tmp_dir = Path(tempfile.mkdtemp(prefix="neo_cr_sess_"))
                copy = tmp_dir / db.name
                _copy_shared(db, copy)
                # WAL 里可能还有未落主库的条目：一起拷，否则表读不到
                for suffix in ("-wal", "-shm"):
                    side = db.with_name(db.name + suffix)
                    if side.is_file():
                        try:
                            _copy_shared(side, copy.with_name(copy.name + suffix))
                        except (OSError, ProfileUnavailable):
                            pass
                con = sqlite3.connect(str(copy), timeout=2)
            rows = con.execute(
                "SELECT name, value, encrypted_value, host_key FROM cookies "
                "WHERE lower(host_key) LIKE ? ORDER BY last_access_utc DESC",
                ("%" + host + "%",)).fetchall()
        except (OSError, sqlite3.Error) as exc:
            text = str(exc)
            if isinstance(exc, PermissionError) or "WinError 32" in text:
                text = "浏览器正在运行并独占 cookie 库：先退出该浏览器再试，或改用会话粘贴"
            errors.append(text)
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        out = {}
        for name, value, encrypted, _host in rows:
            key_name = str(name or "")
            if not key_name or (wanted and key_name.lower() not in wanted):
                continue
            if key_name in out:
                continue
            plain = str(value or "")
            if not plain and encrypted:
                if key is None and key_error is None:
                    try:
                        key = _chromium_key(root)
                    except ProfileUnavailable as exc:
                        key_error = str(exc)
                if key_error:
                    errors.append(key_error)
                    continue
                try:
                    plain = _chromium_value(encrypted, key)
                except ProfileUnavailable as exc:
                    errors.append(str(exc))
                    continue
                except Exception:            # noqa: BLE001  单条损坏不影响其余
                    continue
            if plain:
                out[key_name] = plain
        if out:
            return out
    raise ProfileUnavailable("未找到 %s 的会话 cookie（Chrome/Edge/Brave 均无有效条目%s）" % (
        host, "：" + errors[0] if errors else ""))


__all__ = ["ProfileUnavailable", "profile_root", "find_cookie_dbs", "read_cookies",
           "session_token", "COOKIE_DB_NAME", "chromium_root",
           "find_chromium_cookie_dbs", "read_cookies_chromium"]
