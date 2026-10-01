from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol


MARKER_START = "===== DESCRIBE THIS SYMBOL ====="
MARKER_END = "===== END SYMBOL ====="
STRIPPED_PLACEHOLDER = "    // ..."


@dataclass
class Symbol:
    name: str
    kind: str
    line: int
    end_line: int
    snippet: str
    params: str = ""
    nesting_depth: int = 0
    param_count: int = 0
    description: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    short_name: str = ""


def count_params(params_str: str) -> int:
    s = params_str.strip()
    if not s:
        return 0
    depth = 0
    count = 1
    for ch in s:
        if ch in "(<[{":
            depth += 1
        elif ch in ")>]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


@dataclass
class CallRef:
    caller_name: str
    callee_name: str


class Extractor(Protocol):
    def extract(self, path: str) -> tuple[list[Symbol], list[CallRef]]:
        ...


EXT_TO_LANG: dict[str, str] = {
    ".go": "go",
    ".cs": "cs",
    ".py": "py",
    ".js": "js",
    ".jsx": "js",
    ".ts": "js",
    ".tsx": "js",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".md": "md",
    ".markdown": "md",
}


def _insert_markers(lines: list[str], target_line: int, target_end_line: int) -> str:
    out: list[str] = []
    for i, line in enumerate(lines):
        line_num = i + 1
        if line_num == target_line:
            out.append(MARKER_START)
        out.append(line)
        if line_num == target_end_line:
            out.append(MARKER_END)
    return "\n".join(out)


def _build_stripped(lines: list[str], syms: list[Symbol], target_line: int, target_end_line: int) -> str:
    blank = [False] * len(lines)
    for sym in syms:
        is_target = sym.line == target_line
        contains_target = sym.line <= target_line and sym.end_line >= target_end_line
        if is_target or contains_target:
            continue
        j = sym.line
        while j < sym.end_line and j < len(lines):
            blank[j] = True
            j += 1

    out: list[str] = []
    in_blank_run = False
    for i, line in enumerate(lines):
        line_num = i + 1
        if line_num == target_line:
            out.append(MARKER_START)
        if blank[i]:
            if not in_blank_run:
                out.append(STRIPPED_PLACEHOLDER)
                in_blank_run = True
        else:
            in_blank_run = False
            out.append(line)
        if line_num == target_end_line:
            out.append(MARKER_END)
    return "\n".join(out)


TRIMMED_PLACEHOLDER = "    // ... (trimmed)"


def _window_around_target(text: str, max_chars: int) -> str:
    """Cut a marked-up context down to max_chars, keeping the target symbol and the lines nearest it.

    Stripping only removes other symbol bodies, so a file dense with declarations still produced contexts of
    25,000 characters, and the model server silently truncated the prompt instead.
    Here the target is kept first, then surrounding lines are added alternately after and before it.
    If the target alone exceeds the budget, its opening lines are kept, since that is where the signature is.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    lines = text.split("\n")
    try:
        start = lines.index(MARKER_START)
        end = lines.index(MARKER_END, start)
    except ValueError:
        return text[:max_chars]

    target = lines[start:end + 1]
    used = sum(len(l) + 1 for l in target)
    if used > max_chars:
        kept, size = [], 0
        for l in target[:-1]:
            if size + len(l) + 1 > max_chars - len(MARKER_END) - len(TRIMMED_PLACEHOLDER) - 2:
                break
            kept.append(l)
            size += len(l) + 1
        return "\n".join(kept + [TRIMMED_PLACEHOLDER, MARKER_END])

    lo, hi = start - 1, end + 1
    before: list[str] = []
    after: list[str] = []
    turn_after = True
    while lo >= 0 or hi < len(lines):
        take_after = hi < len(lines) and (turn_after or lo < 0)
        line = lines[hi] if take_after else lines[lo]
        if used + len(line) + 1 > max_chars:
            break
        used += len(line) + 1
        if take_after:
            after.append(line)
            hi += 1
        else:
            before.append(line)
            lo -= 1
        turn_after = not turn_after

    out = ([TRIMMED_PLACEHOLDER] if lo >= 0 else []) + before[::-1] + target + after
    if hi < len(lines):
        out.append(TRIMMED_PLACEHOLDER)
    return "\n".join(out)


def build_context(path: str, target_line: int, target_end_line: int, max_chars: int) -> str:
    """Return the file with the target symbol marked, never longer than max_chars.

    A file under max_chars is sent whole. A larger one has other symbol bodies stripped, and if that is still
    over budget it is windowed around the target.
    """
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if len(data) <= max_chars:
        return _insert_markers(lines, target_line, target_end_line)
    # File exceeds max_chars: strip other symbol bodies using tree-sitter.
    from blerk.symbols.treesitter_extractor import Extractor
    syms, _ = Extractor().extract(path)
    return _window_around_target(_build_stripped(lines, syms, target_line, target_end_line), max_chars)
