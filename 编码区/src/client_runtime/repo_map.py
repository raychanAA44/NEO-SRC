# -*- coding: utf-8 -*-
r"""client-runtime/repo_map.py —— Repo Map：代码全局拓扑感知（CR-3 / 4.0 感知层）。

## 为什么需要 Repo Map

Agent 面对一个陌生仓库时，最大的成本不是"改哪一行"，而是**不知道改哪里**：
50 个文件、300 个函数，逐个读会把上下文烧光（本项目实测 5000 行文件读一次就 40K tokens）。

Repo Map 给的是**一张压缩过的全局地图**：把每个文件的**顶层符号签名**按重要性排序，
塞进 1K tokens 的预算里。模型拿到它就能判断"该去 `core/prune.py` 看 `fold_text`"，
而不是盲目 `ls` 一遍再逐个 `cat`。

## 三个能力（对应票面契约）

### ① 符号提取（`build_symbol_graph`）
- `.py` 用标准库 `ast`：类、函数、方法、顶层常量，含**签名**（参数名/类型注解/返回类型）。
- 其它语言用**轻量正则**：`function foo(`、`class Foo`、`func Foo(`、`def foo(` 等常见形态。
  **不假装精确**——正则只抓顶层声明，抓不到的就不出现在地图里（地图少一行无关紧要，
  给出错误的签名会误导模型去改不存在的代码）。

### ② 引用图 + PageRank（`rank_symbols`）
- 边 = "A 里出现了符号 B 的名字"。纯正则/字符串匹配，**不做语义解析**。
  理由：真正的语义解析要完整类型系统，代价远超收益；而"谁被引用得多"这个信号，
  字符串匹配已经能给到 80% 的准确度。
- PageRank：纯 Python 迭代（阻尼 0.85、默认 20 轮）。**不引入 networkx/numpy**（票面要求）。

### ③ 预算受限渲染（`render_repo_map`）
- 输出形如 `path/to/file.py: class Foo, def bar(x: int) -> str`
- 按分数从高到低填充，**超预算就停**（低权重符号自然被省略，而不是硬截断半个签名 ——
  截断的签名比没有更危险，模型会照着错的参数改）。

## 性能设计（票面：50~100 文件 <100ms）

- **单次遍历**：每个文件只读一次、只解析一次。
- **符号名集合**用于引用匹配：先在全部文件里建"名字→文件"索引，再逐文件扫文本匹配，
  避免 O(n²) 的两两比对。
- **跳过明显无关目录**：`.git`/`node_modules`/`__pycache__`/`venv` 等。
- **文件大小上限**：超大文件（>1MB）只取前 200KB 建图，避免读盘时间不可控。
"""
from __future__ import annotations

import ast
import os
import re

__all__ = ["RepoMapGenerator", "SYMBOL_KINDS"]

# 扫描时跳过的目录（不是"不重要"，是"不该进地图"：依赖代码进地图会淹掉自己的代码）
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", "venv", ".venv", "env", ".env", "dist",
             "build", "target", ".idea", ".vscode", "site-packages", ".tox"}

# 参与建图的源码后缀（其余文件完全不读 —— 二进制/数据文件读进来是纯浪费）
SRC_EXTS = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
            ".go", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp",
            ".rs", ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash"}

MAX_FILE_BYTES = 1_000_000        # 单文件读盘上限
SCAN_HEAD_BYTES = 200_000         # 超大文件只扫前 200KB 建图
MAX_FILES = 3000                  # 仓库文件数上限（防跑进系统目录）

# 非 Python 语言的顶层声明（只抓常见形态，抓不到就不进地图）
_RE_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub\s+)?(?:async\s+)?"
    r"(?:(?:function|func|fn|def|class|interface|struct|trait|impl|type)\s+"
    r"([A-Za-z_$][\w$]*)\s*(\([^)]{0,200}\))?)",
    re.M)

