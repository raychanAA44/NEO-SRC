#!/usr/bin/env python3
"""S23 static readability gate for the HUB NEO UI.

Scans shipped CSS and inline style strings for:
  * font-size / font shorthand sizes below 11px (px and pt; 1pt = 4/3px at 96dpi)
  * .btn / button / select / input rules forced below the 28px interaction floor
  * stamp text shrunk below its declared size with a transform scale

Exit code 0 means the static floor holds; 1 lists every violation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "编码区" / "src" / "ui"
CSS_FILES = [UI / "assets" / "css" / "base.css", UI / "assets" / "css" / "fusion.css"]
MARKUP_FILES = [UI / "index.html"] + sorted((UI / "assets" / "js").glob("*.js"))

FONT_SIZE_RE = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)(px|pt)")
FONT_SHORTHAND_RE = re.compile(
    r"font\s*:\s*(?:(?:normal|italic|oblique|small-caps|bold|bolder|lighter|[1-9]00)\s+)*"
    r"(\d+(?:\.\d+)?)(px|pt)"
)
# 96dpi 打印/屏幕换算：1pt = 4/3px，pt 与 px 同一条 11px 红线。
PT_TO_PX = 4 / 3
INLINE_RE = re.compile(r"style\s*=\s*['\"]([^'\"]*)['\"]")
CONTROL_MIN_RE = re.compile(r"min-height\s*:\s*(\d+(?:\.\d+)?)px")
SCALE_RE = re.compile(r"transform\s*:\s*scale\(\s*(\d*\.?\d+)\s*\)")
CSS_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
CONTROL_SELECTOR_RE = re.compile(r"\.btn(?![\w-])|\bbutton\b|\bselect\b|\binput\b")


def px_of(value: str, unit: str) -> float:
    size = float(value)
    return size * PT_TO_PX if unit == "pt" else size


def check_style_text(label: str, text: str, violations: list[str]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in FONT_SIZE_RE.finditer(line):
            px = px_of(match.group(1), match.group(2))
            if px < 11:
                violations.append(
                    f"{label}:{lineno} font-size {float(match.group(1)):g}{match.group(2)} "
                    f"({px:.2f}px) < 11px"
                )
        for match in FONT_SHORTHAND_RE.finditer(line):
            px = px_of(match.group(1), match.group(2))
            if px < 11:
                violations.append(
                    f"{label}:{lineno} font shorthand {float(match.group(1)):g}{match.group(2)} "
                    f"({px:.2f}px) < 11px"
                )


def check_inline(label: str, text: str, violations: list[str]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in INLINE_RE.finditer(line):
            style = match.group(1)
            for size_match in FONT_SIZE_RE.finditer(style):
                px = px_of(size_match.group(1), size_match.group(2))
                if px < 11:
                    violations.append(
                        f"{label}:{lineno} inline font-size {float(size_match.group(1)):g}"
                        f"{size_match.group(2)} ({px:.2f}px) < 11px"
                    )
            for size_match in FONT_SHORTHAND_RE.finditer(style):
                px = px_of(size_match.group(1), size_match.group(2))
                if px < 11:
                    violations.append(
                        f"{label}:{lineno} inline font {float(size_match.group(1)):g}"
                        f"{size_match.group(2)} ({px:.2f}px) < 11px"
                    )


def check_blocks(label: str, text: str, violations: list[str]) -> None:
    """每一条规则单独验收：控件高度底线与印章文字不得被缩放。"""
    for match in CSS_BLOCK_RE.finditer(text):
        selector = " ".join(match.group(1).split())
        body = match.group(2)
        if not selector or selector.startswith("@"):
            continue
        height = CONTROL_MIN_RE.search(body)
        if height and CONTROL_SELECTOR_RE.search(selector) and float(height.group(1)) < 28:
            violations.append(
                f"{label} selector '{selector}' min-height {float(height.group(1)):g}px < 28px"
            )
        if ".stamp" in selector:
            scale = SCALE_RE.search(body)
            if scale and float(scale.group(1)) < 1:
                violations.append(
                    f"{label} selector '{selector}' scales stamp text to {float(scale.group(1)):g}x"
                )


def main() -> int:
    violations: list[str] = []
    for path in CSS_FILES:
        text = path.read_text(encoding="utf-8")
        label = str(path.relative_to(ROOT))
        check_style_text(label, text, violations)
        check_blocks(label, text, violations)
    for path in MARKUP_FILES:
        text = path.read_text(encoding="utf-8")
        check_inline(str(path.relative_to(ROOT)), text, violations)

    # The shared small-button baseline must remain at the 28px interaction floor.
    base = (UI / "assets" / "css" / "base.css").read_text(encoding="utf-8")
    small = re.search(r"\.btn\.small\s*\{([^}]*)\}", base)
    if not small:
        violations.append("base.css: missing .btn.small rule")
    else:
        height = CONTROL_MIN_RE.search(small.group(1))
        if not height or float(height.group(1)) < 28:
            violations.append("base.css: .btn.small min-height < 28px")

    if violations:
        print(f"READABILITY FAIL: {len(violations)} violation(s)")
        for item in violations:
            print("  -", item)
        return 1
    print(
        "READABILITY PASS: fonts >= 11px; buttons/forms >= 28px; "
        "no stamp text scaled below 1x"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
