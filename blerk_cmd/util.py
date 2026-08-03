from __future__ import annotations

import os
from dataclasses import dataclass, field


def normalize_dir(path: str) -> str:
    return os.path.realpath(path or ".")


def placeholders(n: int) -> str:
    return ",".join("?" * n)


@dataclass
class Scope:
    directory: str = ""
    exts: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


def build_path_filters(scope: Scope) -> tuple[list[str], list]:
    filters: list[str] = []
    params: list = []

    if scope.directory:
        fwd = scope.directory.replace("\\", "/").rstrip("/")
        bwd = scope.directory.replace("/", "\\").rstrip("\\")
        filters.append("(f.path LIKE ? OR f.path LIKE ? OR f.path LIKE ? OR f.path LIKE ?)")
        params += [f"%{fwd}/%", f"%{fwd}", f"%{bwd}\\%", f"%{bwd}"]

    if scope.exts:
        ext_conds = " OR ".join("f.path LIKE ?" for _ in scope.exts)
        filters.append(f"({ext_conds})")
        for ext in scope.exts:
            params.append(f"%{ext}")

    for pat in scope.excludes:
        sql = pat.replace("\\", "/").replace("*", "%").replace("?", "_")
        filters.append("f.path NOT LIKE ?")
        params.append(sql)

    return filters, params
