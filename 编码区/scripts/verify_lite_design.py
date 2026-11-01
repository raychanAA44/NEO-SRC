#!/usr/bin/env python3
"""KWSA lite 面板的「破落科技风」静态设计闸门。

姊妹脚本：`verify_readability.py` 管 HUB NEO 主控台（外链 CSS）；
本脚本管内嵌 <style> 的单文件面板（lite 的 index.html / login.html）。

扫描项（全部对应 `设计库/03-设计风格/3.8-破落科技风/01-破落科技风设计规范.md`）：

  1. 字号红线 §3.3——任何 font-size / font 简写 / clamp 下限 < 11px（px、pt、rem 统一折算）
  2. 交互热区 §3.3——button / input / select / textarea / .btn 的 min-height < 28px
  3. 零圆角 §4.1——border-radius 取了非 0 值
  4. 语义令牌 §2.1——`:root` 之外出现硬编码十六进制色值
  5. 零彩色 §1.2——色值跨越 40 级色度（接近红/绿/蓝等语义色）
  6. 离线自持 §3.1——引用外部 http(s) 字体、图片、样式

用法：
    python 编码区/scripts/verify_lite_design.py
    python 编码区/scripts/verify_lite_design.py --dir ../NEO LITE   # 验交付件

退出码：0 = 全部达标；1 = 有违规（逐条打印 文件:行号）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB = ROOT / "编码区" / "src" / "kwsa_lite" / "web"
# 交付件里的面板位置（build_lite.py 的落点），--dir 指到交付根时用得到。
DELIVERY_WEB = Path("编码区") / "src" / "kwsa_lite" / "web"

FONT_FLOOR = 11.0          # §3.3 L0 绝对下限
CONTROL_FLOOR = 28.0       # §3.3 L3 最小可点击高度
CHROMA_LIMIT = 40          # max(r,g,b) - min(r,g,b) 超过即视为彩色

PT_TO_PX = 4 / 3
REM_TO_PX = 16.0

STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
CSS_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
INLINE_STYLE_RE = re.compile(r"style\s*=\s*['\"]([^'\"]*)['\"]")
FONT_SIZE_RE = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)(px|pt|rem)")
FONT_SHORT_RE = re.compile(
    r"font\s*:\s*(?:(?:normal|italic|oblique|small-caps|bold|bolder|lighter|[1-9]00)\s+)*"
    r"(\d+(?:\.\d+)?)(px|pt|rem)"
)
CLAMP_RE = re.compile(r"clamp\(\s*(\d+(?:\.\d+)?)(px|pt|rem)\s*,")
MIN_HEIGHT_RE = re.compile(r"min-height\s*:\s*(\d+(?:\.\d+)?)(px|pt|rem)")
RADIUS_RE = re.compile(r"border-radius\s*:\s*([^;]+)")
HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
CONTROL_SELECTOR_RE = re.compile(r"\bbutton\b|\binput\b|\bselect\b|\btextarea\b|\.btn(?![A-Za-z0-9_-])")
EXTERNAL_RE = re.compile(
    r"(?:src|href)\s*=\s*['\"]https?://|@import\s+(?:url\()?['\"]?https?://|"
    r"url\(\s*['\"]?https?://",
    re.I,
)


def px_of(value: str, unit: str) -> float:
    size = float(value)
    if unit == "pt":
        return size * PT_TO_PX
    if unit == "rem":
        return size * REM_TO_PX
    return size


def expand_hex(raw: str) -> tuple[int, int, int] | None:
    """把 3/4/6/8 位十六进制统一成 (r, g, b)；解析不了返回 None。"""
    if len(raw) in (3, 4):
        raw = "".join(ch * 2 for ch in raw[:3])
    elif len(raw) in (6, 8):
        raw = raw[:6]
    else:
        return None
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return None


def lineno_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def check_sizes(label: str, text: str, violations: list[str], offset: int = 0) -> None:
    """字号红线：font-size、font 简写、clamp 下限三者都要过 11px。"""
    for regex, kind in ((FONT_SIZE_RE, "font-size"), (FONT_SHORT_RE, "font 简写"),
                        (CLAMP_RE, "clamp 下限")):
        for match in regex.finditer(text):
            px = px_of(match.group(1), match.group(2))
            if px < FONT_FLOOR:
                violations.append(
                    f"{label}:{lineno_of(text, offset + match.start())} {kind} "
                    f"{float(match.group(1)):g}{match.group(2)} ({px:.2f}px) < {FONT_FLOOR:g}px"
                )


def check_blocks(label: str, css: str, violations: list[str], offset: int = 0) -> None:
    """逐条规则验收：交互热区、零圆角、令牌纪律、零彩色。"""
    for match in CSS_BLOCK_RE.finditer(css):
        selector = " ".join(match.group(1).split())
        body = match.group(2)
        if not selector or selector.startswith("@"):
            continue
        where = f"{label}:{lineno_of(css, offset + match.start())} selector '{selector}'"

        height = MIN_HEIGHT_RE.search(body)
        if height and CONTROL_SELECTOR_RE.search(selector):
            px = px_of(height.group(1), height.group(2))
            if px < CONTROL_FLOOR:
                violations.append(f"{where} min-height {px:g}px < {CONTROL_FLOOR:g}px")

        radius = RADIUS_RE.search(body)
        if radius:
            value = radius.group(1).strip().split()[0]
            if not value.startswith("0"):
                violations.append(f"{where} border-radius {value}（规范要求零圆角）")

        if selector.strip() == ":root":
            continue
        for hex_match in HEX_RE.finditer(body):
            raw = hex_match.group(1)
            rgb = expand_hex(raw)
            if rgb is None:
                continue
            violations.append(f"{where} 硬编码色值 #{raw}（应走语义令牌）")
            if max(rgb) - min(rgb) > CHROMA_LIMIT:
                violations.append(f"{where} 彩色值 #{raw}（规范锁定黑白灰银四色域）")


def scan_file(path: Path, label: str, violations: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for block in STYLE_BLOCK_RE.finditer(text):
        css = block.group(1)
        # 偏移到 <style> 内容起点，行号才对得上源文件而不是 CSS 片段。
        offset = block.start(1)
        check_sizes(label, css, violations, offset)
        check_blocks(label, css, violations, offset)
    # style 块之外只剩内联 style：换成等量空行，既保住行号又不重复计数。
    stripped = STYLE_BLOCK_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), text)
    check_sizes(label, stripped, violations)
    for match in EXTERNAL_RE.finditer(text):
        violations.append(
            f"{label}:{lineno_of(text, match.start())} 引用外部资源 "
            f"（{match.group(0)[:40]}…）；规范要求离线自持"
        )


def collect(web_dir: Path) -> list[Path]:
    if not web_dir.is_dir():
        return []
    return sorted(web_dir.glob("*.html"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KWSA lite 面板设计闸门")
    parser.add_argument("--dir", default=None,
                        help="交付根目录（如 ../NEO LITE）；默认验源码面板")
    args = parser.parse_args(argv)

    if args.dir:
        base = Path(args.dir).resolve()
        web_dir = base / DELIVERY_WEB
    else:
        base = ROOT
        web_dir = DEFAULT_WEB

    files = collect(web_dir)
    if not files:
        print(f"[gate] 找不到面板文件: {web_dir}", file=sys.stderr)
        return 2

    violations: list[str] = []
    for path in files:
        try:
            label = str(path.relative_to(base))
        except ValueError:
            label = str(path)
        scan_file(path, label, violations)

    if violations:
        print(f"LITE DESIGN FAIL: {len(violations)} 条违规")
        for item in violations:
            print("  -", item)
        return 1

    print(
        f"LITE DESIGN PASS: {len(files)} 个面板文件 · 字号 >= {FONT_FLOOR:g}px · "
        f"热区 >= {CONTROL_FLOOR:g}px · 零圆角 · 零硬编码色值 · 零外部资源"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
