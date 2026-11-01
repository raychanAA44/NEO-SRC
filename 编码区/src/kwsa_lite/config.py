# -*- coding: utf-8 -*-
"""KWSA lite 配置：单文件 JSON，读时热加载，控制面原子回写。

设计边界：
- 上游密钥只进加密密钥仓或环境变量（``token_env`` / ``headers`` 里的 ``env:VAR``），
  配置文件中出现明文字段 ``token`` 直接拒绝启动。
- 官方账单登录账号/密码也只进密钥仓；配置只保存登录地址与账单路径，不落明文。
- 配置变更按 mtime+size 热加载，不需要重启进程；控制面写入用临时文件 + ``os.replace``。
- 端口禁区与 NEO 相同：8792 永久废弃，8795/8791 是外部关联端口，不得占用。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from .vault import VaultError, mask_token

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_PORTS = {8791, 8792, 8795}
DEFAULT_PORT = 8798
DEFAULT_MAX_BODY = 32 * 1024 * 1024

ALLOWED_PROTOCOLS = ("http://", "https://")
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EGRESS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
RESERVED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})

# 停用原因码是封闭集合：码固定文案，实测证据（上游 HTTP 原文）另存 evidence，
# 这样面板能一句话说清「为什么不可用」，而不用把上游报文塞进标签。
UNAVAILABLE_CODES = {
    "manual": "手动停用",
    "channel_off": "渠道已停用",
    "upstream_plan": "上游计划不含此模型",
    "upstream_denied": "上游拒绝此 Key 访问",
    "upstream_blocked": "上游风控拦截",
    "upstream_gate": "缺上游门禁请求头",
    "upstream_rollout": "上游仍在灰测放量",
    "upstream_missing": "上游已无此模型",
    "auth_error": "上游鉴权失败",
    "rate_limited": "上游限流",
    "bridge_token": "桥 Token 失效",
    "probe_ok": "复检已通过，可启用",
    "upstream_error": "上游报错",
    "network_error": "网络不可达",
}


def unavailable_label(code: str) -> str:
    return UNAVAILABLE_CODES.get(str(code or ""), "原因未记录")


class ConfigError(ValueError):
    """配置结构或安全约束不满足，启动即失败。"""


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "off", "no", "")


def _mask_user(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 2:
        return "●" * len(text)
    return text[0] + "●" * min(6, len(text) - 1)


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _control_state(raw, default: bool = True) -> dict:
    """控制面一行：兼容历史写法 ``bool`` 与带解释的 ``{enabled, code, reason, ...}``。"""
    if isinstance(raw, dict):
        state = {"enabled": _as_bool(raw.get("enabled"), default)}
        for key in ("code", "reason", "evidence", "fix"):
            value = str(raw.get(key) or "").strip()
            if value:
                state[key] = value[:240]
        for key in ("since", "tested_at"):
            stamp = _as_int(raw.get(key), 0)
            if stamp > 0:
                state[key] = stamp
        return state
    if isinstance(raw, bool):
        return {"enabled": raw}
    return {"enabled": default}


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str_list(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _resolve_db_path(value) -> Path:
    path = Path(str(value or "运维区/data/kwsa_lite.db"))
    return path if path.is_absolute() else (ROOT / path)


def _valid_port(port: int) -> bool:
    return 1024 <= int(port) <= 65535 and int(port) not in FORBIDDEN_PORTS


def is_loopback_host(host: str) -> bool:
    """回环判定只认本机地址；`0.0.0.0`/`::`/LAN IP 一律按外部监听处理。"""
    text = str(host or "").strip().strip("[]").lower()
    return text in ("localhost", "::1") or text.startswith("127.")


class Config:
    """一份 KWSA lite 配置的只读快照 + 控制面写入能力。"""

    def __init__(self, doc: dict, path=None, environ=None):
        if not isinstance(doc, dict):
            raise ConfigError("配置根节点必须是 JSON 对象")
        self.path = Path(path) if path else None
        self._environ = environ if environ is not None else os.environ
        self._lock = threading.RLock()
        self._stamp = self._stat_stamp()
        self.vault = None
        self.secret_errors = {}
        self.doc = copy.deepcopy(doc)
        self._apply()

    # ── 加载 ────────────────────────────────────────────────
    @classmethod
    def load(cls, path, environ=None) -> "Config":
        path = Path(path)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError("配置文件不存在: %s" % path) from exc
        except ValueError as exc:
            raise ConfigError("配置文件不是合法 JSON: %s (%s)" % (path, exc)) from exc
        return cls(doc, path=path, environ=environ)

    def _stat_stamp(self):
        if not self.path:
            return (0, 0)
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (0, 0)

    def changed_on_disk(self) -> bool:
        return self.path is not None and self._stat_stamp() != self._stamp

    def reload(self) -> "Config":
        if not self.path:
            return self
        fresh = Config.load(self.path, environ=self._environ)
        with self._lock:
            self.doc = fresh.doc
            self._stamp = fresh._stamp
            self._apply()
        return self

    def attach_vault(self, vault) -> "Config":
        """挂载渠道密钥仓；挂载后重新解析 token，不把明文写回配置。"""
        with self._lock:
            self.vault = vault
            self._apply()
        return self

    # ── 解析 ────────────────────────────────────────────────
    def _apply(self) -> None:
        doc = self.doc
        self.secret_errors = {}
        server = doc.get("server") if isinstance(doc.get("server"), dict) else {}

        port = _as_int(self._environ.get("KWSA_LITE_PORT") or server.get("port"), DEFAULT_PORT)
        if not _valid_port(port):
            raise ConfigError(
                "端口 %s 不允许：必须位于 1024-65535，且不得占用 8792/8795/8791" % port
            )
        host = str(self._environ.get("KWSA_LITE_HOST") or server.get("host") or "127.0.0.1").strip()
        self.server = {
            "host": host,
            "port": port,
            "max_body_bytes": _as_int(
                self._environ.get("KWSA_LITE_MAX_BODY") or server.get("max_body_bytes"),
                DEFAULT_MAX_BODY,
            ),
            "timeout_s": _as_float(
                self._environ.get("KWSA_LITE_TIMEOUT") or server.get("timeout_s"), 180.0
            ),
        }

        raw_auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
        auth_enabled = _as_bool(
            self._environ.get("KWSA_LITE_AUTH"), _as_bool(raw_auth.get("enabled"), True)
        )
        self.auth = {
            "enabled": auth_enabled,
            "username": str(
                self._environ.get("KWSA_LITE_AUTH_USER")
                or raw_auth.get("username") or "admin"
            ).strip() or "admin",
            "session_ttl_s": max(300, _as_int(raw_auth.get("session_ttl_s"), 12 * 3600)),
            "cookie_secure": _as_bool(
                self._environ.get("KWSA_LITE_COOKIE_SECURE"),
                _as_bool(raw_auth.get("cookie_secure"), False),
            ),
            "login_max_failures": max(1, _as_int(raw_auth.get("login_max_failures"), 5)),
            "login_window_s": max(30, _as_int(raw_auth.get("login_window_s"), 300)),
            "trust_proxy": _as_bool(
                self._environ.get("KWSA_LITE_TRUST_PROXY"),
                _as_bool(raw_auth.get("trust_proxy"), False),
            ),
        }
        if not auth_enabled and not is_loopback_host(host):
            raise ConfigError(
                "监听 %s 不是回环地址，必须启用 auth.enabled；"
                "否则任何人都能读面板和调用控制面" % host
            )
        if self.auth["trust_proxy"] and not is_loopback_host(host):
            raise ConfigError(
                "监听 %s 不是回环地址，禁止 auth.trust_proxy；"
                "只有本机 TLS 反代才能信任 X-Forwarded-For" % host
            )

        raw_channels = doc.get("channels")
        if not isinstance(raw_channels, dict) or not raw_channels:
            raise ConfigError("channels 至少需要一个渠道")
        channels = {}
        for name, entry in raw_channels.items():
            channel = str(name or "").strip()
            if not channel or not isinstance(entry, dict):
                continue
            if str(entry.get("token") or "").strip():
                raise ConfigError(
                    "渠道 %s 出现明文 token；请改用 token_env 指向环境变量" % channel
                )
            base = str(entry.get("base_url") or "").strip().rstrip("/")
            if not base.startswith(ALLOWED_PROTOCOLS):
                raise ConfigError("渠道 %s 的 base_url 必须是 http(s) 绝对地址" % channel)
            models = _str_list(entry.get("models"))
            aliases = entry.get("aliases") if isinstance(entry.get("aliases"), dict) else {}
            headers, header_env = {}, {}
            raw_headers = entry.get("headers")
            if isinstance(raw_headers, dict):
                for hk, hv in raw_headers.items():
                    hkey = str(hk).strip()
                    if not hkey:
                        continue
                    hval = str(hv)
                    if hval.startswith("env:"):
                        env_name = hval[4:].strip()
                        header_env[hkey] = env_name
                        hval = str(self._environ.get(env_name) or "").strip()
                        if not hval:
                            continue
                    headers[hkey] = hval
            token_env = str(entry.get("token_env") or "").strip()
            token = str(self._environ.get(token_env) or "").strip() if token_env else ""
            token_source = "env" if token else "none"
            if not token and self.vault is not None:
                try:
                    secret = self.vault.get(channel)
                except VaultError as exc:
                    self.secret_errors[channel] = str(exc)
                    secret = ""
                if secret:
                    token = secret
                    token_source = "vault"
            login_url = str(entry.get("login_url") or "").strip()
            if login_url and not login_url.startswith(ALLOWED_PROTOCOLS):
                raise ConfigError("渠道 %s 的 login_url 必须是 http(s) 绝对地址" % channel)
            usage_path = str(entry.get("usage_path") or "").strip()
            if usage_path and not usage_path.startswith("/"):
                raise ConfigError("渠道 %s 的 usage_path 必须以 / 开头" % channel)
            login_user, login_password, login_error = "", "", ""
            if self.vault is not None:
                try:
                    raw_login = self.vault.get("login:" + channel)
                except VaultError as exc:
                    login_error = str(exc)
                    raw_login = ""
                if raw_login:
                    try:
                        blob = json.loads(raw_login)
                    except ValueError:
                        blob = None
                    if isinstance(blob, dict):
                        login_user = str(blob.get("username") or "").strip()
                        login_password = str(blob.get("password") or "")
                    else:
                        login_error = "登录凭据条目格式异常"
            endpoint = str(entry.get("endpoint") or "").strip()
            channels[channel] = {
                "name": channel,
                "base_url": base,
                "token_env": token_env,
                "token": token,
                "token_source": token_source,
                "login_url": login_url,
                "usage_path": usage_path,
                "login_user": login_user,
                "login_password": login_password,
                "login_present": bool(login_user and login_password),
                "login_source": "vault" if (login_user or login_password) else "none",
                "login_error": login_error,
                "models": models,
                "aliases": {str(k).strip(): str(v).strip()
                            for k, v in aliases.items() if str(k).strip() and str(v).strip()},
                "headers": headers,
                "header_env": header_env,
                "endpoint": endpoint,
                "decision": bool(entry.get("decision")) or endpoint.endswith("/systemone"),
                "pricing_channel": str(entry.get("pricing_channel") or channel).strip() or channel,
                "probe_path": str(entry.get("probe_path") or "/health").strip() or "/health",
                "egress": str(entry.get("egress") or "").strip(),
                "virtual": "",
                "gate": "",
            }

        # ── 出口分路（代理腿）────────────────────────────────
        raw_egress = doc.get("egress") if isinstance(doc.get("egress"), dict) else {}
        legs = []
        leg_ids = set()
        for item in (raw_egress.get("legs") or []):
            if not isinstance(item, dict):
                continue
            leg_id = str(item.get("id") or "").strip()
            if not leg_id or leg_id == "direct" or leg_id in leg_ids:
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if not url.startswith(ALLOWED_PROTOCOLS):
                raise ConfigError(
                    "出口腿 %s 只接受 http:// 或 https:// 代理地址" % leg_id)
            leg_ids.add(leg_id)
            legs.append({
                "id": leg_id,
                "name": str(item.get("name") or leg_id).strip() or leg_id,
                "url": url,
                "enabled": _as_bool(item.get("enabled"), True),
                "note": str(item.get("note") or "").strip(),
            })
        default_leg = str(raw_egress.get("default") or "").strip()
        if default_leg and default_leg not in leg_ids:
            default_leg = ""
        self.egress = {
            "enabled": _as_bool(raw_egress.get("enabled"), True),
            "default": default_leg,
            "legs": legs,
        }
        self.egress_warnings = {}
        for name, entry in channels.items():
            leg = entry.get("egress") or ""
            if leg and leg not in leg_ids:
                self.egress_warnings[name] = "出口腿 %s 不存在或已删除，按直连" % leg
                entry["egress"] = ""

        # ── OC 免费档桥（可选开启，虚拟渠道）─────────────────
        raw_oc = doc.get("oc_bridge") if isinstance(doc.get("oc_bridge"), dict) else {}
        oc_enabled = _as_bool(
            self._environ.get("KWSA_LITE_OC_BRIDGE"), _as_bool(raw_oc.get("enabled"), False))
        oc_mode = str(raw_oc.get("mode") or "bridge").strip().lower()
        if oc_mode not in ("bridge", "direct"):
            raise ConfigError("oc_bridge.mode 只能是 bridge（打外部桥）或 direct（自补门禁）")
        oc_channel = str(raw_oc.get("channel") or "oc-bridge").strip()
        if not CHANNEL_NAME_RE.match(oc_channel):
            raise ConfigError("oc_bridge.channel 不是合法渠道代号")
        oc_bridge_url = str(raw_oc.get("bridge_url") or "http://127.0.0.1:8898/v1").strip().rstrip("/")
        oc_upstream = str(raw_oc.get("upstream") or "https://opencode.ai/inference/v1").strip().rstrip("/")
        if not oc_upstream.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("oc_bridge.upstream 必须是 http(s) 绝对地址")
        if oc_mode == "bridge" and not oc_bridge_url.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("oc_bridge.bridge_url 必须是 http(s) 绝对地址")
        oc_token_env = str(raw_oc.get("token_env") or "OC_BRIDGE_TOKEN").strip()
        oc_token = str(self._environ.get(oc_token_env) or "").strip() if oc_token_env else ""
        oc_token_source = "env" if oc_token else "none"
        if not oc_token and self.vault is not None:
            try:
                secret = self.vault.get("oc_bridge")
            except VaultError as exc:
                self.secret_errors[oc_channel] = str(exc)
                secret = ""
            if secret:
                oc_token, oc_token_source = secret, "vault"
        self.oc_bridge = {
            "enabled": oc_enabled,
            "mode": oc_mode,
            "channel": oc_channel,
            "bridge_url": oc_bridge_url,
            "upstream": oc_upstream,
            "user_agent": str(raw_oc.get("user_agent") or "").strip(),
            "models": _str_list(raw_oc.get("models")),
            "token": oc_token,
            "token_env": oc_token_env,
            "token_source": oc_token_source,
            "egress": str(raw_oc.get("egress") or "").strip(),
        }
        if oc_enabled:
            virtual_models = list(self.oc_bridge["models"])
            if oc_mode == "bridge":
                virtual_base = oc_bridge_url
                virtual_token = oc_token
                virtual_probe = "/v1/models"
                gate = ""
            else:
                virtual_base = oc_upstream
                virtual_token = "public"
                virtual_probe = "/v1/models"
                gate = "oc-direct"
            channels[oc_channel] = {
                "name": oc_channel,
                "base_url": virtual_base,
                "token_env": self.oc_bridge["token_env"],
                "token": virtual_token,
                "token_source": ("public" if oc_mode == "direct"
                                 else self.oc_bridge["token_source"]),
                "login_url": "", "usage_path": "", "login_user": "",
                "login_password": "", "login_present": False, "login_source": "none",
                "login_error": "",
                "models": virtual_models,
                "aliases": {},
                "headers": {},
                "header_env": {},
                "endpoint": "",
                "decision": False,
                "pricing_channel": oc_channel,
                "probe_path": virtual_probe,
                "egress": self.oc_bridge["egress"],
                "virtual": "oc",
                "gate": gate,
            }

        default_channel = str(doc.get("default_channel") or "").strip()
        if not default_channel:
            default_channel = next(iter(channels))
        if default_channel not in channels:
            if default_channel == oc_channel:
                # OC 桥关掉后默认渠道指向它：回落而不是让整份配置起不来。
                default_channel = next(iter(channels))
            else:
                raise ConfigError("default_channel 指向未定义渠道: %s" % default_channel)

        groups = {}
        raw_groups = doc.get("groups")
        if isinstance(raw_groups, dict):
            for gid, group in raw_groups.items():
                group_id = str(gid or "").strip()
                if not group_id or not isinstance(group, dict):
                    continue
                members = []
                for item in (group.get("members") or []):
                    if not isinstance(item, dict):
                        continue
                    model = str(item.get("id") or item.get("upstream_name") or "").strip()
                    if not model:
                        continue
                    members.append({
                        "id": model,
                        "channel": str(item.get("channel") or "").strip(),
                        "upstream_name": str(item.get("upstream_name") or "").strip(),
                        "tier": str(item.get("tier") or "").strip(),
                        "priority": _as_int(item.get("priority"), 100),
                        "enabled": _as_bool(item.get("enabled"), True),
                    })
                members.sort(key=lambda m: (m["priority"], m["id"]))
                groups[group_id] = {
                    "id": group_id,
                    "name": str(group.get("name") or group_id),
                    "policy": dict(group.get("policy") or {}),
                    "members": members,
                }

        raw_control = doc.get("control") if isinstance(doc.get("control"), dict) else {}
        raw_ctl_channels = raw_control.get("channels") if isinstance(raw_control.get("channels"), dict) else {}
        raw_ctl_models = raw_control.get("models") if isinstance(raw_control.get("models"), dict) else {}
        control = {
            "channels": {
                str(k): _control_state(v, True)
                for k, v in raw_ctl_channels.items() if str(k)
            },
            "models": {
                str(ch): {str(m): _control_state(state, True) for m, state in (rows or {}).items()}
                for ch, rows in raw_ctl_models.items() if str(ch) and isinstance(rows, dict)
            },
        }

        pricing = doc.get("pricing") if isinstance(doc.get("pricing"), dict) else {}
        self.pricing = {
            "unit": str(pricing.get("unit") or "USD_per_mtok"),
            "usd_cny": _as_float(pricing.get("usd_cny"), 7.2),
            "channels": pricing.get("channels") if isinstance(pricing.get("channels"), dict) else {},
            "note": str(pricing.get("note") or pricing.get("$note") or "").strip(),
        }

        ledger = doc.get("ledger") if isinstance(doc.get("ledger"), dict) else {}
        self.ledger = {
            "path": _resolve_db_path(
                self._environ.get("KWSA_LITE_LEDGER_DB") or ledger.get("path")
            ),
            "keep_days": max(1, _as_int(ledger.get("keep_days"), 7)),
        }

        rate = doc.get("rate_limit") if isinstance(doc.get("rate_limit"), dict) else {}
        self.rate_limit = {
            "enabled": _as_bool(self._environ.get("KWSA_LITE_RATE_LIMIT"), _as_bool(rate.get("enabled"), True)),
            "rps": _as_float(self._environ.get("KWSA_LITE_RPS") or rate.get("rps"), 100.0),
            "burst": _as_float(self._environ.get("KWSA_LITE_BURST") or rate.get("burst"), 150.0),
        }
        if self.rate_limit["rps"] <= 0:
            raise ConfigError("rate_limit.rps 必须大于 0")

        data_plane = (doc.get("data_plane")
                      if isinstance(doc.get("data_plane"), dict) else {})
        self.data_plane = {
            "require_key": _as_bool(
                self._environ.get("KWSA_LITE_REQUIRE_KEY"),
                _as_bool(data_plane.get("require_key"), True),
            ),
            "keys_path": _resolve_db_path(
                self._environ.get("KWSA_LITE_KEYS_FILE")
                or data_plane.get("keys_path")
                or "运维区/data/kwsa_lite_client_keys.json"
            ),
            "default_rps": _as_float(data_plane.get("default_rps"), 20.0),
            "default_burst": _as_float(data_plane.get("default_burst"), 40.0),
        }
        if not (0.1 <= self.data_plane["default_rps"] <= 10000):
            raise ConfigError("data_plane.default_rps 必须在 0.1-10000 之间")
        if not (0.1 <= self.data_plane["default_burst"] <= 10000):
            raise ConfigError("data_plane.default_burst 必须在 0.1-10000 之间")

        raw_audit = doc.get("audit") if isinstance(doc.get("audit"), dict) else {}
        self.audit = {
            "path": _resolve_db_path(
                self._environ.get("KWSA_LITE_AUDIT_FILE")
                or raw_audit.get("path")
                or "运维区/data/kwsa_lite_audit.jsonl"
            ),
            "max_bytes": max(64 * 1024, _as_int(raw_audit.get("max_bytes"), 2 * 1024 * 1024)),
        }

        routing = doc.get("routing") if isinstance(doc.get("routing"), dict) else {}
        self.routing = {
            "timeout_s": _as_float(routing.get("timeout_s"), self.server["timeout_s"]),
            "task_lock_ttl_s": max(1, _as_int(routing.get("task_lock_ttl_s"), 43200)),
        }

        probe = doc.get("probe") if isinstance(doc.get("probe"), dict) else {}
        self.probe = {
            "enabled": _as_bool(self._environ.get("KWSA_LITE_PROBE"), _as_bool(probe.get("enabled"), False)),
            "interval_s": max(10, _as_int(probe.get("interval_s"), 60)),
            "timeout_s": max(1, _as_int(probe.get("timeout_s"), 4)),
        }

        self.channels = channels
        self.default_channel = default_channel
        self.groups = groups
        self.control = control

    # ── 查询 ────────────────────────────────────────────────
    def channel(self, name: str) -> dict:
        return self.channels.get(str(name or "").strip(), {})

    def channel_enabled(self, name: str) -> bool:
        entry = self.control["channels"].get(str(name or ""), {})
        return bool(entry.get("enabled", True))

    def model_enabled(self, channel: str, model: str) -> bool:
        if not self.channel_enabled(channel):
            return False
        rows = self.control["models"].get(channel) or {}
        row = rows.get(model)
        if isinstance(row, dict):
            return bool(row.get("enabled", True))
        return row is not False

    def channel_state(self, name: str) -> dict:
        """渠道启停 + 为什么停 + 怎么恢复；面板与 /api/catalog 共用这一份口径。"""
        name = str(name or "")
        state = dict(self.control["channels"].get(name) or {"enabled": True})
        state.setdefault("enabled", True)
        state["scope"] = "channel"
        state["channel"] = name
        state["label"] = "" if state["enabled"] else unavailable_label(state.get("code") or "manual")
        return state

    def model_state(self, channel: str, model: str) -> dict:
        channel = str(channel or "")
        model = str(model or "")
        if not self.channel_enabled(channel):
            return self.channel_state(channel)
        rows = self.control["models"].get(channel) or {}
        state = dict(rows.get(model) or {"enabled": True})
        state.setdefault("enabled", True)
        state["scope"] = "model"
        state["channel"] = channel
        state["model"] = model
        state["label"] = "" if state["enabled"] else unavailable_label(state.get("code") or "manual")
        return state

    def public(self) -> dict:
        """脱敏配置快照：绝不返回 token 与自定义头原文。"""
        return {
            "default_channel": self.default_channel,
            "channels": [
                {
                    "channel": name,
                    "base_url": entry["base_url"],
                    "models": list(entry["models"]),
                    "endpoint": entry["endpoint"] or "/v1/chat/completions",
                    "decision": entry["decision"],
                    "pricing_channel": entry["pricing_channel"],
                    "token_env": entry["token_env"],
                    "token_present": bool(entry["token"]),
                    "token_source": entry.get("token_source") or "none",
                    "token_masked": mask_token(entry["token"]),
                    "secret_error": self.secret_errors.get(name, ""),
                    "login_url": entry.get("login_url") or "",
                    "usage_path": entry.get("usage_path") or "",
                    "login_present": bool(entry.get("login_present")),
                    "login_user_masked": _mask_user(entry.get("login_user") or ""),
                    "password_present": bool(entry.get("login_password")),
                    "login_source": entry.get("login_source") or "none",
                    "login_error": entry.get("login_error") or "",
                    "enabled": self.channel_enabled(name),
                    "aliases": dict(entry["aliases"]),
                    "probe_path": entry["probe_path"],
                    "headers": {k: "●●●●●●" for k in entry["headers"]},
                    "header_env": dict(entry["header_env"]),
                    "egress": entry.get("egress") or "",
                    "virtual": entry.get("virtual") or "",
                    "gate": entry.get("gate") or "",
                }
                for name, entry in self.channels.items()
            ],
            "groups": [
                {"id": g["id"], "name": g["name"], "policy": dict(g["policy"]),
                 "members": [dict(m) for m in g["members"]]}
                for g in self.groups.values()
            ],
            "control": copy.deepcopy(self.control),
            "pricing": {
                "unit": self.pricing["unit"],
                "usd_cny": self.pricing["usd_cny"],
                "models": self._pricing_model_count(),
            },
            "ledger": {"backend": "sqlite", "path": str(self.ledger["path"]),
                       "keep_days": self.ledger["keep_days"]},
            "rate_limit": dict(self.rate_limit),
            "data_plane": {
                "require_key": self.data_plane["require_key"],
                "keys_path": str(self.data_plane["keys_path"]),
                "default_rps": self.data_plane["default_rps"],
                "default_burst": self.data_plane["default_burst"],
            },
            "routing": dict(self.routing),
            "egress": copy.deepcopy(self.egress),
            "egress_warnings": dict(self.egress_warnings),
            "oc_bridge": {
                **{k: v for k, v in self.oc_bridge.items() if k != "token"},
                "token_present": bool(self.oc_bridge["token"]),
            },
            "probe": dict(self.probe),
            "auth": {
                "enabled": self.auth["enabled"],
                "username": self.auth["username"],
                "session_ttl_s": self.auth["session_ttl_s"],
                "cookie_secure": self.auth["cookie_secure"],
                "trust_proxy": self.auth["trust_proxy"],
            },
        }

    def _pricing_model_count(self) -> int:
        count = 0
        for rows in (self.pricing.get("channels") or {}).values():
            if isinstance(rows, dict):
                count += len(rows)
        return count

    # ── 控制面写入 ──────────────────────────────────────────
    @staticmethod
    def _stamp_unavailable(row: dict, enabled: bool, code: str = "", reason: str = "",
                           evidence: str = "", fix: str = "") -> None:
        """启用即清空解释；停用则写下原因码与恢复路径，避免「关了但不知道为什么」。"""
        if enabled:
            for key in ("code", "reason", "evidence", "fix", "since", "tested_at"):
                row.pop(key, None)
            return
        row["code"] = str(code or "manual").strip()[:48] or "manual"
        row["reason"] = str(reason or "").strip()[:240] or "面板手动停用"
        row["fix"] = str(fix or "").strip()[:240]
        row["since"] = _as_int(row.get("since"), 0) or int(time.time())
        text = str(evidence or "").strip()[:240]
        if text:
            row["evidence"] = text
            row["tested_at"] = int(time.time())
        else:
            row.pop("tested_at", None)

    def set_channel_enabled(self, name: str, enabled: bool, *, code: str = "",
                            reason: str = "", evidence: str = "", fix: str = "") -> dict:
        if name not in self.channels:
            raise ConfigError("未知渠道: %s" % name)
        self._reject_virtual(name)
        with self._lock:
            row = self.doc.setdefault("control", {}).setdefault("channels", {}).setdefault(name, {})
            row["enabled"] = bool(enabled)
            self._stamp_unavailable(row, bool(enabled), code, reason, evidence, fix)
            self._apply()
            return {"channel": name, **self.channel_state(name)}

    def set_model_enabled(self, channel: str, model: str, enabled: bool, *, code: str = "",
                          reason: str = "", evidence: str = "", fix: str = "") -> dict:
        if channel not in self.channels:
            raise ConfigError("未知渠道: %s" % channel)
        if not model:
            raise ConfigError("model 不能为空")
        with self._lock:
            rows = self.doc.setdefault("control", {}).setdefault("models", {}).setdefault(channel, {})
            row = rows.get(model) if isinstance(rows.get(model), dict) else {}
            row["enabled"] = bool(enabled)
            self._stamp_unavailable(row, bool(enabled), code, reason, evidence, fix)
            if row.get("enabled"):
                rows.pop(model, None)          # 启用后不留 {"enabled": true} 垃圾行
            else:
                rows[model] = row
            self._apply()
            return {"channel": channel, "model": model, **self.model_state(channel, model)}

    def annotate_model(self, channel: str, model: str, code: str, reason: str = "",
                       evidence: str = "", fix: str = "") -> dict:
        """只刷新「为什么不可用」的实测证据，不动启停；已启用的模型不写注释。"""
        if channel not in self.channels:
            raise ConfigError("未知渠道: %s" % channel)
        if not model:
            raise ConfigError("model 不能为空")
        with self._lock:
            if self.model_state(channel, model)["enabled"]:
                return self.model_state(channel, model)
            rows = self.doc.setdefault("control", {}).setdefault("models", {}).setdefault(channel, {})
            row = rows.get(model) if isinstance(rows.get(model), dict) else {}
            row["enabled"] = False
            self._stamp_unavailable(row, False, code, reason, evidence, fix)
            rows[model] = row
            self._apply()
            return self.model_state(channel, model)

    def annotate_channel(self, name: str, code: str, reason: str = "",
                         evidence: str = "", fix: str = "") -> dict:
        """渠道级解释：仅用于已经停用的渠道，不顺手改启停状态。"""
        if name not in self.channels:
            raise ConfigError("未知渠道: %s" % name)
        with self._lock:
            if self.channel_state(name)["enabled"]:
                return self.channel_state(name)
            row = self.doc.setdefault("control", {}).setdefault("channels", {}).setdefault(name, {})
            row["enabled"] = False
            self._stamp_unavailable(row, False, code, reason, evidence, fix)
            self._apply()
            return self.channel_state(name)

    # ── 渠道收录 ────────────────────────────────────────────
    @staticmethod
    def _channel_name(name) -> str:
        value = str(name or "").strip()
        if not CHANNEL_NAME_RE.match(value):
            raise ConfigError("渠道代号只允许字母、数字、点、下划线、连字符，且以字母或数字开头（≤64 字符）")
        return value

    def _channel_row(self, name: str) -> dict:
        for row in self.public()["channels"]:
            if row["channel"] == name:
                return row
        return {}

    def _reject_virtual(self, name: str) -> None:
        entry = self.channels.get(str(name or ""))
        if isinstance(entry, dict) and entry.get("virtual"):
            raise ConfigError("渠道 %s 是 OC 桥虚拟渠道，请到 OC 桥版块改或关" % name)

    def upsert_channel(self, data: dict) -> dict:
        """新增或编辑渠道；token 只进密钥仓，配置文件中不落明文。"""
        if not isinstance(data, dict):
            raise ConfigError("渠道参数必须是 JSON 对象")
        name = self._channel_name(data.get("name"))
        self._reject_virtual(name)
        base = str(data.get("base_url") or "").strip().rstrip("/")
        if not base.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("上游地址必须是 http(s) 绝对地址")
        existing_entry = self.doc.get("channels", {}).get(name)
        existing_entry = existing_entry if isinstance(existing_entry, dict) else {}
        models = _str_list(data.get("models", existing_entry.get("models")))
        aliases = {}
        raw_aliases = data.get("aliases", existing_entry.get("aliases"))
        if isinstance(raw_aliases, dict):
            aliases = {str(k).strip(): str(v).strip()
                       for k, v in raw_aliases.items()
                       if str(k).strip() and str(v).strip()}
        endpoint = str(data.get("endpoint", existing_entry.get("endpoint")) or "").strip()
        if endpoint and not endpoint.startswith("/"):
            raise ConfigError("端点路径必须以 / 开头")
        probe_path = str(data.get("probe_path", existing_entry.get("probe_path")) or "/health").strip() or "/health"
        if not probe_path.startswith("/"):
            raise ConfigError("探针路径必须以 / 开头")
        # 保留历史非 env 自定义头；本次只增删 env:VAR 形式的头。
        raw_headers = data.get("headers")
        if raw_headers is None:
            headers = dict(existing_entry.get("headers") or {})
        else:
            headers = {k: v for k, v in (existing_entry.get("headers") or {}).items()
                       if not str(v).startswith("env:")}
        if isinstance(raw_headers, dict):
            for hk, hv in raw_headers.items():
                hkey = str(hk or "").strip()
                hval = str(hv or "").strip()
                if not hkey:
                    continue
                if hkey.lower() in RESERVED_HEADERS:
                    raise ConfigError("自定义头不能覆盖保留头: %s" % hkey)
                if not hval.startswith("env:") or not hval[4:].strip():
                    raise ConfigError("自定义头 %s 只接受 env:VAR，避免密钥明文落盘" % hkey)
                headers[hkey] = hval
        token_env = str(data.get("token_env") or "").strip() if "token_env" in data else str(existing_entry.get("token_env") or "").strip()
        raw_token = data.get("token")
        token = str(raw_token).strip() if raw_token is not None else ""
        if token in ("●●●●●●", "********"):
            token = ""
        if len(token) > 4096:
            raise ConfigError("密钥长度异常（>4096 字符）")
        decision = (bool(data.get("decision")) if "decision" in data
                    else bool(existing_entry.get("decision"))) or endpoint.endswith("/systemone")
        pricing_channel = str(data.get("pricing_channel", existing_entry.get("pricing_channel")) or name).strip() or name
        login_url = str(data.get("login_url", existing_entry.get("login_url")) or "").strip()
        if login_url and not login_url.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("官方登录地址必须是 http(s) 绝对地址")
        usage_path = str(data.get("usage_path", existing_entry.get("usage_path")) or "").strip()
        if usage_path and not usage_path.startswith("/"):
            raise ConfigError("官方账单路径必须以 / 开头")

        existing = name in self.channels
        old_token = ""
        old_login = ""
        existing_login = {}
        if existing and self.vault is not None:
            try:
                old_token = self.vault.get(name)
            except VaultError:
                old_token = ""
            try:
                old_login = self.vault.get("login:" + name)
                parsed = json.loads(old_login) if old_login else {}
                existing_login = parsed if isinstance(parsed, dict) else {}
            except (VaultError, ValueError):
                old_login, existing_login = "", {}

        if token and self.vault is None:
            raise ConfigError("密钥仓未挂载，无法保存渠道密钥")
        if token:
            self.vault.set(name, token)

        login_action = "keep"
        login_secret = None
        if _as_bool(data.get("clear_login"), False):
            login_action = "delete"
        else:
            raw_user = data.get("login_user")
            raw_pass = data.get("login_password")
            password = "" if raw_pass is None else str(raw_pass).strip()
            if password in ("●●●●●●", "********"):
                password = ""
            if password:
                username = str(raw_user if raw_user is not None else
                               existing_login.get("username") or "").strip()
                if not username:
                    raise ConfigError("保存登录密码时必须同时提供登录账号")
                login_action = "set"
                login_secret = {"username": username, "password": password}
            elif raw_user is not None and str(raw_user).strip():
                username = str(raw_user).strip()
                if existing_login.get("password"):
                    login_action = "set"
                    login_secret = {"username": username,
                                    "password": str(existing_login.get("password"))}
        if login_action == "set":
            if self.vault is None:
                raise ConfigError("密钥仓未挂载，无法保存官方登录凭据")
            self.vault.set("login:" + name, json.dumps(login_secret, ensure_ascii=False))
        elif login_action == "delete" and self.vault is not None:
            self.vault.delete("login:" + name)

        new_doc = copy.deepcopy(self.doc)
        channels_doc = new_doc.setdefault("channels", {})
        entry = channels_doc.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry.update({
            "base_url": base,
            "models": models,
            "aliases": aliases,
            "headers": headers,
            "endpoint": endpoint,
            "decision": decision,
            "pricing_channel": pricing_channel,
            "probe_path": probe_path,
            "login_url": login_url,
            "usage_path": usage_path,
        })
        if token_env:
            entry["token_env"] = token_env
        elif "token_env" in data:
            entry.pop("token_env", None)
        entry.pop("token", None)
        channels_doc[name] = entry

        control_doc = new_doc.setdefault("control", {}).setdefault("channels", {})
        if existing:
            if "enabled" in data:
                control_doc.setdefault(name, {})["enabled"] = bool(data.get("enabled"))
            else:
                control_doc.setdefault(name, {}).setdefault("enabled", True)
        else:
            control_doc[name] = {"enabled": bool(data.get("enabled", True))}

        if not new_doc.get("default_channel") or data.get("make_default"):
            new_doc["default_channel"] = name

        old_doc = self.doc
        self.doc = new_doc
        try:
            self._apply()
        except Exception:
            self.doc = old_doc
            self._apply()
            if self.vault is not None and token:
                if old_token:
                    self.vault.set(name, old_token)
                else:
                    self.vault.delete(name)
            if self.vault is not None and login_action != "keep":
                if old_login:
                    self.vault.set("login:" + name, old_login)
                else:
                    self.vault.delete("login:" + name)
            raise

        return self._channel_row(name)

    def delete_channel(self, name: str) -> dict:
        name = self._channel_name(name)
        if name not in self.channels:
            raise ConfigError("未知渠道: %s" % name)
        self._reject_virtual(name)
        remaining = [key for key in self.channels if key != name]
        if not remaining:
            raise ConfigError("至少保留一个渠道；不能删除最后一个")
        new_doc = copy.deepcopy(self.doc)
        new_doc.get("channels", {}).pop(name, None)
        new_doc.setdefault("control", {}).get("channels", {}).pop(name, None)
        new_doc.setdefault("control", {}).get("models", {}).pop(name, None)
        pricing = new_doc.get("pricing", {}).get("channels", {})
        if isinstance(pricing, dict):
            pricing.pop(name, None)
        groups = new_doc.get("groups", {})
        if isinstance(groups, dict):
            for gid in list(groups):
                group = groups.get(gid)
                if not isinstance(group, dict):
                    continue
                members = [m for m in (group.get("members") or [])
                           if isinstance(m, dict) and str(m.get("channel") or "") != name]
                if members:
                    group["members"] = members
                else:
                    groups.pop(gid, None)
        if new_doc.get("default_channel") == name:
            new_doc["default_channel"] = remaining[0]
        old_doc = self.doc
        self.doc = new_doc
        try:
            self._apply()
        except Exception:
            self.doc = old_doc
            self._apply()
            raise
        token_deleted = False
        login_deleted = False
        if self.vault is not None:
            token_deleted = self.vault.delete(name)
            login_deleted = self.vault.delete("login:" + name)
        return {"channel": name, "deleted": True, "token_deleted": token_deleted,
                "login_deleted": login_deleted, "default_channel": self.default_channel}

    def set_default_channel(self, name: str) -> dict:
        name = self._channel_name(name)
        if name not in self.channels:
            raise ConfigError("未知渠道: %s" % name)
        self.doc["default_channel"] = name
        self._apply()
        return {"default_channel": name}

    def forget_channel_token(self, name: str) -> dict:
        name = self._channel_name(name)
        if name not in self.channels:
            raise ConfigError("未知渠道: %s" % name)
        deleted = self.vault.delete(name) if self.vault is not None else False
        self._apply()
        return {"channel": name, "token_deleted": deleted,
                "token_source": self.channels[name].get("token_source", "none")}

    # ── 价目：峰谷单价 ──────────────────────────────────────
    @staticmethod
    def _rate(value, field: str):
        if value in (None, ""):
            return None
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise ConfigError("价目 %s 必须是数字" % field)
        if rate < 0:
            raise ConfigError("价目 %s 不能为负数" % field)
        return round(rate, 6)

    @staticmethod
    def _pricing_block(doc: dict, channel: str) -> dict:
        pricing = doc.setdefault("pricing", {})
        pricing.setdefault("unit", "USD_per_mtok")
        block = pricing.setdefault("channels", {}).setdefault(channel, {})
        if not isinstance(block, dict):
            block = {}
            pricing["channels"][channel] = block
        if not isinstance(block.get("models"), dict):
            # 旧扁平表转块：原有单价搬到 models 下，peak_rule 保留。
            flat = {k: v for k, v in block.items()
                    if isinstance(v, dict) and any(x in v for x in ("in", "cache", "out"))}
            rule = block.get("peak_rule")
            block.clear()
            block["models"] = flat
            if rule:
                block["peak_rule"] = rule
        return block

    def set_price(self, data: dict) -> dict:
        """写入一个模型的峰/谷单价；只改价目，不动渠道。"""
        channel = str(data.get("channel") or "").strip()
        if not CHANNEL_NAME_RE.match(channel):
            raise ConfigError("价目渠道键不合法（字母数字 . _ -，≤64）")
        model = str(data.get("model") or "").strip()
        if not model:
            raise ConfigError("价目必须指定模型")
        row = {}
        for key in ("in", "cache", "out"):
            value = self._rate(data.get(key), key)
            if value is None:
                raise ConfigError("价目缺少 %s 单价" % key)
            row[key] = value
        for key in ("in", "cache", "out"):
            off = self._rate(data.get("off_" + key), "off_" + key)
            if off is not None:
                row["off_" + key] = off
        note = str(data.get("note") or "").strip()
        if note:
            row["note"] = note[:120]
        with self._lock:
            block = self._pricing_block(self.doc, channel)
            block["models"][model] = row
            self._apply()
        return {"channel": channel, "model": model, "row": row,
                "unit": self.pricing.get("unit")}

    def delete_price(self, channel: str, model: str) -> dict:
        channel = str(channel or "").strip()
        model = str(model or "").strip()
        if not channel or not model:
            raise ConfigError("删除价目需要 channel 与 model")
        with self._lock:
            block = (self.doc.get("pricing", {}).get("channels", {}) or {}).get(channel)
            models = block.get("models") if isinstance(block, dict) else None
            if not isinstance(models, dict) or model not in models:
                raise ConfigError("价目不存在: %s / %s" % (channel, model))
            models.pop(model)
            self._apply()
        return {"channel": channel, "model": model, "deleted": True}

    def set_peak_rule(self, channel: str, rule) -> dict:
        """设置峰谷窗口；rule=None 或 kind=flat 表示只打单一价。"""
        channel = str(channel or "").strip()
        if not CHANNEL_NAME_RE.match(channel):
            raise ConfigError("价目渠道键不合法")
        norm = None
        if isinstance(rule, dict) and str(rule.get("kind") or "") != "flat":
            kind = str(rule.get("kind") or "utc_weekday_window")
            if kind == "utc_weekday_window":
                start = _as_int(rule.get("start_hour"), 12)
                end = _as_int(rule.get("end_hour"), 18)
                if not (0 <= start < end <= 24):
                    raise ConfigError("峰窗必须满足 0 ≤ start < end ≤ 24")
                norm = {"kind": kind, "start_hour": start, "end_hour": end}
            elif kind == "utc_weekday_windows":
                windows = []
                for item in (rule.get("windows") or []):
                    try:
                        start, end = int(item[0]), int(item[1])
                    except (TypeError, ValueError, IndexError):
                        raise ConfigError("windows 每项必须是 [start, end]")
                    if not (0 <= start < end <= 24):
                        raise ConfigError("峰窗必须满足 0 ≤ start < end ≤ 24")
                    windows.append([start, end])
                if not windows:
                    raise ConfigError("utc_weekday_windows 至少需要一个窗口")
                norm = {"kind": kind, "windows": windows}
            else:
                raise ConfigError("不支持的峰谷规则: %s" % kind)
        with self._lock:
            block = self._pricing_block(self.doc, channel)
            if norm is None:
                block.pop("peak_rule", None)
            else:
                block["peak_rule"] = norm
            self._apply()
        return {"channel": channel, "peak_rule": norm}

    # ── 路由矩阵：分组与成员 ────────────────────────────────
    def _group_doc(self, doc: dict, gid: str, name: str = "") -> dict:
        groups = doc.setdefault("groups", {})
        group = groups.get(gid)
        if not isinstance(group, dict):
            group = {"name": name or gid.split("/", 1)[-1],
                     "policy": {"task_lock": True, "smart_tier": False},
                     "members": []}
            groups[gid] = group
        if not isinstance(group.get("members"), list):
            group["members"] = []
        return group

    def add_group_member(self, data: dict) -> dict:
        """给 auto/* 分组增/改一个成员（同渠道同模型视为替换）。"""
        gid = str(data.get("group") or "").strip()
        if not gid.startswith("auto/"):
            raise ConfigError("分组 id 必须以 auto/ 开头")
        channel = str(data.get("channel") or "").strip()
        if channel not in self.channels:
            raise ConfigError("未知渠道: %s" % channel)
        model = str(data.get("id") or data.get("upstream_name") or "").strip()
        if not model:
            raise ConfigError("成员必须指定模型")
        tier = str(data.get("tier") or "weak").strip()
        if tier not in ("strong", "weak", ""):
            raise ConfigError("tier 只能是 strong 或 weak")
        priority = _as_int(data.get("priority"), 100)
        enabled = _as_bool(data.get("enabled"), True)
        warning = ""
        if model not in (self.channels[channel].get("models") or []):
            warning = "该模型不在渠道模型清单，路由自检会报 member_model_missing"
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            group = self._group_doc(new_doc, gid, str(data.get("name") or ""))
            members = [m for m in group["members"] if isinstance(m, dict)]
            members = [m for m in members
                       if not (str(m.get("channel") or "") == channel
                               and str(m.get("id") or m.get("upstream_name") or "") == model)]
            added = {"channel": channel, "id": model, "upstream_name": model,
                     "tier": tier, "priority": priority, "enabled": enabled}
            members.append(added)
            members.sort(key=lambda m: (_as_int(m.get("priority"), 100),
                                        str(m.get("id") or "")))
            group["members"] = members
            self.doc = new_doc
            self._apply()
        return {"group": gid, "member": added, "members": len(members),
                "warning": warning}

    def remove_group_member(self, gid: str, channel: str, model: str) -> dict:
        gid = str(gid or "").strip()
        channel = str(channel or "").strip()
        model = str(model or "").strip()
        groups = self.doc.get("groups") if isinstance(self.doc.get("groups"), dict) else {}
        group = groups.get(gid)
        if not isinstance(group, dict):
            raise ConfigError("未知分组: %s" % gid)
        members = [m for m in (group.get("members") or []) if isinstance(m, dict)]
        kept = [m for m in members
                if not (str(m.get("channel") or "") == channel
                        and str(m.get("id") or m.get("upstream_name") or "") == model)]
        if len(kept) == len(members):
            raise ConfigError("分组内没有这个成员: %s/%s" % (channel, model))
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            self._group_doc(new_doc, gid)["members"] = kept
            self.doc = new_doc
            self._apply()
        return {"group": gid, "removed": {"channel": channel, "model": model},
                "members": len(kept)}

    def set_group_policy(self, gid: str, policy: dict) -> dict:
        gid = str(gid or "").strip()
        if gid not in self.groups:
            raise ConfigError("未知分组: %s" % gid)
        merged = dict(self.groups[gid].get("policy") or {})
        for key in ("task_lock", "smart_tier"):
            if key in (policy or {}):
                merged[key] = _as_bool(policy[key])
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            self._group_doc(new_doc, gid)["policy"] = merged
            self.doc = new_doc
            self._apply()
        return {"group": gid, "policy": merged}

    def delete_group(self, gid: str) -> dict:
        gid = str(gid or "").strip()
        groups = self.doc.get("groups") if isinstance(self.doc.get("groups"), dict) else {}
        if gid not in groups:
            raise ConfigError("未知分组: %s" % gid)
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            new_doc.get("groups", {}).pop(gid, None)
            self.doc = new_doc
            self._apply()
        return {"group": gid, "deleted": True}

    # ── 出口分路（代理腿）──────────────────────────────────
    def save_egress_leg(self, data: dict) -> dict:
        leg_id = str(data.get("id") or "").strip()
        if not EGRESS_ID_RE.match(leg_id) or leg_id == "direct":
            raise ConfigError("出口腿 id 只允许字母数字 . _ -（≤32），且不能叫 direct")
        url = str(data.get("url") or "").strip()
        if not url.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("代理地址必须是 http:// 或 https://（标准库不带 socks 支持）")
        row = {"id": leg_id, "name": str(data.get("name") or leg_id).strip() or leg_id,
               "url": url, "enabled": _as_bool(data.get("enabled"), True),
               "note": str(data.get("note") or "").strip()[:120]}
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            egress = new_doc.setdefault("egress", {})
            legs = [item for item in (egress.get("legs") or [])
                    if isinstance(item, dict) and str(item.get("id") or "") != leg_id]
            legs.append(row)
            legs.sort(key=lambda item: str(item.get("id") or ""))
            egress["legs"] = legs
            egress.setdefault("enabled", True)
            if _as_bool(data.get("make_default"), False):
                egress["default"] = leg_id
            self.doc = new_doc
            self._apply()
        return {"leg": row, "default": self.egress["default"]}

    def delete_egress_leg(self, leg_id: str) -> dict:
        leg_id = str(leg_id or "").strip()
        if leg_id not in {leg["id"] for leg in self.egress["legs"]}:
            raise ConfigError("未知出口腿: %s" % leg_id)
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            egress = new_doc.setdefault("egress", {})
            egress["legs"] = [item for item in (egress.get("legs") or [])
                              if str((item or {}).get("id") or "") != leg_id]
            if str(egress.get("default") or "") == leg_id:
                egress.pop("default", None)
            for entry in (new_doc.get("channels") or {}).values():
                if isinstance(entry, dict) and str(entry.get("egress") or "") == leg_id:
                    entry.pop("egress", None)
            oc = new_doc.get("oc_bridge")
            if isinstance(oc, dict) and str(oc.get("egress") or "") == leg_id:
                oc.pop("egress", None)
            self.doc = new_doc
            self._apply()
        return {"leg": leg_id, "deleted": True}

    def set_default_egress(self, leg_id: str) -> dict:
        leg_id = str(leg_id or "").strip()
        if leg_id and leg_id not in {leg["id"] for leg in self.egress["legs"]}:
            raise ConfigError("未知出口腿: %s" % leg_id)
        with self._lock:
            egress = self.doc.setdefault("egress", {})
            if leg_id:
                egress["default"] = leg_id
            else:
                egress.pop("default", None)
            self._apply()
        return {"default": self.egress["default"]}

    def set_channel_egress(self, name: str, leg_id: str) -> dict:
        name = self._channel_name(name)
        if name not in self.channels:
            raise ConfigError("未知渠道: %s" % name)
        if self.channels[name].get("virtual"):
            raise ConfigError("OC 桥渠道的出口请在 OC 桥设置里改")
        leg_id = str(leg_id or "").strip()
        if leg_id and leg_id not in {leg["id"] for leg in self.egress["legs"]}:
            raise ConfigError("未知出口腿: %s" % leg_id)
        with self._lock:
            new_doc = copy.deepcopy(self.doc)
            entry = (new_doc.get("channels") or {}).get(name)
            if not isinstance(entry, dict):
                raise ConfigError("未知渠道: %s" % name)
            if leg_id:
                entry["egress"] = leg_id
            else:
                entry.pop("egress", None)
            self.doc = new_doc
            self._apply()
        return {"channel": name, "egress": leg_id or "direct"}

    # ── OC 免费档桥 ─────────────────────────────────────────
    def save_oc_bridge(self, data: dict) -> dict:
        oc = dict(self.doc.get("oc_bridge") or {}) if isinstance(self.doc.get("oc_bridge"), dict) else {}
        mode = str(data.get("mode", oc.get("mode")) or "bridge").strip().lower()
        if mode not in ("bridge", "direct"):
            raise ConfigError("OC 桥 mode 只能是 bridge 或 direct")
        channel = str(data.get("channel", oc.get("channel")) or "oc-bridge").strip()
        if not CHANNEL_NAME_RE.match(channel):
            raise ConfigError("OC 桥渠道代号不合法")
        existing = self.doc.get("channels", {}).get(channel)
        if existing is not None and channel != self.oc_bridge.get("channel"):
            raise ConfigError("渠道 %s 已被占用，换个代号" % channel)
        bridge_url = str(data.get("bridge_url", oc.get("bridge_url"))
                         or "http://127.0.0.1:8898/v1").strip().rstrip("/")
        if not bridge_url.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("桥地址必须是 http(s) 绝对地址")
        upstream = str(data.get("upstream", oc.get("upstream"))
                       or "https://opencode.ai/inference/v1").strip().rstrip("/")
        if not upstream.startswith(ALLOWED_PROTOCOLS):
            raise ConfigError("上游基址必须是 http(s) 绝对地址")
        leg = str(data.get("egress", oc.get("egress")) or "").strip()
        if leg and leg not in {item["id"] for item in self.egress["legs"]}:
            raise ConfigError("未知出口腿: %s" % leg)
        models = _str_list(data.get("models", oc.get("models")))
        user_agent = str(data.get("user_agent", oc.get("user_agent")) or "").strip()
        token_env = str(data.get("token_env", oc.get("token_env")) or "OC_BRIDGE_TOKEN").strip()
        raw_token = data.get("token")
        token = "" if raw_token is None else str(raw_token).strip()
        if token in ("●●●●●●", "********"):
            token = ""
        if _as_bool(data.get("clear_token"), False):
            if self.vault is not None:
                self.vault.delete("oc_bridge")
        elif token:
            if self.vault is None:
                raise ConfigError("密钥仓未挂载，无法保存桥 token")
            self.vault.set("oc_bridge", token)
        new_doc = copy.deepcopy(self.doc)
        new_doc["oc_bridge"] = {
            "enabled": _as_bool(data.get("enabled"), _as_bool(oc.get("enabled"), False)),
            "mode": mode,
            "channel": channel,
            "bridge_url": bridge_url,
            "upstream": upstream,
            "models": models,
            "user_agent": user_agent,
            "egress": leg,
            "token_env": token_env,
        }
        self.doc = new_doc
        self._apply()
        warning = ""
        if self.oc_bridge["enabled"] and mode == "bridge" and not self.oc_bridge["token"]:
            warning = "桥开了但没有 token：直连桥若设了 OC_DIRECT_TOKEN 会回 401"
        if self.oc_bridge["enabled"] and not self.oc_bridge["models"]:
            warning = (warning + " / " if warning else "") + "还没填模型清单，可用「探测桥」拉取"
        return {"oc_bridge": self.public()["oc_bridge"], "warning": warning}

    def save(self) -> None:
        """原子回写整份配置；控制面变更必须立即落盘，重启不丢。"""
        if not self.path:
            raise ConfigError("内存配置没有文件路径，无法保存")
        text = json.dumps(self.doc, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        self._stamp = self._stat_stamp()
