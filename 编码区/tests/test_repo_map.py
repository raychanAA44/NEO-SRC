# -*- coding: utf-8 -*-
"""编码区/tests/test_repo_map.py —— CR-3 单测(移植自 client-runtime,unittest 版)。

覆盖:
  · 符号提取(Python `ast` 带签名 / 其它语言正则)
  · 引用图构建
  · PageRank 排序合理性(被引用多的类排前、测试文件降权)
  · Token 预算截断(严格不超预算)
  · 聚焦文件优先
  · 性能（20+ 文件全流程 <100ms，预热 + 3 次取最优，见 `_timing.py`）
  · 不假装支持(语法错误的 .py 不产出错误符号;跳过依赖目录)
"""
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from _timing import best_ms  # noqa: E402
from client_runtime.repo_map import RepoMapGenerator  # noqa: E402


class RepoMapTestBase(unittest.TestCase):
    """造一个 ~60 文件的小仓库(业务码 + 测试码 + 依赖目录 + 非 Python)。"""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="rmrepo_"))
        (self.repo / "core").mkdir()
        # 一个"被广泛引用"的核心类
        (self.repo / "core" / "engine.py").write_text(textwrap.dedent('''
            class Engine:
                """核心引擎。"""
                def run(self, x: int) -> int:
                    return x

            def helper(a: str, b: int = 0) -> str:
                return a
        '''), encoding="utf-8")
        # 20 个引用 Engine 的业务文件
        for i in range(20):
            (self.repo / ("mod_%02d.py" % i)).write_text(textwrap.dedent('''
                from core.engine import Engine, helper

                def task_%d():
                    e = Engine()
                    return helper("x")
            ''' % i), encoding="utf-8")
        # 5 个测试文件(高频互相引用 fixture 名,用于验证降权)
        (self.repo / "tests").mkdir()
        for i in range(5):
            (self.repo / "tests" / ("test_m%d.py" % i)).write_text(textwrap.dedent('''
                def fixture_%d():
                    return 1

                def test_case_%d(fixture_%d):
                    assert fixture_%d == 1
            ''' % (i, i, i, i)), encoding="utf-8")
        # 依赖目录(必须被跳过)
        nm = self.repo / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("function shouldNotAppear() { return 1; }\n",
                                     encoding="utf-8")
        # 非 Python 源码
        (self.repo / "svc.go").write_text(
            "package main\n\nfunc HandleRequest(ctx Context) error {\n\treturn nil\n}\n",
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)


