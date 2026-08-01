from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    EXACT = "exact"
    SUFFIX = "suffix"
    PREFIX = "prefix"
    CONTAINS = "contains"
    REGEXP = "regexp"


@dataclass
class Pattern:
    kind: Kind
    value: str = ""
    re: re.Pattern | None = None
    dir_only: bool = False
    has_slash: bool = False


@dataclass
class IgnoreSet:
    dir: str
    patterns: list[Pattern] = field(default_factory=list)


_REGEX_ESCAPE = set(".()[]{}+^$|\\")


def glob_to_regex(glob: str) -> str:
    parts = ["^"]
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                parts.append(".*")
                i += 2
                continue
            parts.append("[^/]*")
        elif c == "?":
            parts.append("[^/]")
        elif c in _REGEX_ESCAPE:
            parts.append("\\")
            parts.append(c)
        else:
            parts.append(c)
        i += 1
    parts.append("$")
    return "".join(parts)


def compile_pattern(glob: str, dir_only: bool, has_slash: bool) -> Pattern:
    lower = glob.lower()
    stars = lower.count("*")
    has_special = "?" in lower or "[" in lower

    if not has_special:
        if stars == 0:
            return Pattern(kind=Kind.EXACT, value=lower, dir_only=dir_only, has_slash=has_slash)
        if stars == 1:
            idx = lower.index("*")
            if idx == 0:
                return Pattern(kind=Kind.SUFFIX, value=lower[1:], dir_only=dir_only, has_slash=has_slash)
            if idx == len(lower) - 1:
                return Pattern(kind=Kind.PREFIX, value=lower[:idx], dir_only=dir_only, has_slash=has_slash)
        if stars == 2 and lower.startswith("*") and lower.endswith("*") and len(lower) > 2:
            return Pattern(kind=Kind.CONTAINS, value=lower[1:-1], dir_only=dir_only, has_slash=has_slash)

    try:
        compiled = re.compile(glob_to_regex(lower))
    except re.error:
        return Pattern(kind=Kind.EXACT, value=lower, dir_only=dir_only, has_slash=has_slash)
    return Pattern(kind=Kind.REGEXP, re=compiled, dir_only=dir_only, has_slash=has_slash)


def match_pattern(p: Pattern, name: str, rel: str) -> bool:
    target = rel if p.has_slash else name
    if p.kind == Kind.EXACT:
        return target == p.value
    if p.kind == Kind.SUFFIX:
        return target.endswith(p.value)
    if p.kind == Kind.PREFIX:
        return target.startswith(p.value)
    if p.kind == Kind.CONTAINS:
        return p.value in target
    if p.kind == Kind.REGEXP:
        return p.re is not None and p.re.match(target) is not None
    return False


def load_ignore_file(path: str) -> list[Pattern]:
    patterns: list[Pattern] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            has_slash = "/" in line
            patterns.append(compile_pattern(line, dir_only, has_slash))
    return patterns


def to_slash(p: str) -> str:
    return p.replace("\\", "/")


def is_ignored(path: str, is_dir: bool, sets: list[IgnoreSet]) -> bool:
    name = os.path.basename(path).lower()
    for s in sets:
        try:
            rel = os.path.relpath(path, s.dir)
        except ValueError:
            continue
        rel = to_slash(rel).lower()
        parts = rel.split("/")
        for p in s.patterns:
            if p.dir_only and not is_dir:
                # Check if any ancestor directory matches the dir_only pattern.
                for i, part in enumerate(parts[:-1]):
                    ancestor_rel = "/".join(parts[: i + 1])
                    if match_pattern(p, part, ancestor_rel):
                        return True
                continue
            if match_pattern(p, name, rel):
                return True
    return False
