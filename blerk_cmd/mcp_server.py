from __future__ import annotations

import anyio
from mcp.server.fastmcp import FastMCP

from blerk import config, db
from blerk_cmd.browse import browse as _browse
from blerk_cmd.deps import deps as _deps
from blerk_cmd.detail import detail as _detail
from blerk_cmd.query import embed, format_compact, query_symbols, to_blob

_MAX_N = 50
_DEFAULT_N = 10
_MIN_SCORE = 0.01

_cfg: config.Config | None = None
_conn = None

mcp = FastMCP("blerk")


def _get_cfg() -> config.Config:
    global _cfg
    if _cfg is None:
        _cfg = config.load(config.default_path())
    return _cfg


def _get_conn():
    global _conn
    cfg = _get_cfg()
    if _conn is None:
        _conn = db.open_db(cfg.db.path, init_schema=False)
    else:
        try:
            _conn.execute("SELECT 1")
        except Exception:
            _conn = db.open_db(cfg.db.path, init_schema=False)
    return _conn


@mcp.tool()
async def search(
    query: str,
    directory: str = "",
    file_extensions: list[str] = [],
    n: int = _DEFAULT_N,
) -> str:
    """Search indexed source code symbols using natural language.

    Args:
        query: Natural language search query, e.g. "debounce timer reset".
        directory: Restrict results to a directory path substring, e.g. "src/rendering".
            Leave empty to search all indexed files.
        file_extensions: Restrict results by file extension, e.g. [".py"] or [".cs", ".go"].
            Leave empty to search all languages.
        n: Number of results to return. Default 10, maximum 50.
    """
    n = max(1, min(n, _MAX_N))
    cfg = _get_cfg()
    try:
        vec = await anyio.to_thread.run_sync(
            lambda: embed(cfg.embedder.endpoint, cfg.embedder.model, query)
        )
    except Exception as e:
        return f"Embedding error: {e}"
    blob = to_blob(vec)
    reranker_endpoint = cfg.reranker.endpoint if cfg.reranker.enabled else ""
    reranker_model = cfg.reranker.model if cfg.reranker.enabled else ""
    reranker_api_key = cfg.reranker.api_key if cfg.reranker.enabled else ""
    conn = _get_conn()
    results = await anyio.to_thread.run_sync(
        lambda: query_symbols(
            conn,
            blob,
            query,
            n,
            exts=list(file_extensions),
            min_score=_MIN_SCORE,
            reranker_endpoint=reranker_endpoint,
            reranker_model=reranker_model,
            reranker_api_key=reranker_api_key,
            directory=directory,
        )
    )
    return format_compact(results) or "No results found."


@mcp.tool()
def browse(
    directory: str = "",
    file_extensions: list[str] = [],
    symbols: bool = False,
) -> str:
    """List indexed source files in a directory.

    When symbols is False (the default), returns one file path per line.
    When symbols is True, returns an indented symbol tree under each file.

    Args:
        directory: Directory to scope the listing. Leave empty to list all indexed files.
        file_extensions: Restrict to specific file types, e.g. [".cs"].
        symbols: Set to True to show the full indented symbol tree for each file.
    """
    conn = _get_conn()
    result = _browse(conn, directory, list(file_extensions), symbols=symbols)
    lines = result.splitlines()
    limit = 1000 if symbols else 2000
    if len(lines) > limit:
        lines = lines[:limit]
        lines.append(f"... (truncated at {limit} lines, narrow with directory or file_extensions)")
    return "\n".join(lines)


@mcp.tool()
def detail(name: str, file_path: str = "") -> str:
    """Get full detail for a named symbol: description, snippet, callers, and callees.

    Use this after search or browse to drill into a specific symbol.

    Args:
        name: Exact symbol name, e.g. "Debouncer" or "run_query".
        file_path: Path substring to disambiguate when the same name appears in multiple files,
            e.g. "watcher.py".
    """
    conn = _get_conn()
    return _detail(conn, name, file_path)


@mcp.tool()
def deps(directory: str = "") -> str:
    """Show the file-level dependency graph for a directory.

    Returns an adjacency list: each line is a file followed by the files it imports.

    Args:
        directory: Directory to scope the graph. Leave empty to show the full graph.
    """
    conn = _get_conn()
    result = _deps(conn, directory)
    lines = result.splitlines()
    if len(lines) > 500:
        lines = lines[:500]
        lines.append("... (truncated, use directory to narrow results)")
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
