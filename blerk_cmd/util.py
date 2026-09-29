from __future__ import annotations

import os
from dataclasses import dataclass, field, replace as dc_replace

from blerk.paths import is_absolute, resolve_path, resolve_root, to_relative, to_slash

__all__ = [
    "Scope",
    "index_root",
    "is_absolute",
    "path_readings",
    "placeholders",
    "resolve_path",
    "resolve_root",
    "scope_clause",
    "scope_directory",
    "scope_filters",
    "scope_readings",
    "to_relative",
    "to_slash",
]


def placeholders(n: int) -> str:
    return ",".join("?" * n)


@dataclass
class Scope:
    """What a command is allowed to look at.

    root is the index root that a relative directory joins to.
    loose widens the directory match to a plain substring, and is only meant for a retry after an anchored match found nothing.
    """

    directory: str = ""
    exts: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    root: str = ""
    loose: bool = False


def index_root(cfg, start: str = "") -> str:
    """Return the watch folder containing start, defaulting to the working directory."""
    return resolve_root(start or os.getcwd(), cfg.watch.folders)


def scope_directory(scope: Scope) -> str:
    """Return the scope directory in the form it should be matched against stored paths."""
    if not scope.directory:
        return ""
    directory = to_slash(scope.directory)
    if is_absolute(directory):
        return resolve_path(directory)
    if scope.root:
        return to_slash(scope.root) + "/" + directory
    return directory


def scope_filters(scope: Scope, column: str = "f.path") -> tuple[list[str], list]:
    """Build the WHERE fragments and parameters that restrict a query to a scope.

    Directory matches are anchored at a path boundary, so "blerk" never matches "blerk_cmd".
    An absolute directory, or a relative one joined to a known root, matches as a prefix.
    A relative directory with no known root matches any stored path ending in it.
    """
    filters: list[str] = []
    params: list = []

    directory = scope_directory(scope)
    if directory:
        if scope.loose:
            filters.append(f"{column} LIKE ?")
            params.append(f"%{directory}%")
        elif is_absolute(directory):
            filters.append(f"({column} = ? OR {column} LIKE ?)")
            params += [directory, f"{directory}/%"]
        else:
            filters.append(f"({column} LIKE ? OR {column} LIKE ?)")
            params += [f"%/{directory}", f"%/{directory}/%"]

    if scope.exts:
        filters.append("(" + " OR ".join(f"{column} LIKE ?" for _ in scope.exts) + ")")
        params += [f"%{ext}" for ext in scope.exts]

    for pattern in scope.excludes:
        filters.append(f"{column} NOT LIKE ?")
        params.append(to_slash(pattern).replace("*", "%").replace("?", "_"))

    return filters, params


def scope_clause(scope: Scope, column: str = "f.path") -> tuple[str, list]:
    """Return the scope filters as a single fragment that can be appended after WHERE 1=1."""
    filters, params = scope_filters(scope, column)
    if not filters:
        return "", []
    return "AND " + " AND ".join(filters), params


def scope_readings(scope: Scope) -> list[Scope]:
    """Return the scopes to try for a directory argument, most specific first.

    First the argument is anchored to the index root, which is what a repo-relative path means.
    Then the root is dropped, so a fragment such as "Scripts" still resolves by matching any path ending in it.
    Last it is treated as a plain substring, which is the only reading that can match part of a path segment.
    """
    if not scope.directory or scope.loose:
        return [scope]
    readings = [scope]
    if scope.root:
        readings.append(dc_replace(scope, root=""))
    readings.append(dc_replace(scope, loose=True))
    return readings


def path_readings(path_arg: str, root: str, column: str = "f.path") -> list[tuple[str, list]]:
    """Return the SQL fragments to try for a user-supplied path, most specific first."""
    return [scope_clause(s, column) for s in scope_readings(Scope(directory=path_arg, root=root))]
