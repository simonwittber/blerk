from __future__ import annotations

import contextlib
import io
import os

from mcp.server.fastmcp import FastMCP

from blerk import config, db
from blerk_cmd.browse import browse as _browse
from blerk_cmd.query import embed, run_query, to_blob

mcp = FastMCP("blerk")

_N = 10
_MIN_SCORE = 0.01


@mcp.tool()
def search(
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
    cfg = config.load(config.default_path())
    conn = db.open_db(cfg.db.path)
    try:
        vec = embed(cfg.embedder.endpoint, cfg.embedder.model, query)
        blob = to_blob(vec)
        buf = io.StringIO()
        reranker_endpoint = cfg.reranker.endpoint if cfg.reranker.enabled else ""
        reranker_model = cfg.reranker.model if cfg.reranker.enabled else ""
        with contextlib.redirect_stdout(buf):
            run_query(conn, blob, query, _N, False, file_extensions, _MIN_SCORE,
                      reranker_endpoint=reranker_endpoint, reranker_model=reranker_model)
        result = buf.getvalue().strip()
        return result or "No results found."
    finally:
        conn.close()


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
    cfg = config.load(config.default_path())
    conn = db.open_db(cfg.db.path)
    try:
        return _browse(conn, directory, file_extensions)
    finally:
        conn.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
