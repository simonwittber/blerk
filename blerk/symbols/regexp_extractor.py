from __future__ import annotations

import os
import re
from dataclasses import dataclass

from blerk.symbols.types import EXT_TO_LANG, SNIPPET_MAX_LINES, Symbol


@dataclass
class _SymbolPattern:
    kind: str
    re: re.Pattern[str]


LANG_PATTERNS: dict[str, list[_SymbolPattern]] = {
    "go": [
        _SymbolPattern("function", re.compile(r"^func\s+(?:\(\w[^)]*\)\s+)?(\w+)\s*\(")),
        _SymbolPattern("type", re.compile(r"^type\s+(\w+)\s+(?:struct|interface)")),
    ],
    "cs": [
        _SymbolPattern("class", re.compile(r"(?:^|\s)class\s+(\w+)")),
        _SymbolPattern("interface", re.compile(r"(?:^|\s)interface\s+(\w+)")),
        _SymbolPattern("struct", re.compile(r"(?:^|\s)struct\s+(\w+)")),
        _SymbolPattern("enum", re.compile(r"(?:^|\s)enum\s+(\w+)")),
        _SymbolPattern(
            "method",
            re.compile(
                r"(?:public|private|protected|internal|static|override|virtual|async)\s+[\w<>\[\]]+\s+(\w+)\s*\("
            ),
        ),
    ],
    "py": [
        _SymbolPattern("function", re.compile(r"^def\s+(\w+)\s*\(")),
        _SymbolPattern("method", re.compile(r"^\s+def\s+(\w+)\s*\(")),
        _SymbolPattern("class", re.compile(r"^class\s+(\w+)")),
    ],
    "js": [
        _SymbolPattern("function", re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(")),
        _SymbolPattern("class", re.compile(r"^(?:export\s+)?class\s+(\w+)")),
        _SymbolPattern("method", re.compile(r"^\s+(?:async\s+)?(\w+)\s*\(")),
    ],
    "c": [
        _SymbolPattern("function", re.compile(r"^[\w\s\*]+\s+(\w+)\s*\([^;]")),
        _SymbolPattern("struct", re.compile(r"^typedef\s+struct\s+\w*\s*\{|^struct\s+(\w+)\s*\{")),
    ],
    "cpp": [
        _SymbolPattern("function", re.compile(r"^[\w\s\*:<>]+\s+(\w+)\s*\([^;]")),
        _SymbolPattern("class", re.compile(r"^(?:class|struct)\s+(\w+)")),
    ],
    "md": [
        _SymbolPattern("heading", re.compile(r"^#{1,6}\s+(.+)")),
    ],
}


def find_end_brace(lines: list[str], start_idx: int) -> int:
    depth = 0
    opened = False
    cap = start_idx + SNIPPET_MAX_LINES
    if cap > len(lines):
        cap = len(lines)
    for i in range(start_idx, cap):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i + 1
    return cap


def find_end_indent(lines: list[str], start_idx: int) -> int:
    decl_line = lines[start_idx]
    decl_indent = len(decl_line) - len(decl_line.lstrip(" \t"))
    cap = start_idx + SNIPPET_MAX_LINES
    if cap > len(lines):
        cap = len(lines)
    end = start_idx + 1
    for i in range(start_idx + 1, cap):
        trimmed = lines[i].lstrip(" \t")
        if trimmed == "":
            continue
        indent = len(lines[i]) - len(trimmed)
        if indent <= decl_indent:
            break
        end = i + 1
    return end


def _heading_level(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "#":
            n += 1
        else:
            break
    if 0 < n < len(line) and line[n] == " ":
        return n
    return 0


def find_end_heading(lines: list[str], start_idx: int) -> int:
    level = _heading_level(lines[start_idx])
    cap = start_idx + SNIPPET_MAX_LINES
    if cap > len(lines):
        cap = len(lines)
    for i in range(start_idx + 1, cap):
        l = _heading_level(lines[i])
        if l > 0 and l <= level:
            return i
    return cap


def _find_end(lang: str, lines: list[str], start_idx: int) -> int:
    if lang == "py":
        return find_end_indent(lines, start_idx)
    if lang == "md":
        return find_end_heading(lines, start_idx)
    return find_end_brace(lines, start_idx)


def extract_from_lines(lang: str, lines: list[str]) -> list[Symbol]:
    patterns = LANG_PATTERNS.get(lang)
    if not patterns:
        return []
    syms: list[Symbol] = []
    for i, line in enumerate(lines):
        for p in patterns:
            m = p.re.search(line)
            if m is None:
                continue
            name = ""
            groups = m.groups()
            for g in reversed(groups):
                if g:
                    name = g
                    break
            if name == "":
                continue
            end_line = _find_end(lang, lines, i)
            snippet = "\n".join(lines[i:end_line])
            syms.append(Symbol(name=name, kind=p.kind, line=i + 1, end_line=end_line, snippet=snippet))
            break
    return syms


def extract_symbols(path: str) -> list[Symbol]:
    ext = os.path.splitext(path)[1].lower()
    lang = EXT_TO_LANG.get(ext)
    if not lang:
        return []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return extract_from_lines(lang, text.split("\n"))
