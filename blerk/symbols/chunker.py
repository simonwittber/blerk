from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CodeBlock:
    content: str
    start_line: int   # 1-based, absolute
    end_line: int     # 1-based, absolute
    block_index: int  # 0-based within symbol


def chunk_symbol(
    content: str,
    start_line: int,
    end_line: int,
    max_chars: int,
    file_path: str = "",
) -> list[CodeBlock]:
    """Return 1+ CodeBlocks for a symbol.

    Returns a single block when content fits within max_chars.
    Otherwise splits at tree-sitter logical boundaries (nested defs),
    falling back to even line-based splitting.
    """
    if not content:
        return [CodeBlock("", start_line, end_line, 0)]
    if max_chars <= 0 or len(content) <= max_chars:
        return [CodeBlock(content, start_line, end_line, 0)]

    splits = _ts_boundaries(content, file_path) or _line_boundaries(content, max_chars)
    if not splits:
        return [CodeBlock(content, start_line, end_line, 0)]

    lines = content.split("\n")
    blocks: list[CodeBlock] = []
    prev = 0
    for boundary in splits:
        chunk = "\n".join(lines[prev:boundary])
        if chunk.strip():
            blocks.append(CodeBlock(
                content=chunk,
                start_line=start_line + prev,
                end_line=start_line + boundary - 1,
                block_index=len(blocks),
            ))
        prev = boundary
    tail = "\n".join(lines[prev:])
    if tail.strip():
        blocks.append(CodeBlock(
            content=tail,
            start_line=start_line + prev,
            end_line=end_line,
            block_index=len(blocks),
        ))

    if len(blocks) <= 1:
        return [CodeBlock(content, start_line, end_line, 0)]
    return blocks


def _ts_boundaries(content: str, file_path: str) -> list[int]:
    """Return 0-based line indices of logical split points via tree-sitter."""
    if not file_path:
        return []
    ext = os.path.splitext(file_path)[1].lower()
    try:
        from blerk.symbols.treesitter_extractor import _EXT_TO_LANG, _CONTAINER_TYPES
        from tree_sitter import Parser
    except ImportError:
        return []
    ld = _EXT_TO_LANG.get(ext)
    if ld is None:
        return []
    parser = Parser(ld.language)
    src = content.encode("utf-8", errors="replace")
    tree = parser.parse(src)
    if tree is None:
        return []

    boundaries: set[int] = set()

    def _walk(node, depth: int) -> None:
        if depth > 0 and node.type in _CONTAINER_TYPES:
            line = node.start_point[0]
            if line > 0:
                boundaries.add(line)
        for child in node.children:
            _walk(child, depth + 1)

    _walk(tree.root_node, 0)
    return sorted(boundaries)


def _line_boundaries(content: str, max_chars: int) -> list[int]:
    """Split at line boundaries targeting max_chars per chunk."""
    lines = content.split("\n")
    boundaries: list[int] = []
    pos = 0
    chunk_start = 0
    for i, line in enumerate(lines):
        pos += len(line) + 1
        if pos >= max_chars and i > chunk_start:
            boundaries.append(i + 1)
            chunk_start = i + 1
            pos = 0
    return boundaries
