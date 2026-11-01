# -*- coding: utf-8 -*-
r"""client-runtime/tool_guard.py —— 端侧工具调用白名单二次校验 + 轻量沙箱（CR-2 / 4.0 支柱③ Layer C）。

## 为什么客户端还要再拦一次（网关已经拦过了）

网关（GW-11 `core/tool_ast.py`）拦的是**模型吐出的工具调用参数**，属于"出站前"的第一道闸。
但客户端是**真正执行**的那一侧，它面对的风险与网关不同：

1. **网关被绕过**：客户端可以直连模型、或用离线缓存的补丁；网关不是必经之路时，唯一防线只剩这里。
2. **参数在客户端被二次加工**：模板替换、变量展开、相对路径拼接，都发生在"网关检查之后、
   真正执行之前"。`workspace_root/../../etc/passwd` 在网关看是普通字符串，在客户端落地就成了越界。
3. **工具名本身就是攻击面**：模型可能调用一个不存在的工具去探测宿主能力。

所以本模块的定位是：**纵深防御的最后一道，宁可误拒不可误放**（与网关"宁可漏报不误伤"的取向相反 ——
因为到这里已经是执行前的最后一步，误放的代价是真被删库）。

## 两层防护

### ① 白名单 + 路径越界检查（`check_tool_call`）

- **工具白名单**：默认只放行开发动作（`read_file`/`write_file`/`run_tests`/`git_status`/
  `git_diff`/`git_commit`）。不在名单里的一律拒。
- **路径越界**：所有 `path`/`file`/`cwd` 类参数解析成绝对路径后，必须落在 `workspace_root` 内；
  同时屏蔽系统敏感目录（`~/.ssh`、`C:\Windows\System32`、`/etc` 等）。
  **用 `os.path.realpath` 而非 `abspath`**：符号链接可以绕过单纯的字符串前缀比较。
- **命令审计**：`command` 类参数先过本模块的命令黑名单（与网关同口径的保守集）。

### ② 轻量沙箱子进程（`execute_command_sandboxed`）

- **环境变量清洗**：删掉 `*_KEY`/`*_TOKEN`/`*_SECRET`/`AWS_*`/`GITHUB_*` 等，子进程拿不到凭据。
- **工作目录锁定**：`cwd` 必须在 workspace 内，别处一律拒。
- **超时**：默认 30s，超时杀进程组。
- **输出上限**：防止 `yes`/`cat 大文件` 把客户端内存打爆。
- **命令审计**：高危命令（`rm -rf`、`mkfs`、管道喂 shell 等）直接拒，不进沙箱。

## 诚实边界（重要）

标准库**做不出真正的 OS 级沙箱**：Windows 要 Job Object / AppContainer，Linux 要
`bwrap`/`seccomp`，macOS 要 Seatbelt —— 它们都不是"纯 Python 标准库"能调的。
本模块给的是**轻量隔离**（环境变量清洗 + 工作目录锁定 + 超时 + 输出上限 + 命令审计），
能挡住绝大多数 agent 误操作与低成本逃逸，但**挡不住蓄意构造的本地提权**。
票面明确要求"纯 Python 标准库实现，适配 Windows 宿主"，故严格按此范围交付，
不吹成"真沙箱"。真正的 OS 级隔离属于后续独立切片（需引入平台能力）。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess

__all__ = ["ClientToolGuard", "ToolBlocked", "DEFAULT_ALLOWLIST", "BLOCKED_MARK"]

# 默认放行的开发动作（票面指定）。刻意**不含** `run_shell`/`exec` 这类万能入口：
# 万能入口等于没有白名单。
DEFAULT_ALLOWLIST = frozenset({
    "read_file", "write_file", "run_tests",
    "git_status", "git_diff", "git_commit",
})

# 拒绝时回传给模型的受控错误标记（与网关出站口径一致，便于统一检索）
BLOCKED_MARK = "TOOL_BLOCKED"

# 参数名里"值是路径"的键（大小写不敏感匹配）
_PATH_KEYS = ("path", "file", "filename", "filepath", "file_path", "target", "dest",
              "destination", "src", "source", "cwd", "dir", "directory", "workdir")
# 参数名里"值是命令"的键
_CMD_KEYS = ("command", "cmd", "script", "shell")

# 系统敏感目录（解析成绝对路径后按前缀/子串双重判定）
SENSITIVE_DIRS = (
    ".ssh", ".aws", ".kube", ".gnupg", ".docker", ".npmrc", ".pypirc", ".netrc",
    "/etc", "/proc", "/sys", "/dev", "/boot",
    "windows\\system32", "windows\\syswow64", "program files", "$recycle.bin",
    "system volume information",
)

# 高危命令（保守集：宁可误拒，因为这里已是执行前最后一步）
_DENY = (
    ("rm_rf", re.compile(r"\brm\s+-(?:rf|fr|r\s*-f)\b")),
    ("mkfs", re.compile(r"\bmkfs(?:\.\w+)?\b")),
    ("dd", re.compile(r"\bdd\s+if=")),
    ("fork_bomb", re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;?\s*:")),
    ("shutdown", re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b")),
    ("pipe_to_shell", re.compile(
        r"\b(?:base64\s+-d|curl|wget|cat)\b[^\n|]{0,200}\|\s*(?:ba|z|k|da)?sh\b")),
    # 内联脚本：`sh -c`、`python -c`、`node -e` 等。
    # 但**放行"明显无副作用"的内联**（只有 print/import/字面量运算）——否则 agent 连
    # `python -c "print(1)"` 这种最常用的探活都做不了，会把它逼去用更危险的方式。
    # 判定方式：在白名单函数名里找不到任何 IO/执行类调用，且不含 open/os/subprocess 等。
    ("inline_script", re.compile(
        r"\b(?:sh|bash|zsh|python[0-9.]*|perl|node)\b[^\n]{0,40}\s-(?:c|e)\s")),
    ("chmod_777", re.compile(r"\bchmod\s+(?:777|u\+s|\+s)\b")),
    ("nc_listen", re.compile(r"\b(?:nc|ncat|netcat)\b[^\n]{0,40}-l")),
)

# 内联脚本里出现这些**执行/IO 类**符号才算危险（否则视为无害探活，放行）
_INLINE_DANGER = re.compile(
    r"\b(?:os\.|subprocess|system|popen|exec|eval|compile|__import__|open\(|"
    r"shutil|pathlib\.Path\([^)]*\)\.(?:write|unlink)|socket|requests|urllib|"
    r"ctypes|pickle|marshal|base64|input\()", re.I)


class ToolBlocked(RuntimeError):
    """工具调用被拒（携带受控原因，供上层回传模型）。"""


def _norm(p: str) -> str:
    return str(p or "").replace("/", os.sep).replace("\\", os.sep).lower()


class ClientToolGuard:
    """端侧工具调用守卫：白名单 + 路径越界 + 命令审计 + 轻量子进程隔离。

    用法：
        g = ClientToolGuard(workspace_root="/path/to/repo")
        ok, why = g.check_tool_call("write_file", {"path": "src/a.py"})
        if ok:
            rc, out, err = g.execute_command_sandboxed("pytest -q", cwd=".")
    """

    def __init__(self, allowlist=None, workspace_root: str = ".",
                 max_output: int = 256 * 1024, deny_extra=None):
        self.workspace_root = os.path.realpath(workspace_root or ".")
        self.allowlist = frozenset(allowlist) if allowlist is not None else DEFAULT_ALLOWLIST
        self.max_output = max(1024, int(max_output))
        self.deny_extra = list(deny_extra or [])
        self.last_reason = ""

    # ── 路径越界 ──────────────────────────────────────────────
    def resolve_path(self, p: str) -> tuple:
        """把参数路径解析成**真实绝对路径** → `(ok, abs_path, 原因)`。

        用 `realpath` 而不是 `abspath`：符号链接能把 `workspace/link -> /etc` 变成
        "看起来在内部、实际在外部"。判越界必须看最终落点。
        """
        raw = str(p or "").strip()
        if not raw:
            return False, "", "路径为空"
        # 展开 `~` 与相对路径（相对 workspace_root）
        cand = os.path.expanduser(raw)
        if not os.path.isabs(cand):
            cand = os.path.join(self.workspace_root, cand)
        real = os.path.realpath(cand)
        root = self.workspace_root
        if real != root and not real.startswith(root + os.sep):
            return False, real, "路径越界：%s 不在工作区 %s 内" % (real, root)
        low = _norm(real)
        for bad in SENSITIVE_DIRS:
            if _norm(bad) in low:
                return False, real, "命中系统敏感目录：%s" % bad
        return True, real, ""

    # ── 命令审计 ──────────────────────────────────────────────
    def audit_command(self, command: str) -> tuple:
        """→ `(ok, 原因)`。保守集：命中即拒。"""
        text = str(command or "")
        if not text.strip():
            return False, "命令为空"
        if len(text) > 8192:
            return False, "命令过长（%d 字符）" % len(text)
        for name, pat in _DENY:
            if pat.search(text):
                # 内联脚本特例：只含 print/import/运算的无害探活放行（见 _INLINE_DANGER）
                if name == "inline_script" and not _INLINE_DANGER.search(text):
                    continue
                return False, "高危命令：%s" % name
        for name, pat in self.deny_extra:
            if pat.search(text):
                return False, "高危命令：%s" % name
        # 逐段看可执行名。⚠️ 分段必须**尊重引号**：直接正则切 `;`/`|` 会把
        # `python -c "import time; time.sleep(5)"` 切成两半，后半段引号不配对，
        # 于是被判"无法安全分词"——把无害探活误拒（实测踩过）。
        # 正确做法：先尝试整条分词；成功说明引号自洽，再在**已分词结果**上看有没有
        # 危险可执行名；整条分词失败才按 fail-closed 拒。
        try:
            whole = shlex.split(text, posix=True)
        except ValueError:
            return False, "命令无法安全分词（引号不配对）"
        heads = set()
        for seg in self._split_outside_quotes(text):
            seg = seg.strip()
            if not seg:
                continue
            try:
                toks = shlex.split(seg, posix=True)
            except ValueError:
                toks = []
            for t in (toks or seg.split()):
                heads.add(t.rsplit("/", 1)[-1].lower())
        for h in heads:
            if h in ("rm", "mkfs", "dd", "shutdown", "reboot", "nc", "ncat", "netcat"):
                return False, "高危可执行名：%s" % h
        # 整条分词成功即可执行（引号自洽）；`whole` 只用于确认可解析
        _ = whole
        return True, ""

    @staticmethod
    def _split_outside_quotes(text: str) -> list:
        """按 `|`/`;`/`&&`/换行 切段，**但跳过引号内部**。"""
        out, buf = [], []
        q = ""
        i = 0
        while i < len(text):
            ch = text[i]
            if q:
                buf.append(ch)
                if ch == q:
                    q = ""
                i += 1
                continue
            if ch in "\"'":
                q = ch
                buf.append(ch)
                i += 1
                continue
            if ch in "|;\n":
                out.append("".join(buf))
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
        return out

    # ── 主入口：工具调用校验 ──────────────────────────────────
    def check_tool_call(self, tool_name: str, arguments: dict) -> tuple:
        """执行前白名单 + 参数边界校验 → `(is_allowed, reason)`。

        耗时预算 <1ms：全部是字符串比较与 `realpath`（后者对单文件 ~20µs），不读文件内容。
        """
        name = str(tool_name or "").strip()
        if not name:
            self.last_reason = "工具名为空"
            return False, self.last_reason
        if name not in self.allowlist:
            self.last_reason = "%s: 工具 %s 不在白名单（允许：%s）" % (
                BLOCKED_MARK, name,
                ",".join(sorted(self.allowlist))[:200])
            return False, self.last_reason

        args = arguments if isinstance(arguments, dict) else {}
        # ① 路径类参数逐个解析
        for k, v in args.items():
            lk = str(k).lower()
            if lk in _PATH_KEYS and isinstance(v, (str, os.PathLike)):
                ok, _abs, why = self.resolve_path(v)
                if not ok:
                    self.last_reason = "%s: %s.%s → %s" % (BLOCKED_MARK, name, k, why)
                    return False, self.last_reason
        # ② 命令类参数过审计
        for k, v in args.items():
            lk = str(k).lower()
            if lk in _CMD_KEYS and isinstance(v, str):
                ok, why = self.audit_command(v)
                if not ok:
                    self.last_reason = "%s: %s.%s → %s" % (BLOCKED_MARK, name, k, why)
                    return False, self.last_reason
        self.last_reason = ""
        return True, ""

    # ── 轻量沙箱执行 ─────────────────────────────────────────
    def _clean_env(self, extra: dict = None) -> dict:
        """清掉凭据类环境变量（子进程不该看见任何 key/token）。"""
        out = {}
        drop = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|"
                          r"AUTH|SESSION|COOKIE)(?:_|$)", re.I)
        prefix_drop = ("AWS_", "GITHUB_", "GH_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_",
                       "AZURE_", "GOOGLE_", "GCP_", "ALIYUN_", "TENCENT_", "HUB_",
                       "DSH_", "SSH_", "GPG_")
        for k, v in os.environ.items():
            ku = k.upper()
            if drop.search(ku) or any(ku.startswith(p) for p in prefix_drop):
                continue
            out[k] = v
        # 明确的路径类变量保留（子进程要能找到 python/系统工具）
        out.update(extra or {})
        return out

    def execute_command_sandboxed(self, command: str, cwd: str = None,
                                  timeout: float = 30.0) -> tuple:
        """在轻量隔离下跑一条命令 → `(returncode, stdout, stderr)`。

        `returncode` 约定：`-1` = 被守卫拒绝（未执行）；`-9` = 超时被杀。
        **不抛异常**——调用方（agent 循环）需要稳定拿到可回传模型的结果。
        """
        ok, why = self.audit_command(command)
        if not ok:
            self.last_reason = why
            return -1, "", "%s: %s" % (BLOCKED_MARK, why)

        run_cwd = self.workspace_root
        if cwd:
            c_ok, abs_cwd, c_why = self.resolve_path(cwd)
            if not c_ok:
                self.last_reason = c_why
                return -1, "", "%s: cwd %s" % (BLOCKED_MARK, c_why)
            if not os.path.isdir(abs_cwd):
                return -1, "", "%s: cwd 不存在 %s" % (BLOCKED_MARK, abs_cwd)
            run_cwd = abs_cwd

        try:
            # ⚠️ 必须用 `Popen` + 显式杀进程树，不能只靠 `subprocess.run(timeout=)`：
            # `shell=True` 时超时只杀掉外层 shell，**孙子进程（真正的 python）继续跑**，
            # 实际等待时间远超 timeout（实测 timeout=0.6s 却等了 5.1s）。
            # 超时的价值就是及时止损，必须连进程树一起杀。
            if os.name == "nt":
                p = subprocess.Popen(
                    command, shell=True, cwd=run_cwd, env=self._clean_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=0x08000000)                  # CREATE_NO_WINDOW
            else:
                p = subprocess.Popen(
                    command, shell=True, cwd=run_cwd, env=self._clean_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True)                    # 独立进程组，便于整组杀
            try:
                raw_out, raw_err = p.communicate(timeout=max(0.1, float(timeout)))
                rc = int(p.returncode or 0)
            except subprocess.TimeoutExpired:
                self._kill_tree(p)
                try:
                    raw_out, raw_err = p.communicate(timeout=2.0)
                except Exception:                                # noqa: BLE001
                    raw_out, raw_err = b"", b""
                self.last_reason = "timeout"
                return -9, "", "命令超时（%.1fs）被杀（含子进程树）" % float(timeout)
        except Exception as e:                                   # noqa: BLE001
            self.last_reason = str(e)[:200]
            return -1, "", "执行失败：%s" % str(e)[:200]

        raw_out = raw_out or b""
        raw_err = raw_err or b""
        out = raw_out[: self.max_output].decode("utf-8", "replace")
        err = raw_err[: self.max_output].decode("utf-8", "replace")
        if len(raw_out) > self.max_output:
            out += "\n...[输出超 %d 字节已截断]" % self.max_output
        if len(raw_err) > self.max_output:
            err += "\n...[错误输出超 %d 字节已截断]" % self.max_output
        self.last_reason = ""
        return rc, out, err

    @staticmethod
    def _kill_tree(p) -> None:
        """杀掉整个进程树（`shell=True` 时孙进程不跟着死的话，超时形同虚设）。"""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, timeout=10)
            else:
                import signal
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:                                        # noqa: BLE001
            try:
                p.kill()
            except Exception:                                    # noqa: BLE001
                pass
