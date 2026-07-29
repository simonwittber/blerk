from __future__ import annotations

import json

from blerk.symbols.types import Symbol


def extract(path: str) -> list[Symbol]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    name = data.get("displayName") or data.get("name", "")
    if not name:
        return []
    description = data.get("description", "")
    keywords = ", ".join(data.get("keywords", []))
    snippet_parts = [f"package: {data.get('name', name)}"]
    if description:
        snippet_parts.append(description)
    if keywords:
        snippet_parts.append(f"keywords: {keywords}")
    return [Symbol(
        name=name,
        kind="package",
        line=1,
        end_line=1,
        snippet="\n".join(snippet_parts),
        description=description,
    )]
