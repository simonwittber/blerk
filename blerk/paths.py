from __future__ import annotations

import os

# Every path stored in the index is absolute and uses forward slashes.
# These helpers are the only place that decides what a path string means, so the rest of the codebase never rolls its own.


def to_slash(path: str) -> str:
    """Return the path with forward slashes and no trailing slash, without touching the filesystem."""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if len(p) > 1 and p.endswith("/") and not p.endswith(":/"):
        p = p.rstrip("/")
    return p


def resolve_path(path: str) -> str:
    """Return the real absolute path with forward slashes when the path exists on disk.

    A path that does not exist is returned as written, with slashes normalized.
    Callers that match against stored paths rely on that, because a scope argument need not name a real directory.
    """
    if not path:
        return ""
    real = os.path.realpath(path)
    if os.path.exists(real):
        return to_slash(real)
    return to_slash(path)


def is_absolute(path: str) -> bool:
    """Return True when the path is absolute in either POSIX or Windows form.

    The Windows drive-letter case is checked explicitly so that indexed Windows paths still read as absolute on POSIX.
    """
    if not path:
        return False
    return path.startswith("/") or (len(path) > 1 and path[1] == ":")


def resolve_root(start: str, folders: list[str]) -> str:
    """Return the watch folder containing start, or an empty string when start sits outside all of them.

    The longest match wins so nested watch folders resolve to the most specific one.
    """
    if not start:
        return ""
    s = resolve_path(start)
    best = ""
    for folder in folders:
        f = to_slash(folder)
        if not f:
            continue
        if (s == f or s.startswith(f + "/")) and len(f) > len(best):
            best = f
    return best


def to_relative(path: str, root: str) -> str:
    """Return the path relative to root, or unchanged when it sits outside root."""
    if not root:
        return path
    p = to_slash(path)
    r = to_slash(root)
    if p == r:
        return "."
    if p.startswith(r + "/"):
        return p[len(r) + 1:]
    return p
