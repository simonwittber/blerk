# Changelog

## [Unreleased]

### Git worktree support

Blerk now tracks git context separately from file content and skips worktree directories during indexing.

**DB schema: repository and git_files (migration 20)**

Two new tables replace the git columns on `files`. `repository` holds one row per git repo, keyed by the common git root path. `git_files` links a file to a repo and branch and carries the git metadata (`git_commit`, `git_author`, `git_enriched_at`). Files with the same content across branches or worktrees share one `files` row and get separate `git_files` rows. The old `git_*` columns on `files` remain for backward compatibility but are no longer written to.

**git_enricher**

The enricher calls `git rev-parse --git-common-dir` to resolve the worktree's `.git` file back to the shared repo root. All worktrees of the same repo map to the same `repository` row. The enricher writes git metadata to `git_files` via an upsert on `(repository_id, file_id, git_branch)`.

**watch_folder**

The scanner and file watcher now skip any directory that contains a `.git` file. A `.git` file (as opposed to a `.git` directory) marks a git worktree or submodule root.

**purge**

The `purge` command gains three new removal strategies, all enabled by default.

- `--no-missing`: skip removing files that no longer exist on disk.
- `--no-worktrees`: skip removing files whose paths fall under a non-main worktree, as reported by `git worktree list --porcelain`.
- `--no-gitignored`: skip removing files now covered by `.gitignore` patterns found by walking the watched folders.

### Hints system

Blerk now stores and surfaces short notes ("hints") tied to file-glob patterns. Hints appear automatically in `search` results when the matched files overlap with a hint's pattern, and are shown at most once per context window.

- `hint_store` saves a hint with a concept tag, a file-glob pattern, and a one or two sentence body. Wide patterns (`*`, `**`) fire on every search regardless of which files matched.
- `hint_session_reset` clears the seen-hint set so hints re-appear in the next context window. The MCP startup instructions tell Claude to call this after context compaction.
- MCP startup instructions are now read from `[hints] instructions` in `config.toml` instead of being hardcoded, and are only injected when the current working directory is inside a watched folder.
- A `PreCompact` hook enqueues the session transcript for async processing (`blerk hint-enqueue`).
- The `hint-extractor` daemon picks up queued transcripts, calls an LLM to extract candidate hints, and stores them with `source="auto"`. Configure it under `[hints.llm]` in `config.toml`.

### Knowledge refiners

The `knowledge-refiner` daemon applies configurable processing passes over stored knowledge items. Two built-in refiner types are available.

**task-filter**: Sends each item to the LLM with a prompt asking whether it is a lasting pattern or a completed one-off task. Items the LLM classifies as stale are deleted.

**fact-check**: Searches the indexed codebase for code evidence related to the claim, then asks the LLM whether that evidence supports or refutes the claim. Items the LLM marks as refuted have `suppressed_at` set and stop appearing in search results. All checked items receive a `fact_checked_at` timestamp regardless of the verdict.

Configure refiners in `[[knowledge.refiners]]` sections in `config.toml`. Each entry requires `type` and `enabled`. Set `prompt_template` to override the default LLM prompt for that refiner type.

The daemon starts when the hub detects `knowledge.llm.enabled = true`. Run a one-shot pass with `blerk knowledge-refiner` or add `--dry-run` to preview actions without writing to the database. Use `--refiner TYPE` to run only one refiner type.

### snippet_search

`query.snippet_search(conn, cfg, query_text, directory, n)` is a new public function that runs the full hybrid search pipeline and returns verbose-formatted text with code snippets. The fact-check refiner uses it to retrieve code evidence without spawning a subprocess. It accepts the same connection and config objects used throughout the codebase.

### DB schema: fact_checked_at (migration 19)

A new `fact_checked_at` column on the `knowledge` table records when the fact-check refiner last processed an item. Items where `fact_checked_at IS NULL` have not been checked. Items with both `fact_checked_at` and `suppressed_at` set were checked and found to be refuted by the code.

### Content-addressed embeddings

Embeddings are now keyed by a SHA256 hash of the block content instead of the block ID. Previously, switching git branches caused blerk to regenerate all vectors because the same source code returned with new block IDs. Now, when code reappears unchanged after a branch switch or revert, the embedder reuses the existing vector. The schema migration to v9 preserves existing vectors by joining through code_blocks to recover the content hash.

