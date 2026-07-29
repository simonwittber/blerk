from __future__ import annotations

import os

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
_conn = db.open_db(_cfg.db.path)


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
) -> str:
    """Search indexed source code symbols using natural language.

    Args:
        query: Natural language search query, e.g. "debounce timer reset" or "database connection pool".
        file_extensions: Restrict results to specific languages by extension,
            e.g. [".py"] for Python, [".cs"] for C#, [".go"] for Go.
            Leave empty to search across all indexed languages.
    """
    vec = await _embed(_cfg.embedder.endpoint, _cfg.embedder.model, query)
    blob = to_blob(vec)
    reranker_endpoint = _cfg.reranker.endpoint if _cfg.reranker.enabled else ""
    reranker_model = _cfg.reranker.model if _cfg.reranker.enabled else ""
    results = query_symbols(_conn, blob, query, _N, file_extensions, _MIN_SCORE,
                            reranker_endpoint=reranker_endpoint, reranker_model=reranker_model)
    return format_compact(results) or "No results found."


@mcp.tool()
def browse(directory: str = "", file_extensions: list[str] = []) -> str:
    """List all indexed source files and their symbols.

    Useful for orienting within a codebase before doing targeted searches.
    When directory is omitted, restricts to the current working directory.

    Args:
        directory: Absolute or relative path to restrict results to.
            Defaults to the current working directory.
        file_extensions: Restrict to specific file types, e.g. [".py"], [".cs"].
            Leave empty to list all indexed files.
    """
    if not directory:
        directory = os.getcwd()
    return _browse(_conn, directory, file_extensions)


@mcp.tool()
def deps(directory: str = "") -> str:
    """Show the file-level dependency graph for a directory.

    Returns a plain adjacency list: each line is a file followed by the files it depends on.
    Paths are relative to the given directory. Requires treesitter engine.

    Args:
        directory: Directory to scope the graph to. Defaults to the current working directory.
    """
    if not directory:
        directory = os.getcwd()
    return _deps(_conn, directory)


@mcp.tool()
def detail(name: str, file_path: str = "") -> str:
    """Get full detail for a symbol by exact name: description, snippet, callers, and callees.

    Use this after browse or search to drill into a specific symbol.

    Args:
        name: Exact symbol name, e.g. "Debouncer" or "run_query".
        file_path: Optional path substring to disambiguate when the same name
            appears in multiple files, e.g. "watcher.py".
    """
    return _detail(_conn, name, file_path)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