class TestSymbolExtraction(RepoMapTestBase):
    def test_python_symbols_with_signature(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        defs = g._defs.get("core/engine.py") or []
        names = [d[1] for d in defs]
        self.assertIn("Engine", names, "应提取到类 Engine:%s" % names)
        self.assertIn("helper", names, "应提取到函数 helper")
        sig = [d[2] for d in defs if d[1] == "helper"][0]
        self.assertIn("def helper(a: str, b: int=0) -> str", sig,
                      "签名应含参数与返回类型:%s" % sig)

    def test_non_python_symbols_extracted(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        defs = g._defs.get("svc.go") or []
        self.assertTrue(any(d[1] == "HandleRequest" for d in defs),
                        "Go 函数应被提取:%s" % defs)

    def test_syntax_error_python_yields_no_symbols(self):
        (self.repo / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        self.assertIn(g._defs.get("bad.py"), (None, []),
                      "语法错误的文件不应产出(猜测的)符号")

    def test_skip_dependency_dirs(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        self.assertFalse(any("node_modules" in rel for rel in g._defs),
                         "依赖目录必须被跳过:%s" % list(g._defs)[:5])


class TestReferenceGraph(RepoMapTestBase):
    def test_graph_has_references(self):
        g = RepoMapGenerator(str(self.repo))
        graph = g.build_symbol_graph()
        self.assertTrue(graph.get("Engine"),
                        "Engine 被 20 个文件引用,图中应有入边:%s" % graph.get("Engine"))
        self.assertGreaterEqual(len(graph["Engine"]), 15,
                                "引用 Engine 的文件数应 >= 15,实际 %d" % len(graph["Engine"]))

    def test_graph_no_self_reference(self):
        g = RepoMapGenerator(str(self.repo))
        graph = g.build_symbol_graph()
        files = graph.get("helper") or []
        self.assertNotIn("core/engine.py", files, "定义处不应把自己算成引用方")


class TestPageRank(RepoMapTestBase):
    def test_rank_puts_core_class_first(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        ranked = g.rank_symbols()
        top5 = [n for n, _s in ranked[:5]]
        self.assertIn("Engine", top5, "被 20 个文件引用的核心类应进 Top5,实际:%s" % top5)

    def test_rank_downweights_tests(self):
        """测试文件里的符号应排在业务符号之后(降权,不是排除)。"""
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        ranked = g.rank_symbols()
        order = {n: i for i, (n, _s) in enumerate(ranked)}
        if "test_case_0" in order and "Engine" in order:
            self.assertLess(order["Engine"], order["test_case_0"],
                            "业务类 Engine 应排在测试函数之前(Engine@%d vs test_case_0@%d)"
                            % (order["Engine"], order["test_case_0"]))

    def test_rank_scores_normalized(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        ranked = g.rank_symbols()
        total = sum(s for _n, s in ranked)
        self.assertAlmostEqual(total, 1.0, delta=0.01, msg="分数应归一化,实际总和 %.4f" % total)


class TestRenderAndBudget(RepoMapTestBase):
    def test_render_compact_format(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        out = g.render_repo_map()
        self.assertTrue(out, "地图不应为空")
        self.assertIn("class Engine", out, "渲染应含类签名:%s" % out[:200])
        self.assertTrue(any(line.count(":") >= 1 for line in out.splitlines()),
                        "应按 `path:` 前缀分组")

    def test_render_respects_token_budget(self):
        for budget in (128, 256, 1024):
            with self.subTest(budget=budget):
                g = RepoMapGenerator(str(self.repo), max_map_tokens=budget)
                g.build_symbol_graph()
                out = g.render_repo_map()
                est = int(len(out) / 3.5)
                self.assertLessEqual(est, budget + 5,
                                     "预算 %d token,实际约 %d token(%d 字符)" % (
                                         budget, est, len(out)))

    def test_render_truncates_not_mangles(self):
        """超预算时应**整行省略**,不能截断半个签名(半个签名会让模型照错参数改代码)。"""
        g = RepoMapGenerator(str(self.repo), max_map_tokens=64)
        g.build_symbol_graph()
        out = g.render_repo_map()
        for line in out.splitlines():
            self.assertFalse(line.rstrip().endswith("..."), "不应出现截断残留:%r" % line)
            self.assertEqual(line.count("("), line.count(")"),
                             "括号应配对(无半截签名):%r" % line)

    def test_focus_files_prioritized(self):
        g = RepoMapGenerator(str(self.repo), max_map_tokens=256)
        g.build_symbol_graph()
        g.rank_symbols()
        out = g.render_repo_map(focus_files=["core/engine.py"])
        first_line = out.splitlines()[0] if out else ""
        self.assertTrue(first_line.startswith("core/engine.py:"),
                        "聚焦文件应优先出现,实际首行:%r" % first_line)


class TestPerformance(RepoMapTestBase):
    def test_performance_under_100ms(self):
        g = RepoMapGenerator(str(self.repo))

        def pipeline():
            g.build_symbol_graph()
            g.rank_symbols()
            g.render_repo_map()

        # 断言用 CPU：全流程约 14ms，宿主被抢占时墙钟会成倍放大（实测满载下 14→157ms），
        # 而 CPU 消耗基本稳定（15.6→62.5ms），预算 100ms 仍有富余。
        cpu, wall = best_ms(pipeline)           # 预热 + 3 次最优
        n_files = len(g._defs)
        self.assertGreaterEqual(n_files, 20, "样本文件数应 >= 20,实际 %d" % n_files)
        self.assertLess(cpu, 100.0, "%d 文件全流程 cpu=%.1fms / wall=%.1fms 应 < 100ms"
                        % (n_files, cpu, wall))

    def test_stats_reports_scale(self):
        g = RepoMapGenerator(str(self.repo))
        g.build_symbol_graph()
        g.rank_symbols()
        st = g.stats()
        self.assertGreaterEqual(st["files"], 20, "统计应反映规模:%s" % st)
        self.assertGreaterEqual(st["symbols"], 20, "统计应反映规模:%s" % st)
        self.assertTrue(st["built"])


if __name__ == "__main__":
    unittest.main()
