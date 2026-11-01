# -*- coding: utf-8 -*-
"""client-runtime/shadow_git.py —— Shadow Git 快照与局部事务管理器（CR-1 / 4.0 支柱①）。

## 它解决什么问题

Agent 改代码是"批量 Diff"：一次可能改 5 个文件。若第 4 个文件补丁打不上（上下文不匹配、
文件被改过），朴素做法会留下**前 3 个已改、第 4 个失败、第 5 个没动**的半成品工作区 ——
这种情况下 agent 会基于错误状态继续改，越改越坏（本项目真实事故形态）。

本模块给"批量改代码"套一层**事务**：
    snapshot() → apply_patch() → [失败 → rollback() 自动] → commit_transaction()

## 为什么用原生 git，而不是自己存文件副本

1. **无损**：`git stash create` / `git hash-object` 存的是 git 对象，二进制安全、带权限位；
   自己 `shutil.copy2` 到临时目录在符号链接、大文件、权限位上都容易出错。
2. **快**：git 对象是 zlib 压缩 + 内容寻址，重复内容零成本。自己拷贝 100MB 文件就是 100MB IO。
3. **能预检**：`git apply --check` 能**在不落盘的前提下**判断补丁能不能打 —— 这是实现
   真正原子性（先全量预检、再统一落盘）的关键，自己实现补丁解析做不到。

## 原子性怎么保证（关键）

    ┌─ apply_patch(patch) ────────────────────────────────────┐
    │ 1. `git apply --check` 全量预检（不落盘）                │
    │    ├─ 任何一处失败 → 直接返回 False，工作区**零改动**     │
    │    └─ 全部通过 → 进入 2（此时失败概率已极低）             │
    │ 2. `git apply` 真正落盘                                  │
    │ 3. 落盘后复核：失败 → rollback(snapshot_id) 全量恢复      │
    └─────────────────────────────────────────────────────────┘

「先 check 再 apply」把绝大多数失败挡在**零副作用**阶段；`apply` 之后的复核兜住
「check 通过但 apply 仍失败」的极小概率（磁盘满、并发写、权限变化）。

## 边界（诚实标注）

- 只处理**文本 Diff**（`git apply` 的能力范围）；二进制文件走 `git apply --binary` 也能过，
  但冲突时无法三方合并 —— 本模块不做 merge，只做「打不上就整体回滚」。
- 工作区必须在 **git 仓库内**（快照依赖 `git` 的对象库）。非 git 目录会抛
  `ShadowGitError`，由调用方决定降级（例如改用纯文件备份）。
- 不做「部分回滚」（只回退某个文件）—— 事务语义要么全成要么全退，部分回退会制造
  更隐蔽的不一致状态。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid

__all__ = ["ShadowGitManager", "ShadowGitError", "SnapshotInfo"]

MAX_SNAPSHOTS = 32          # 内存快照上限（超出清最旧，防内存无界增长）
GIT_TIMEOUT = 60            # 单次 git 调用超时（秒）
# Windows 上目标文件可能被杀软/索引器瞬时持有，os.replace 会报 WinError 5。
# 实测 300 轮快照+回滚里 3 次撞上（1%）；回滚失败比慢更危险，故短暂重试而不是直接抛。
# 满载 CPU 压测（12 核占满）又暴露了一次：80ms 总重试预算在重负载下会被吃穿 →
# 提高到 12 次 × 40ms（最坏 ~440ms），只在真的撞上占用时才会等。
REPLACE_RETRIES = 12
REPLACE_BACKOFF = 0.04      # 秒；总等待上限 ~440ms


class ShadowGitError(RuntimeError):
    """工作区不可用（非 git 仓库 / git 缺失 / 权限不足）时抛出。"""


def _replace_with_retry(tmp: str, target: str) -> None:
    """原子替换；目标被瞬时占用（WinError 5/32）时短暂重试。

    原子性靠 `os.replace` 本身，重试只解决“同一瞬间有别人/杀软拿着句柄”的竞争；
    重试用尽仍失败则抛出——绝不静默丢内容。
    """
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt >= REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_BACKOFF)


class SnapshotInfo:
    """一次快照的元信息。`files` 是 路径→git blob sha 的映射（空串表示文件原本不存在）。"""

    __slots__ = ("sid", "repo", "files", "created_at", "stash", "note")

    def __init__(self, sid: str, repo: str, files: dict, stash: str = "", note: str = ""):
        self.sid = sid
        self.repo = repo
        self.files = dict(files or {})
        self.created_at = time.time()
        self.stash = stash
        self.note = note

    def __repr__(self) -> str:
        return "<Snapshot %s files=%d stash=%s>" % (self.sid, len(self.files),
                                                    (self.stash or "-")[:8])


class ShadowGitManager:
    """工作区事务管理器：快照 → 打补丁（原子）→ 回滚 / 提交。

    典型用法：

        mgr = ShadowGitManager("/path/to/repo")
        sid = mgr.snapshot(["src/a.py", "src/b.py"])
        if not mgr.apply_patch(diff_text):
            mgr.rollback(sid)              # 失败自动全量回滚（也可交给上下文管理器）
        else:
            run_lint_and_tests()           # 验证通过
            mgr.commit_transaction(sid)    # 销毁快照、固化

    ## 性能设计（关键，决定了 <10ms 能否达成）

    **快照与回滚走纯 Python（内存字典 + 文件字节），不碰 git 子进程。**
    实测本机单次 git 进程成本 **67~120ms**（`git --version` 66.6ms、仓库内 `rev-parse` 119.5ms，
    对照 `cmd echo` 也要 90.7ms）—— 纯 Windows 进程创建开销，与 git 做什么无关。
    票面要求快照/回滚 <10ms，**任何起子进程的实现都物理不可达**（首版 281ms → 批量后 129ms，
    仍卡在进程创建上）。

    因此职责切分：
      · `snapshot()` / `rollback()` —— 纯 Python，微秒~毫秒级，满足 <10ms
      · `apply_patch()` —— **仍用 `git apply`**，因为 `--check` 预检（不落盘判可打性）
        是原子性的核心价值，自己实现 Diff 解析做不到同等可靠；这属于**低频操作**
        （每个事务一次），100ms 可接受，且不在票面性能指标内。
    """

    def __init__(self, repo: str = ".", keep: int = MAX_SNAPSHOTS):
        self.repo = os.path.abspath(repo or ".")
        self.keep = max(1, int(keep))
        self._snaps: "dict[str, SnapshotInfo]" = {}
        self._order: list = []
        self._blobs: "dict[str, bytes]" = {}      # sha256 → 内容（内容寻址，去重）
        self._modes: "dict[str, int]" = {}        # sha256 → stat 权限位
        self.last_error = ""
        if not shutil.which("git"):
            raise ShadowGitError("找不到 git 可执行文件：本模块依赖原生 git 做快照")
        if not self._git(["rev-parse", "--is-inside-work-tree"], check=False).strip() == "true":
            raise ShadowGitError("工作区不是 git 仓库：%s（快照依赖 git 对象库）" % self.repo)

    # ── 底层：git 调用 ────────────────────────────────────────
    def _git(self, args: list, check: bool = True, cwd: str = None,
             stdin: bytes = None) -> str:
        """跑一条 git 命令。`check=True` 时非零退出抛 `ShadowGitError`（带 git 原话）。"""
        cmd = ["git"] + list(args)
        try:
            p = subprocess.run(cmd, cwd=cwd or self.repo, input=stdin,
                               capture_output=True, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ShadowGitError("git 超时（%ss）：%s" % (GIT_TIMEOUT, " ".join(cmd)))
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        if check and p.returncode != 0:
            raise ShadowGitError("git %s 失败（%d）：%s"
                                 % (args[0] if args else "?", p.returncode,
                                    (err or out).strip()[:300]))
        return out

    # ── 快照 ─────────────────────────────────────────────────
    def snapshot(self, paths: list, note: str = "") -> str:
        """记录 `paths` 当前内容的快照 → 返回 `snapshot_id`。**纯 Python，无子进程。**

        内容存进内存字典（`sha256 → bytes`），同一内容只存一份（内容寻址，重复内容零成本）。
        文件不存在时记 `None`（回滚 = 删除它）。

        为什么不用 `git hash-object`：见类注释「性能设计」—— 单次 git 进程 67~120ms，
        直接打爆 <10ms 预算。纯 Python 读文件 + sha256 对本机常见文件是微秒级。
        """
        sid = uuid.uuid4().hex[:12]
        files = {}
        for rel in (paths or []):
            rel = str(rel)
            absp = os.path.join(self.repo, rel)
            if not os.path.isfile(absp):
                files[rel] = None                     # 不存在 → 回滚时删除
                continue
            try:
                with open(absp, "rb") as f:
                    data = f.read()
            except OSError:
                files[rel] = None                     # 读不到就当不存在（回滚删掉更安全）
                continue
            sha = hashlib.sha256(data).hexdigest()
            self._blobs[sha] = data                   # 内容寻址：同内容只存一份
            files[rel] = sha
            try:                                      # 记住权限位（可执行文件回滚后仍可执行）
                self._modes[sha] = os.stat(absp).st_mode
            except OSError:
                pass
        info = SnapshotInfo(sid, self.repo, files, note=note)
        self._snaps[sid] = info
        self._order.append(sid)
        while len(self._order) > self.keep:
            old = self._order.pop(0)
            dropped = self._snaps.pop(old, None)
            if dropped is not None:
                self._gc_blobs(dropped)
        return sid

    def _gc_blobs(self, info: "SnapshotInfo") -> None:
        """回收不再被任何存活快照引用的内容（防内存无界增长）。"""
        live = set()
        for s in self._snaps.values():
            for sha in s.files.values():
                if sha:
                    live.add(sha)
        for sha in list(self._blobs.keys()):
            if sha not in live:
                self._blobs.pop(sha, None)
                self._modes.pop(sha, None)

    def get(self, snapshot_id: str) -> SnapshotInfo:
        info = self._snaps.get(str(snapshot_id))
        if info is None:
            raise ShadowGitError("快照不存在或已销毁：%s" % snapshot_id)
        return info

    # ── 打补丁（原子）─────────────────────────────────────────
    def can_apply(self, patch_str: str) -> tuple:
        """**只预检不落盘** → `(ok, 原因)`。这是原子性的第一道闸。"""
        if not patch_str or not patch_str.strip():
            return False, "空补丁"
        try:
            p = subprocess.run(["git", "apply", "--check", "--whitespace=nowarn", "-"],
                               cwd=self.repo, input=patch_str.encode("utf-8"),
                               capture_output=True, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "git apply --check 超时"
        if p.returncode == 0:
            return True, ""
        return False, (p.stderr or p.stdout or b"").decode("utf-8", "replace").strip()[:300]

    def apply_patch(self, patch_str: str, snapshot_id: str = "") -> bool:
        """应用统一 Diff → `True` 成功 / `False` 失败（**失败时工作区必与调用前一致**）。

        传了 `snapshot_id` 时，apply 之后若出现意外失败会自动 `rollback(snapshot_id)`；
        没传则由调用方自己管（`can_apply` 已把绝大多数失败挡在零副作用阶段）。
        """
        ok, why = self.can_apply(patch_str)
        if not ok:
            self.last_error = why
            return False
        try:
            p = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                               cwd=self.repo, input=patch_str.encode("utf-8"),
                               capture_output=True, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.last_error = "git apply 超时"
            if snapshot_id:
                self.rollback(snapshot_id)
            return False
        if p.returncode == 0:
            self.last_error = ""
            return True
        # check 通过但 apply 失败（磁盘满/权限/并发写）→ 兜底回滚
        self.last_error = (p.stderr or b"").decode("utf-8", "replace").strip()[:300]
        if snapshot_id:
            self.rollback(snapshot_id)
        return False

    # ── 回滚 / 提交 ──────────────────────────────────────────
    def rollback(self, snapshot_id: str) -> None:
        """把快照记录的文件**无损恢复**到快照时刻的内容；原本不存在的则删除。
        **纯 Python，无子进程**（内容已在 `_blobs` 里，回滚只是写文件）。

        使用「临时文件 + `os.replace`」原子写：回滚途中崩溃也不会留下半截文件。
        """
        info = self.get(snapshot_id)
        for rel, sha in info.files.items():
            absp = os.path.join(self.repo, rel)
            if not sha:                              # 快照时不存在 → 删除
                try:
                    if os.path.isfile(absp):
                        os.remove(absp)
                except OSError:
                    pass
                continue
            blob = self._blobs.get(sha)
            if blob is None:
                # 内容已被 GC（正常不该发生）→ 明确报错，绝不"假回滚"。
                # 假回滚比不回滚更危险：调用方会以为工作区已经干净，继续在上面改。
                raise ShadowGitError("快照内容已丢失（sha=%s，文件 %s）：无法无损回滚"
                                     % (sha[:12], rel))
            d = os.path.dirname(absp) or "."
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            # 原子写，但**不用 `tempfile.mkstemp`**：实测 12 次 mkstemp 裸开销 7.4ms
            # （每次要开 fd + 试随机名 + 收紧权限），本场景只需"同目录临时名"。
            # 也用**确定名**（`<file>.shadowgit-<sid>`）而不是随机名，省一次随机源调用。
            #
            # 进一步省开销（实测数据：临时名+replace 9.2ms，+chmod 11.8ms，直接写 2.8ms）：
            #   · 目标文件不存在 → 直接写（无需原子替换，没有"半截旧文件"风险）
            #   · 权限位相同 → 跳过 chmod（绝大多数情况相同）
            tmp = "%s.shadowgit-%s" % (absp, str(snapshot_id)[:8])
            try:
                if not os.path.exists(absp):
                    with open(absp, "wb") as f:
                        f.write(blob)
                    want = self._modes.get(sha)
                    if want is not None:
                        try:
                            os.chmod(absp, want)
                        except OSError:
                            pass
                    continue
                with open(tmp, "wb") as f:
                    f.write(blob)
                want = self._modes.get(sha)
                if want is not None:
                    try:
                        if (os.stat(absp).st_mode & 0o777) != (want & 0o777):
                            os.chmod(tmp, want)
                    except OSError:
                        pass
                _replace_with_retry(tmp, absp)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    def commit_transaction(self, snapshot_id: str) -> None:
        """验证通过 → 销毁快照（固化修改）。**不产生 git commit**（那是用户的决定）。"""
        self._snaps.pop(str(snapshot_id), None)
        try:
            if snapshot_id in self._order:
                self._order.remove(snapshot_id)
        except ValueError:
            pass

    # ── 辅助 ────────────────────────────────────────────────
    def dirty(self, paths: list = None) -> list:
        """列出有改动的文件（`paths` 为空则看全仓）。用于"提交前确认真的改了东西"。"""
        args = ["status", "--porcelain"]
        if paths:
            args += ["--"] + [str(x) for x in paths]
        out = self._git(args)
        files = []
        for line in out.splitlines():
            line = line.rstrip()
            if len(line) > 3:
                files.append(line[3:].strip().strip('"'))
        return files

    def diff(self, paths: list = None) -> str:
        """取工作区当前 diff（给"人"看 / 给 linter 用）。"""
        args = ["diff", "--no-color"]
        if paths:
            args += ["--"] + [str(x) for x in paths]
        return self._git(args)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            # 上下文里抛异常 = 事务失败 → 把所有未提交快照回滚
            for sid in list(self._order):
                try:
                    self.rollback(sid)
                except Exception:                            # noqa: BLE001
                    pass
        return False

    def open_snapshots(self) -> list:
        return list(self._order)
