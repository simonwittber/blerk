from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol


SNIPPET_MAX_LINES = 100

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


def build_context(path: str, target_line: int, target_end_line: int, max_chars: int) -> str:
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if len(data) <= max_chars:
        return _insert_markers(lines, target_line, target_end_line)
    from blerk.symbols.regexp_extractor import extract_from_lines
    ext = os.path.splitext(path)[1].lower()
    lang = EXT_TO_LANG.get(ext, "")
    syms = extract_from_lines(lang, lines) if lang else []
    return _build_stripped(lines, syms, target_line, target_end_line)