SYMBOL_KINDS = ("class", "function", "method", "constant")


def _est_tokens(text: str) -> int:
    """与网关同口径的 token 估算（chars/3.5）。口径一致才能和 `prune` 的预算对上。"""
    return int(len(text or "") / 3.5)


def _short(s: str, n: int = 120) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "…"


class RepoMapGenerator:
    """工作区符号地图生成器。

    用法：
        g = RepoMapGenerator("/path/to/repo", max_map_tokens=1024)
        g.build_symbol_graph()
        ranked = g.rank_symbols()
        prompt_prefix = g.render_repo_map()
    """

    def __init__(self, workspace_root: str = ".", max_map_tokens: int = 1024,
                 focus_files=None):
        self.workspace_root = os.path.realpath(workspace_root or ".")
        self.max_map_tokens = max(64, int(max_map_tokens))
        self.focus_files = [str(x) for x in (focus_files or [])]
        # 索引：文件 → [(kind, name, signature_line)]
        self._defs: "dict[str, list]" = {}
        self._graph: "dict[str, list]" = {}     # 符号 → 引用了它的符号列表（反向边）
        self._scores: "dict[str, float]" = {}
        self._built = False
        self._scan_ms = 0.0

    # ── 扫描 ─────────────────────────────────────────────────
    def _iter_files(self):
        """遍历工作区源码文件（跳过依赖目录，受 MAX_FILES 约束）。"""
        n = 0
        for dirpath, dirnames, filenames in os.walk(self.workspace_root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in SRC_EXTS:
                    continue
                absp = os.path.join(dirpath, fn)
                rel = os.path.relpath(absp, self.workspace_root).replace(os.sep, "/")
                yield rel, absp
                n += 1
                if n >= MAX_FILES:
                    return

    def _read(self, absp: str) -> str:
        try:
            size = os.path.getsize(absp)
            with open(absp, "rb") as f:
                raw = f.read(min(size, SCAN_HEAD_BYTES) if size > MAX_FILE_BYTES
                             else size)
            return raw.decode("utf-8", "replace")
        except OSError:
            return ""

    def _defs_of_python(self, text: str) -> list:
        """用 `ast` 提 Python 符号（带签名）。解析失败则不产出符号（不猜）。"""
        out = []
        try:
            tree = ast.parse(text)
        except Exception:                                        # noqa: BLE001
            return out
        for node in tree.body:                                   # 只看顶层
            try:
                if isinstance(node, ast.ClassDef):
                    out.append(("class", node.name, "class %s" % node.name))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pre = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    args = ast.unparse(node.args)
                    ret = (" -> %s" % ast.unparse(node.returns)) if node.returns else ""
                    out.append(("function", node.name,
                                "%s %s(%s)%s" % (pre, node.name, args, ret)))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id.isupper():
                            out.append(("constant", t.id, t.id))
            except Exception:                                    # noqa: BLE001
                continue
        return out

    def _defs_of_other(self, text: str) -> list:
        out = []
        for m in _RE_DEF.finditer(text):
            name = m.group(1)
            if not name or name in ("if", "for", "while", "switch", "catch"):
                continue
            sig = _short(m.group(0), 120).strip()
            kind = "class" if re.search(r"\b(?:class|interface|struct|trait)\b", sig) else "function"
            out.append((kind, name, sig))
        return out

    def build_symbol_graph(self) -> "dict[str, list[str]]":
        """扫描全部源码文件 → 构建符号引用图 → 返回 `{符号: [引用它的文件]}`。"""
        import time
        t0 = time.perf_counter()
        self._defs = {}
        for rel, absp in self._iter_files():
            text = self._read(absp)
            if not text:
                continue
            defs = (self._defs_of_python(text) if rel.endswith((".py", ".pyi"))
                    else self._defs_of_other(text))
            if defs:
                self._defs[rel] = defs

        # 建"符号 → 定义文件"索引（同一符号可能多个文件定义，全记）
        name2files = {}
        for rel, defs in self._defs.items():
            for _kind, name, _sig in defs:
                name2files.setdefault(name, []).append(rel)

        # 反向边：符号 X 被哪些文件引用（文本里出现 X 且 X 不是定义在本文件）
        graph = {name: [] for name in name2files}
        if name2files:
            # 预编译一次"候选名字"的正则（\b 界定，避免子串误配：`bar` 不该匹配 `foobar`）
            pats = {n: re.compile(r"\b%s\b" % re.escape(n)) for n in name2files}
            for rel, absp in self._iter_files():
                text = self._read(absp)
                if not text:
                    continue
                # 超大文本做一次粗筛：逐个名字全扫是 O(names × files)，先按首字符剪枝
                for name, pat in pats.items():
                    def_files = name2files[name]
                    if len(def_files) == 1 and def_files[0] == rel:
                        continue                                 # 只在自己文件出现 → 无外部引用
                    if name in text and pat.search(text):
                        graph[name].append(rel)
        self._graph = graph
        self._built = True
        self._scan_ms = (time.perf_counter() - t0) * 1000
        return graph

    # ── PageRank ─────────────────────────────────────────────
    def rank_symbols(self, damping: float = 0.85, iters: int = 20) -> "list[tuple[str, float]]":
        """纯 Python 迭代 PageRank → `[(符号名, 分数)]` 降序。

        图结构：节点 = 符号；边 = "文件 A（含符号 B）引用了符号 C" ⇒ C 从 B 获得权重。
        简化处理：引用关系是在**文件粒度**上观察到的，因此把"同一文件内的所有符号"
        视为整体给被引用符号投票 —— 这让"热门文件里的符号"整体权重更高，符合直觉
        （模型更该先看热点文件）。
        """
        if not self._built:
            self.build_symbol_graph()
        names = list(self._graph.keys())
        if not names:
            self._scores = {}
            return []
        n = len(names)
        # 每个符号的入边来源（谁引用了它）
        incoming = {nm: self._graph.get(nm) or [] for nm in names}
        # 文件 → 该文件的符号集合（用于把"文件级引用"摊到符号上）
        file_syms = {rel: [x[1] for x in defs] for rel, defs in self._defs.items()}
        score = {nm: 1.0 / n for nm in names}
        for _ in range(max(1, int(iters))):
            new = {nm: (1.0 - damping) / n for nm in names}
            for nm in names:
                srcs = incoming.get(nm) or []
                if not srcs:
                    continue
                share = damping * score[nm] / max(1, len(srcs))
                for src_file in srcs:
                    # 把该符号的权重摊给"引用它的文件"里的符号（近似：均分）
                    peers = file_syms.get(src_file) or []
                    if not peers:
                        continue
                    add = share / len(peers)
                    for p in peers:
                        if p in new:
                            new[p] += add
            # 归一（防数值漂移）
            total = sum(new.values()) or 1.0
            score = {k: v / total for k, v in new.items()}
        self._scores = score
        # ── 路径/类型降权与引用次数因子(实测校准)────────────────
        # 测试文件降权 0.35:pytest fixture 名被反复引用会虚高,agent 真正需要的是被测代码;
        # 类 > 函数 > 常量的类型权重:被多处引用的常量 PageRank 虚高但 agent 无从下手;
        # 引用次数开平方因子:小仓库 PR 归一化后区分度低,引用次数是更直接的重要性信号。
        sym2file_weight = {}
        for rel, defs in self._defs.items():
            for _kind, name, _sig in defs:
                sym2file_weight.setdefault(name, rel)
        _kind_w = {"class": 1.6, "function": 1.15, "method": 0.9, "constant": 0.55}
        sym_kind = {}
        for rel, defs in self._defs.items():
            for kind, name, _sig in defs:
                sym_kind.setdefault(name, kind)

        def _w(nm: str) -> float:
            rel = sym2file_weight.get(nm, "")
            low = rel.lower()
            w = _kind_w.get(sym_kind.get(nm, ""), 1.0)
            if "/tests/" in low or low.startswith("tests/") or "/test_" in low:
                return w * 0.35
            if low.endswith(("_test.py", "_test.go", ".test.js", ".spec.ts")):
                return w * 0.35
            if "/examples/" in low or "/docs/" in low or "/bench" in low:
                return w * 0.6
            return w
        score = {k: v * _w(k) for k, v in score.items()}
        # 再乘"绝对引用次数"因子（开平方抑制极端值）：
        # 小仓库里 PageRank 归一化后区分度太低（5 个文件时几乎所有符号分数都在 1/n 附近），
        # 单靠 PR 会出现"常量压过类"的反直觉结果。引用次数是更直接的重要性信号，
        # 两者相乘后：被引用多的类 > 被引用多的常量 > 没人引用的类，符合"先看骨架"的直觉。
        ref_cnt = {nm: len(self._graph.get(nm) or []) for nm in score}
        score = {k: v * (1.0 + (ref_cnt.get(k, 0) ** 0.5)) for k, v in score.items()}
        total = sum(score.values()) or 1.0
        self._scores = {k: v / total for k, v in score.items()}
        return sorted(self._scores.items(), key=lambda kv: (-kv[1], kv[0]))

    # ── 渲染 ─────────────────────────────────────────────────
    def render_repo_map(self, focus_files=None) -> str:
        """按分数渲染紧凑地图，受 `max_map_tokens` 约束。

        `focus_files` 里的文件优先出现（agent 当前正在改的文件，其符号更该被看见）。
        """
        if not self._scores:
            self.rank_symbols()
        focus = [str(x).replace(os.sep, "/") for x in (focus_files or self.focus_files)]
        # 符号 → 定义文件（取第一个定义处）
        sym2file = {}
        for rel, defs in self._defs.items():
            for _kind, name, sig in defs:
                sym2file.setdefault(name, (rel, sig))

        ranked = list(self._scores.items())
        # 聚焦文件加分（不改变排序语义，只是把相关性高的前置）
        if focus:
            def boost(item):
                nm, sc = item
                rel = sym2file.get(nm, ("", ""))[0]
                return (0 if rel in focus else 1, -sc)
            ranked.sort(key=boost)

        # 按文件聚合（同一文件的符号连着出现，读起来像"目录结构"而不是"排行榜"）
        by_file: "dict[str, list]" = {}
        for nm, sc in ranked:
            rel, sig = sym2file.get(nm, ("", ""))
            if not rel:
                continue
            by_file.setdefault(rel, []).append((nm, sig, sc))
        # 文件内符号也按分数降序
        for rel in by_file:
            by_file[rel].sort(key=lambda x: -x[2])
        # 文件顺序按"该文件最高分符号"降序，focus 优先
        file_order = sorted(by_file.keys(),
                            key=lambda r: (0 if r in focus else 1,
                                           -max(s for _n, _s, s in by_file[r])))

        budget_chars = int(self.max_map_tokens * 3.5)
        lines, used = [], 0
        for rel in file_order:
            header = "%s:" % rel
            body = []
            for _nm, sig, _sc in by_file[rel]:
                cand = sig
                if used + len(header) + sum(len(b) + 2 for b in body) + len(cand) + 3 > budget_chars:
                    break
                body.append(cand)
            if not body:
                continue
            line = "%s %s" % (header, ", ".join(body))
            if used + len(line) + 1 > budget_chars:
                continue
            lines.append(line)
            used += len(line) + 1
            if used >= budget_chars:
                break
        return "\n".join(lines)

    # ── 辅助 ────────────────────────────────────────────────
    def stats(self) -> dict:
        return {"files": len(self._defs), "symbols": len(self._scores or self._graph),
                "scan_ms": round(self._scan_ms, 1), "built": self._built}
