# Changelog

## 0.1.2

### MCP server delegates to CLI

MCP server tools now run the `blerk` CLI as a subprocess instead of calling Python functions directly.
This removes the `anyio` dependency from the MCP server.
All MCP tools run synchronously.

### Lint MCP tool

The MCP server now exposes a `lint` tool.
It accepts `directory`, `exclude`, `max_lines`, `max_symbols`, `max_callees`, `max_params`, `max_nesting`, `unused`, and `statics` parameters.

### Confusing MCP tool

The MCP server now exposes a `confusing` tool.
It accepts `directory`, `file_extensions`, `exclude`, `n`, and `reset` parameters.

### search alias for query

`search` is now a registered CLI alias for `query`.
It appears in both the dispatch table and the help text.

## 0.1.1

### Interactive init

`blerk init` now runs interactively. It checks Ollama, lists available models, and prompts for watch folders, LLM model, embedding model, and an API key. It writes `config.toml` and `secrets.toml` directly. Pass the hidden `--dry-run` flag to print the generated files without writing them.

### Exclude patterns

`blerk lint` and `blerk confusing` both accept `--exclude PATTERN`. The flag is repeatable and accepts globs such as `*Generated*`. Matching paths are skipped.

### Confusing tags in lint output

`blerk lint` now shows a `confusing=N` count in the summary line. If any symbols carry a `confusing=true` tag, lint lists them with their reasons below the violations.

### blerk remove cleans up the database

`blerk remove <path>` removes the folder from the watch list. The hub detects the config change and deletes all DB records for that folder. Queue entries for those files are removed via cascade delete.

### blerk-confusing entry point

`blerk-confusing` is now a registered entry point in `pyproject.toml`.

## 0.1.0 - Initial release

### Commands

- `blerk` is a unified CLI dispatcher. Subcommands: `init`, `start`, `stop`, `status`, `query`, `browse`, `add`, `remove`.
- `blerk add <path>` and `blerk remove <path>` manage watched folders in the config file.
- `blerk stop` sends SIGTERM to a running hub via a PID file written by `blerk start`.
- `blerk browse` lists all indexed files and their symbols, indented to show class membership. Defaults to the current working directory.
- `blerk status` shows daemon heartbeat data and, for each watched folder, file count and percentage of symbols described and embedded.

### Search

- Hybrid BM25 + vector search with Reciprocal Rank Fusion (RRF).
- Optional re-ranking via Ollama `/api/rerank`. Configure with `[reranker]` in config.
- Results filtered by minimum RRF score to suppress low-confidence hits.
- `--ext` flag on `blerk query` restricts results to a file extension (e.g. `.py`, `.cs`). Markdown headings are excluded unless `.md` is specified.
- Query output is indented: snippet content sits under the symbol header, indented by two spaces.
- Function and method signatures appear in results (e.g. `function run_query(conn, blob, query_text, ...)`).

### Embedding

- Embedding text includes the file path and caller/callee names for richer semantic context.
- Function and method signatures are extracted by both tree-sitter and regexp extractors and stored in a `params` column on `symbols`.
- Only functions and methods receive LLM descriptions. All non-heading symbols are embedded.
- Classes, structs, and interfaces are embedded using name and description only (no snippet), avoiding context-length failures from large class bodies.
- On context-length errors from Ollama, the embedder halves the text and retries rather than requeueing.

### Multiple LLM endpoints

- `[llm]` in config can be written as `[[llm]]` (TOML array of tables) to specify multiple endpoints.
- The hub spawns one `blerk-describe` process per LLM entry, each targeting its own endpoint and model.
- Each describer instance registers under its own name in the status table (`llm-describer-0`, `llm-describer-1`, etc.).

### MCP server

- `blerk-mcp` entry point exposes `search` and `browse` tools via FastMCP.
- `search` performs hybrid vector + BM25 search and returns formatted results.
- `browse` lists symbols for the current working directory, suitable for orienting an LLM in a codebase.

### Ignore file

- Default ignore file lives at `~/.blerk/ignore` and is referenced from config via `watch.ignore_file`.
- `PackageCache/` is included in the default ignore patterns (Unity package cache).
