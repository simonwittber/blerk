# Changelog

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
