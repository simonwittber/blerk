from __future__ import annotations

from blerk.symbols.types import Symbol


def extract(path: str) -> list[Symbol]:
    try:
        import yaml
    except ImportError:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[Symbol] = []
    for key, value in data.items():
        key_str = str(key)
        if isinstance(value, dict) and "name" in value:
            name = str(value["name"])
            snippet = f"{key_str}: {name}"
            deps = value.get("dependencies")
            if deps is not None:
                snippet += f"\ndependencies: {deps}"
            out.append(Symbol(
                name=name,
                kind="job",
                line=1,
                end_line=1,
                snippet=snippet,
            ))
        else:
            raw = str(value)
            truncated = raw[:80] + "..." if len(raw) > 80 else raw
            out.append(Symbol(
                name=key_str,
                kind="config-key",
                line=1,
                end_line=1,
                snippet=f"{key_str}: {truncated}",
            ))
    return out
