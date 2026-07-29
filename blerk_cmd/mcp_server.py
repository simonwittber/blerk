from __future__ import annotations

import httpx
from mcp.server.fastmcp import FastMCP

from blerk import config, db
from blerk_cmd.browse import browse as _browse
from blerk_cmd.deps import deps as _deps
from blerk_cmd.detail import detail as _detail
from blerk_cmd.query import format_compact, query_symbols, to_blob

mcp = FastMCP("blerk")

_N = 10
_MIN_SCORE = 0.01

_cfg = config.load(config.default_path())


async def _embed(endpoint: str, model: str, text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            endpoint + "/api/embeddings",
            json={"model": model, "prompt": text},
        )
    if r.status_code != 200:
        raise RuntimeError(f"ollama {r.status_code}: {r.text}")
    return r.json()["embedding"]


@mcp.tool()
async def search(
    query: str,
    file_extensions: list[str] = [],
    directory: str = "",
) -> str:
    """Search indexed source code symbols using natural language.

    Args:
        query: Natural language search query, e.g. "debounce timer reset" or "database connection pool".
        file_extensions: Restrict results to specific languages by extension,
            e.g. [".py"] for Python, [".cs"] for C#, [".go"] for Go.
            Leave empty to search across all indexed languages.
        directory: Restrict results to a directory path substring, e.g. "src/rendering".
            Leave empty to search across all indexed files.
    """
    try:
        vec = await _embed(_cfg.embedder.endpoint, _cfg.embedder.model, query)
    except Exception as e:
        return f"Embedding service unavailable: {e}"
    blob = to_blob(vec)
    reranker_endpoint = _cfg.reranker.endpoint if _cfg.reranker.enabled else ""
    reranker_model = _cfg.reranker.model if _cfg.reranker.enabled else ""
    conn = db.open_db(_cfg.db.path, init_schema=False)
    try:
        results = query_symbols(conn, blob, query, _N, file_extensions, _MIN_SCORE,
                                reranker_endpoint=reranker_endpoint, reranker_model=reranker_model,
                                directory=directory)
        return format_compact(results) or "No results found."
    finally:
        conn.close()


@mcp.tool()
def browse(directory: str = "", file_extensions: list[str] = []) -> str:
    """List all indexed source files and their symbols.

    Useful for orienting within a codebase before doing targeted searches.
    When directory is omitted, lists all indexed files.

    Args:
        directory: Absolute or relative path to restrict results to.
            Leave empty to list all indexed files.
        file_extensions: Restrict to specific file types, e.g. [".py"], [".cs"].
            Leave empty to list all indexed files.
    """
    conn = db.open_db(_cfg.db.path, init_schema=False)
    try:
        result = _browse(conn, directory, file_extensions)
    finally:
        conn.close()
    lines = result.splitlines()
    if len(lines) > 500:
        lines = lines[:500]
        lines.append("... (truncated, use file_extensions or directory to narrow results)")
    return "\n".join(lines)


@mcp.tool()
def deps(directory: str = "") -> str:
    """Show the file-level dependency graph for a directory.

    Returns a plain adjacency list: each line is a file followed by the files it depends on.
    Paths are relative to the given directory. Requires treesitter engine.

    Args:
        directory: Directory to scope the graph to. Leave empty to show the full graph.
    """
    conn = db.open_db(_cfg.db.path, init_schema=False)
    try:
        result = _deps(conn, directory)
    finally:
        conn.close()
    lines = result.splitlines()
    if len(lines) > 300:
        lines = lines[:300]
        lines.append("... (truncated, use directory to narrow results)")
    return "\n".join(lines)


@mcp.tool()
def detail(name: str, file_path: str = "") -> str:
    """Get full detail for a symbol by exact name: description, snippet, callers, and callees.

    Use this after browse or search to drill into a specific symbol.

    Args:
        name: Exact symbol name, e.g. "Debouncer" or "run_query".
        file_path: Optional path substring to disambiguate when the same name
            appears in multiple files, e.g. "watcher.py".
    """
    conn = db.open_db(_cfg.db.path, init_schema=False)
    try:
        return _detail(conn, name, file_path)
    finally:
        conn.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