**Fix:** `summary.py` counted embedded symbols using `e.symbol_id`, a column that does not exist in the embeddings table. The query now joins through code_blocks correctly.

### Fix: vector dimension mismatch in blerk search

The search query now filters embeddings to the configured model. Previously it compared vectors from all stored models against the query vector, which caused a dimension mismatch error when the embedding model changed. Old vectors from a different model are ignored.

### blerk init: reranker configuration

`blerk init` now asks whether to enable the reranker. If enabled, it prompts for the endpoint, model, and an optional API key. These values write to the `[reranker]` section of `config.toml`.

### Bundled analyzer rules

The repo now ships `analyzers.example.toml` with a set of ready-to-use analyzer rules: `antislop`, `design`, `cache`, `queries`, `async`, `review`, and a new `csharp` analyzer. The `csharp` analyzer has one rule, `boxing`, which flags implicit boxing of value types in `.cs` files. Common patterns it detects include use of non-generic collections, value types assigned to `object` variables, structs stored in interface-typed variables, and enum values passed as `object`.

### blerk init: copy analyzers on first run

`blerk init` now copies `analyzers.example.toml` to `~/.blerk/analyzers.toml` on first run. If `analyzers.toml` already exists, it skips the copy.

### blerk similar

A new `blerk similar` command finds function-level code candidates for refactoring via the DRY (Don't Repeat Yourself) principle, using semantic embeddings to detect similar implementations.

```
blerk similar <directory> [--threshold N] [--ext EXT]
```

Scans functions in the specified directory (excluding test code) and groups semantically similar implementations into clusters. Each cluster shows member functions with their similarity distance (0=identical, 1=orthogonal). Default threshold is 0.1, targeting near-duplicates worth consolidating.

## [0.5.1] - 2026-08-09

### Fixes

**watch-folder logging**: file deletion now appears in logs. Previously, only upserted files were logged; deletions were silent.

**Queue pipeline tests**: added comprehensive test suite (`tests/test_queue_flow.py`) verifying file-event queuing, pipeline integrity, restart safety, and migration content_hash backfill correctness.

## [0.5.0] - 2026-08-09

### blerk show

A new `blerk show` command displays source code for a file or symbol directly from disk.

```
blerk show <target> [--file PATH] [--lines N]
```

`target` is resolved first as an exact symbol name, then as a file path substring. If multiple symbols share the name, all matches are shown with headers. `--file` restricts symbol lookup to a path substring. `--lines` caps the number of lines returned (default 200).

### MCP server: show tool replaces lint and antislop

The MCP server now exposes a `show` tool instead of `lint` and `antislop`. The `lint` and `antislop` tools are removed from the MCP interface; use the CLI directly for those.

`show(target, file="", lines=200)` returns the source of a file or symbol.

### Call graph accuracy

Three bugs in call-graph construction are fixed.

**Bug 1: LIKE fuzzy callee matching**

When a caller referenced a symbol not in the same file, the symbolizer looked up the callee in the database using `name LIKE '%.ShortName'` with `LIMIT 1`. With no `ORDER BY`, this picked an arbitrary symbol that happened to match the suffix. For example, a call to `ToString()` could resolve to `Unity.U2D.Physics.Extras.TestChain.ToString` — a completely unrelated type.

Fix: exact `name = ?` match only. Calls that cannot be resolved stay in `external_refs` rather than being falsely attributed.

**Bug 2: Short-name promotion ambiguity**

When a caller is indexed before its callee, the ref lands in `external_refs` with a short callee name. On the next indexing run, when the callee file is processed, the symbolizer promoted external refs by matching the short name against newly inserted symbols. If two symbols shared the same short name (e.g. two `Update()` methods in different classes), the first one promoted the ref and deleted the evidence — making a correct re-promotion impossible later.

Fix: promotion now matches `external_refs.callee_name` against the fully qualified `sym.name` only. Ambiguous short names are not promoted.

**Bug 3: Same-file method name collision**

Within a single file, `short_to_qualified` was a flat dict keyed by short method name. The last class to define `Update()` silently overwrote the earlier one, so callers in the first class got the wrong qualified name.

Fix: short names that appear more than once in the file are excluded from `short_to_qualified`. A new `class_method_to_qualified` dict (keyed by `(class_prefix, short_name)`) handles the unambiguous case where the caller and callee share a class prefix.

### C# call resolution improvements

**Class-aware bare calls**: a bare call `Update()` inside `ClassA.Run` is now resolved to `ClassA.Update` when `ClassA.Update` is defined in the same file, even if `ClassB.Update` also exists.

**Declared field type resolution**: member-access calls on typed fields are now qualified using the field's declared type. For example, `_engine.Execute()` inside a class with `private EngineA _engine` resolves to `EngineA.Execute`. The resolver reads field and parameter type annotations from the tree-sitter AST.

### Go receiver-based method call resolution

Member-access calls on the receiver variable are now qualified using the receiver's declared type. For example, `s.Process()` inside `func (s *Service) Run()` resolves to `Service.Process`. Two types with the same method name in the same file are resolved to the correct type without collision.

### analyzer: removed silent symbol fallback

When the LLM returned an unrecognised symbol name, `blerk analyze` previously fell back to the first symbol in the file. It now logs a warning and skips the finding instead.

### Migration 7: content_hash backfill

The migration from the old `snippet` column now backfills `content_hash` before dropping `snippet`. Without this, all symbols were classified as "changed" on the first restart after upgrading to 0.4.0, causing the symbolizer to rebuild all code blocks and re-queue all embeddings unnecessarily.

### watch_folder: OSError handling on hash

`upsert_file` now wraps `hash_file` in a try/except. Previously a file that disappeared between the filesystem event and the hash read would crash the watcher thread.

### watch_folder: drain pending events on shutdown

Debounced events pending at the time of graceful shutdown are now flushed synchronously before the watcher stops. Previously, file changes that arrived just before Ctrl-C could be lost.

## [0.4.0] - 2026-08-06

### CodeBlock architecture

Symbols no longer store a `snippet` column. Instead, each symbol has one or more `code_blocks` rows that hold the content. Blocks are embedded and described separately.

**What changes:**

- The `symbols` table gains a `content_hash` column (SHA-256 truncated to 16 hex chars) for efficient change detection, and loses the `snippet` column.
- A new `code_blocks` table stores `(symbol_id, block_index, content, start_line, end_line, description, described_at)`. Every symbol has at least one block.
- Two new queue tables replace the old ones: `code_block_embed_queue` and `code_block_describe_queue` reference `block_id` instead of `symbol_id`. The old `description_queue` and `embedding_queue` tables are dropped.
- The `embeddings` table now uses `block_id` instead of `symbol_id`.
- A new `code_blocks_fts` FTS5 virtual table indexes block content for BM25 content search.
- The `symbols_fts` table no longer includes the snippet column.

**Large symbol chunking:**

When a symbol's content exceeds `max_embed_chars`, the symbolizer splits it into logical blocks using the tree-sitter AST. Split points are nested function/class/method definitions. If tree-sitter produces no split points, the content is split at line boundaries. Each block is embedded and described independently.

**Three-signal hybrid search:**

`blerk query` now fuses three ranking signals via RRF:
- Vector similarity over embeddings (via `code_blocks → embeddings` join)
- BM25 over symbol names and descriptions (`symbols_fts`)
- BM25 over code block content (`code_blocks_fts`)

**Migration:**

Schema migration 7 handles existing databases automatically. It creates the new tables, rebuilds FTS, populates `code_blocks` from the old `snippet` column, migrates the `embeddings` table to use `block_id`, and drops the old queue tables. Existing embeddings are dropped and will be regenerated.

## [0.3.2] - 2026-08-05

### blerk summary: directory argument required

`blerk summary` now requires a directory argument and scopes all counts to that path, consistent with all other commands.

## [0.3.1] - 2026-08-05

### blerk summary

A new `blerk summary` command prints a project index snapshot: watch folders, file and symbol counts, embedding and description coverage, files modified in the last 7 days, and a finding count by severity.

### MCP server instructions

The MCP `initialize` response now includes an `instructions` field when the working directory is inside a watched folder.
Claude Code injects this field into context at session start.
The instruction directs Claude to run `blerk summary` for project details.

### blerk query: compact output format

The compact and verbose output formats no longer include blank lines between results.
Verbose mode packs all fields onto one header line: kind, name, path, line range, score, and description.
Snippet lines follow the header, indented by two spaces.
This reduces token usage when query output is passed to an LLM.

## [0.3.0] - 2026-08-04

### Symbolizer removes missing files from the database

When the symbolizer processes a queued file that no longer exists on disk, it deletes the file record from the database and moves on.
Previously, deleted files stayed in the database until you ran `blerk purge` manually.

### Path normalization uses os.path.realpath everywhere

All commands that accept a directory argument now resolve the path through `os.path.realpath` before use.
The config loader resolves `watch.folders` the same way at load time.
This fixes path mismatches on systems where a directory is accessed through a junction or symlink.

### Directory argument is required on all commands

All CLI commands and MCP tools now require the directory argument.
No command silently defaults to the current directory.

### Edited files move to the front of the embedding queue

When the symbolizer detects that a file has changed, it sets priority 2 on the embedding queue entries for changed and new symbols.
New files still use the default priority 1.
This means recent edits get embeddings before the background backlog.

### Add HLSL and GLSL shader language support

The symbolizer now extracts functions, structs, and call references from HLSL and GLSL shader files.

Two new packages are required: `tree-sitter-hlsl` and `tree-sitter-glsl`.

HLSL extensions indexed: `.hlsl`, `.fx`, `.fxh`, `.hlsli`

GLSL extensions indexed: `.glsl`, `.vert`, `.frag`, `.geom`, `.comp`, `.tese`, `.tesc`

Both grammars use the old tree-sitter 0.22 binary ABI. A small `ctypes` wrapper in `treesitter_extractor.py` converts the raw pointer to a PyCapsule so tree-sitter 0.23+ can accept it. This wrapper can be removed once the upstream packages publish 0.23-compatible wheels.

### antislop: removed as a standalone command

`blerk antislop` and its `confusing` alias are removed.
The antislop rule is now a standard analyzer named `antislop` with a rule named `confusing`, defined in `~/.blerk/analyzers.toml`.

Run `blerk analyze --analyzer antislop` to get the same results.
Run `blerk findings --analyzer antislop` to read stored results without re-running the LLM.
Reset with `blerk analyze --analyzer antislop --reset`.

The `[antislop]` config section in `config.toml` is no longer used.
Set `endpoint` and `model` directly in the `[[analyzers]]` block in `analyzers.toml`.

### blerk findings

A new `blerk findings` command reads stored findings from the database and prints them.
It replaces having to re-run `blerk analyze` to see results, since `blerk analyze` skips already-analyzed symbols.

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--dir PATH` | cwd | Restrict to a path substring. |
| `--ext EXT` | (all) | Filter by file extension. Repeatable. |
| `--exclude PATTERN` | (none) | Exclude paths matching a glob pattern. Repeatable. |
| `--analyzer NAME` | (all) | Filter by analyzer name. Repeatable. |
| `--rule RULE` | (all) | Filter by rule name. Repeatable. |
| `--severity error\|warning\|info` | (all) | Filter by severity. |
| `--min-confidence FLOAT` | 0.0 | Minimum confidence threshold. |
| `--output text\|json` | text | Output format. |

### Path normalization: symlink resolution

All commands that accept a `--dir` argument now resolve symlinks via `os.path.realpath` before using the path as a filter.
The watcher also resolves symlinks when storing file paths in the database.
This ensures that paths accessed through a symlinked directory match stored paths correctly.

### blerk analyze

A new `blerk analyze` command runs configurable LLM rule checks against indexed symbols.
Rules are plain text descriptions stored in `~/.blerk/analyzers.toml`.
The LLM checks each symbol against every rule in the analyzer and returns findings with a severity, a message, and a confidence score.

Findings are stored in a new `findings` table, keyed by `(symbol_id, rule_id)`.
Repeated runs skip symbols that already have a finding for the active rules.
Use `--reset` to delete existing findings and re-run from scratch.

Two new tables, `analyzers` and `analyzer_rules`, store the rule definitions.
`blerk analyze` upserts from the config file on each run.
Rules removed from the config file stay in the database as orphans until you delete them explicitly.
Renaming a rule creates a new `rule_id` and leaves old findings intact.

If you change a rule's description in the config file, the database row updates on the next run.
Existing findings were produced by the old description and are not marked stale.
Run `blerk analyze --reset --analyzer <name>` after changing a rule to get a consistent set of findings.

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--analyzer NAME` | all | Run only this analyzer. Repeatable. |
| `--dir PATH` | (all) | Restrict to a path substring. |
| `--ext EXT` | (all) | Filter by file extension. Repeatable. |
| `--rule RULE` | (all) | Run only this rule. Repeatable. |
| `--min-confidence FLOAT` | from config | Override the minimum confidence to record. |
| `--limit N` | 0 (all) | Check at most N symbols. |
| `--output text\|json` | text | Output format. |
| `--no-save` | off | Print findings but do not write to the database. |
| `--reset` | off | Delete existing findings for selected analyzers, then exit. |

### antislop: migrated to findings table

`blerk antislop` now stores results in the `findings` table instead of `symbol_tags`.
The schema migration removes all existing `confusing` and `confusing_reason` tags.
Run `blerk antislop` again to re-populate findings after the upgrade.

The `--reset` flag now deletes findings rows for the antislop rule instead of deleting tags.
The output and behavior are otherwise unchanged.

### antislop: priority ordering

`blerk antislop` now processes symbols in order of importance. Symbols with the most inbound callers go first, with function size (line count) as the tiebreaker. This ensures the `-n` budget is spent on the most-used code rather than arbitrary insertion order.

### Remove regexp symbolizer

The regexp-based symbol extractor is removed. Tree-sitter is now the only extraction engine. The `symbolizer.engine` config field is ignored and can be removed from existing configs. Files with unsupported extensions (including `.md`) return no symbols instead of falling back to regexp.

### Lint: severity scores and clone grouping

Every lint violation now carries a severity score: the ratio of the actual value to the threshold.
A function with 200 lines against a threshold of 40 scores `5.0x`.
Violations are sorted worst-first so the most egregious findings lead the output.

Exact clone groups are now reported as one violation instead of one per file.
Three files containing the same function produce a single line: `exact clone: fn (3 copies, hash ...)`.

Near-clone pairs are grouped into connected components via union-find.
A cluster of N similar functions produces one line: `near-clone group: N symbols, closest distance D`.

### New flag: --min-score

`blerk lint --min-score X` hides violations with a score below X.
Use `--min-score 2.0` to see only findings at least twice the threshold.
This reduces display noise without changing what the rules detect or raising any threshold permanently.

## 0.2.2

### Four new SRP lint rules

`wide_package` (default on, threshold 5): flags files that import from more than N distinct packages. Runs by default.

`dep_spread` (opt-in): flags files where the ratio of distinct dependency files to total symbols exceeds a percentage threshold. Enable with `--max-dep-spread N`.

`split_class` (opt-in): flags classes whose methods form two or more disconnected groups with no shared calls between them (LCOM). Enable with `--max-cohesion N`.

`mixed_abstraction` (opt-in): flags files that call into both widely-shared utility modules and leaf-level implementation modules in the same function. Enable with `--abstraction-threshold N`.

### Refactoring

`daemon_main(run_fn)` is now a helper in `blerk/daemon_util.py`.
The `main()` functions in `blerk-embed`, `blerk-git`, and `blerk-symbolize` were identical; all three now delegate to it.

`blerk_cmd/query.py` introduces a `QueryOptions` dataclass that bundles `n`, `exts`, `min_score`, `reranker`, `directory`, `tags`, `refs`, and `verbose`.
`query_symbols` and `run_query` each take one `opts` argument instead of 13 and 15 positional parameters.

`blerk_cmd/antislop.py` introduces a `Scope` dataclass that bundles `directory`, `exts`, and `excludes`.
`sweep`, `_fetch_symbols`, `_count_already_tagged`, and `reset_tags` each take one `scope` argument instead of repeating the three filter parameters.

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
