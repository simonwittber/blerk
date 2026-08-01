# Changelog

## [Unreleased]

## 0.2.1

### Config section renamed: [confusing] to [antislop]

The `[confusing]` section in `~/.blerk/config.toml` is now `[antislop]`.
If you have an existing config with `[confusing]`, rename the section header to `[antislop]`.
The `endpoint`, `model`, and `api_key` fields are unchanged.

### Configurable prompts

The `[reranker]` and `[antislop]` config sections now accept a `prompt` field.
Set it to override the default prompt template sent to the LLM.
Omit the field to keep the built-in default.

### Internal refactoring

Shared daemon helpers (`fmt_duration`, `setup_logging`, `beginning_of_day`, `make_shutdown`) are extracted into `blerk/daemon_util.py`.
All daemon entry points now import from there instead of duplicating the code.
`db.write_heartbeat` now accepts a `db.Heartbeat` dataclass instead of positional arguments.
Test fixtures are consolidated in `tests/conftest.py`.

## 0.2.0

### Daemon activity logging

All daemons now log one line per processed item showing the daemon name, elapsed time, and what was processed.
For example: `symbolizer: 200ms, 12 symbols, src/Foo.py` or `embedder: 80ms, MyClass.DoThing in src/Foo.py`.
Pass `--silent` on the command line or set `silent = true` in config to suppress these lines while keeping error output.
httpx request logs are now silenced to WARNING level in all daemons regardless of the silent flag.
Shared helpers `fmt_duration` and `setup_logging` live in `blerk/daemon_util.py`.

### antislop --reset

`blerk antislop --reset` now clears all confusing tags under the current directory, regardless of `--ext` or `--exclude` filters.
It prints "All antislop tags removed." and exits without running a sweep.

### Lint rule suppression

Place a `.blerk` file in any directory to suppress lint rules for that directory and all subdirectories.
The file uses TOML format with a `suppress` key listing the rule names to silence.
Use `suppress = ["*"]` to suppress all rules under that path.
Use the `exclude` key to skip files from linting entirely.
Patterns in `exclude` are relative to the `.blerk` file location and support `*` wildcards.
This replaces the `[[lint.suppress]]` config mechanism.

### Near-clone detection uses LSH banding

The SimHash near-clone check in `blerk lint` previously compared every pair of fingerprints, which was O(n²).
It now uses locality-sensitive hashing (LSH) banding to generate only plausible candidate pairs before computing Hamming distance.
With `n_bands = threshold + 1` bands, any pair within the configured distance is guaranteed to share at least one band, so no near-clones are missed.
Performance on large codebases improves substantially.

### Ignore file fixes

The default ignore file template and the installed `~/.blerk/ignore` file previously used `**/foo/` patterns.
These were silently broken: the `**/` prefix triggers full-path matching, and the regex requires a `/` before the directory name, so top-level directories (like `.git`) were never matched.
All patterns now use plain `foo/` form, which matches by basename at any depth.
The template also adds missing Unity entries (`PackageCache/`, `~UnityDirMonSyncFile~*`, `*.sln.docstates`) and organises patterns under comments.

### Coordinator

A new `CoordinatorServer` runs in the hub and listens on a UDP port.
Each daemon creates a `CoordinatorClient` that registers its queue name and listening port with the coordinator.
When the symbolizer or LLM describer finishes a batch, it sends a `NOTIFY` message to the hub.
The hub routes a `CHECK` signal to an idle worker registered for that queue.
Workers call `client.wait()` instead of a plain sleep, so they wake immediately when new work arrives.
Configure the hub port with `coordinator.port` in config.

### SRP hints in lint

`blerk lint` now reports `wide_module` violations.
It flags files whose symbols call into more than `--max-deps` distinct other files (default: 10).
A file with many outgoing file dependencies may be doing too many things.

### ISP hints in lint

`blerk lint` now reports `fat_class` violations.
It flags classes, structs, and interfaces whose method count exceeds `--max-methods` (default: 10).

### Coordinator status in blerk status

`blerk status` now shows a coordinator row.
It reports the hub port and the number of registered workers, or "not running" if the coordinator port file is absent.

### Tests

The MCP server tests are rewritten for the raw JSON-RPC server.
A new test module covers `CoordinatorServer` and `CoordinatorClient`.
New test modules cover the `fat_class` and `wide_module` lint rules, and the coordinator status row.

### Duplicate function detection

A new `fingerprinter` daemon computes two fingerprints per function and method: `normhash` (SHA256 of the whitespace-normalised snippet) and `simhash` (64-bit SimHash over character 4-grams).
Both are stored in a new `fingerprints` table.
`blerk lint` now reports `exact_clone` (same normhash in two or more files) and `near_clone` (SimHash Hamming distance within `--max-clone-distance`, default 3).
The hub starts the fingerprinter daemon alongside the other daemons.

### DIP hints in lint

`blerk lint` now reports `dip_hint` violations.
It flags modules that may depend on lower-level modules based on inbound dependency counts.
The rule uses module-level grouping: namespace for C#, package directory for Go, file path for Python and JavaScript.
Use `--dip-threshold N` to set the minimum inbound count for a module to be considered low-level (default: 3, set -1 to disable).

### MCP server delegates to CLI

MCP server tools now run the `blerk` CLI as a subprocess instead of calling Python functions directly.
This removes the `anyio` dependency from the MCP server.
All MCP tools run synchronously.

### Lint MCP tool

The MCP server now exposes a `lint` tool.
It accepts `directory`, `exclude`, `max_lines`, `max_symbols`, `max_callees`, `max_params`, `max_nesting`, `unused`, and `statics` parameters.

### antislop MCP tool

The MCP server now exposes an `antislop` tool.
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
