# blerk Architecture

blerk indexes source code into a SQLite database and makes it searchable by vector similarity. It also provides structural lint and LLM-based code quality analysis.

## Process layout

```
blerk (hub)
├── blerk-watch        watch_folder.py   file system watcher
├── blerk-symbolize    symbolizer.py     symbol extractor (N workers)
├── blerk-git          git_enricher.py   git metadata fetcher
├── blerk-describe     llm_describer.py  LLM description generator
├── blerk-embed        embedder.py       vector embedding generator
└── blerk-fingerprint  fingerprinter.py  duplicate-detection fingerprinter
```

The hub also runs a `CoordinatorServer` on a UDP port. Each daemon registers a `CoordinatorClient` with its queue name and port. When a daemon finishes a batch it sends a NOTIFY to the hub, which forwards a CHECK signal to any idle worker for that queue. Workers call `client.wait()` instead of sleeping on a fixed interval, so they wake immediately when new work arrives.

The hub spawns all daemons as subprocesses. It monitors each child and restarts it on exit, with exponential backoff (1s to 60s). The hub considers a child stable after 30 seconds. A stable restart resets backoff to 1s.

Processes do not communicate with each other directly. All coordination goes through a shared SQLite database file (`~/.blerk/blerk.db`), supplemented by the UDP coordinator signals.

## Data pipeline

Work flows through five SQLite queue tables. Each daemon writes its output only. SQL triggers create the next queue entry automatically.

```
File system event
      |
      v
  files table  ──trigger──>  symbol_queue     ──>  symbolizer
               ──trigger──>  git_queue        ──>  git-enricher
                                  |
                                  v
                            symbols table
                                  |
               ──trigger──>  description_queue  ──>  llm-describer
               ──trigger──>  embedding_queue    ──>  embedder
               ──trigger──>  fingerprint_queue  ──>  fingerprinter
                                  |
               ──trigger──>  embedding_queue    ──>  embedder (second pass after description)
                                  |
                                  v
                    embeddings / fingerprints tables
```

### Step by step

1. **watch-folder** detects a file creation or content change (SHA1 hash differs). It upserts the file into the `files` table.

2. Two SQL triggers fire on `files` insert/update:
   - `files_after_insert`: enqueues the file into `symbol_queue` and `git_queue`.
   - `files_after_update` (hash changed only): re-enqueues into `symbol_queue`.

3. **symbolizer** claims a batch from `symbol_queue`. For each file it runs either the regexp extractor (fast, no call refs) or the tree-sitter extractor (accurate, extracts call refs too). It replaces the file's `symbols` rows in a single transaction. It writes `symbol_refs` for calls to indexed symbols, and `external_refs` for calls to external names not in the index. The `ext` column on each symbol stores the file extension for same-language comparisons.

4. **git-enricher** claims from `git_queue`. For each file it walks up to the enclosing `.git` directory, runs `git log -1 --format=%H|%an|%D`, and writes `git_commit`, `git_author`, `git_branch` back to `files`.

5. Three triggers fire when the symbolizer inserts a symbol:
   - `symbols_description_insert`: fires for `function` and `method` only, enqueues into `description_queue`.
   - `symbols_embedding_insert`: fires for every kind except `heading`, enqueues into `embedding_queue`.
   - `symbols_fingerprint_insert`: fires for `function` and `method` with a non-null snippet, enqueues into `fingerprint_queue`.

6. **llm-describer** claims from `description_queue`. It builds a prompt from the symbol's source context (surrounding file content with markers) and POSTs to an OpenAI-compatible `/v1/chat/completions` endpoint (Ollama, OpenAI, etc.). It writes the response to `symbols.description`.

7. When `symbols.description` changes from NULL to a value, the `symbols_description_update` trigger fires and enqueues the symbol into `embedding_queue` again for a richer second-pass embedding.

8. **embedder** claims from `embedding_queue`. It builds an input string (`name[: description]\n\nsnippet`) and truncates it to `max_embed_chars`. It then POSTs to Ollama's native `/api/embeddings` endpoint, encodes the response as a little-endian float32 blob, and upserts into `embeddings(symbol_id, model)`.

9. **fingerprinter** claims from `fingerprint_queue`. For each symbol it computes two fingerprints from the snippet:
   - `normhash`: SHA256 of the whitespace- and case-normalised snippet. Two functions with the same normhash are exact clones.
   - `simhash`: 64-bit SimHash over character 4-grams. Used for near-duplicate detection via Hamming distance.
   Both are stored in the `fingerprints` table.

## Queue mechanics

All queue tables share the same structure:

```sql
id        INTEGER PRIMARY KEY
<target>  INTEGER NOT NULL REFERENCES <parent>(id) ON DELETE CASCADE
status    TEXT    NOT NULL DEFAULT 'pending'   -- pending | processing | failed
priority  INTEGER NOT NULL DEFAULT 1
attempts  INTEGER NOT NULL DEFAULT 0
queued_at INTEGER NOT NULL DEFAULT (unixepoch())
error     TEXT
```

Shared daemon utilities (`fmt_duration`, `setup_logging`, `beginning_of_day`, `make_shutdown`) live in `blerk/daemon_util.py`.
All daemon entry points import from there.

Each daemon runs this loop:

1. **Claim a batch**: `UPDATE <queue> SET status='processing' WHERE id IN (SELECT id FROM <queue> WHERE status='pending' ORDER BY priority DESC, id ASC LIMIT ?) RETURNING id, <target_col>`. This is a single atomic statement.
2. **Process each row**: do the work.
3. **On success**: `mark_done` deletes the row.
4. **On failure**: `requeue` increments `attempts`. If `attempts >= max_retries`, the row is marked `failed`. Otherwise the row goes back to `pending` with `priority=0` so it sinks below fresh work.
5. **On startup**: `recover_orphans` resets any `processing` rows to `pending`. This handles a crash or kill between claim and mark_done.

The `busy_timeout=30000` pragma makes readers wait up to 30 seconds for the WAL writer.

## Embeddings and hybrid search

The embedder stores vectors as raw little-endian float32 blobs (4 bytes per dimension). The [sqlite-vec](https://github.com/asg017/sqlite-vec) extension provides `vec_distance_cosine(a, b)`, which operates directly on these blobs using SIMD acceleration.

The query CLI (`blerk query`) uses **Reciprocal Rank Fusion (RRF)** to combine two ranking signals:

- **Vector leg**: embed the query via Ollama, then rank all symbols by `vec_distance_cosine` ascending.
- **BM25 leg**: match the query text against the `symbols_fts` FTS5 virtual table (name, description, snippet), ranked by FTS5's built-in BM25.

Each symbol gets an RRF score from whichever legs it appears in:

```
score = sum(1 / (60 + rank + 1)  for each leg the symbol appears in)
```

Symbols that appear in both legs score higher than those in only one. Both legs fetch 5x more results before fusion.

## Lint

`blerk lint` runs structural rules against the indexed codebase. Rules use a shared `_lint_files` temporary table that scopes queries to the requested directory and excludes. All rules join this table instead of repeating LIKE scans against `files`.

Rules include: function line count, parameter count, nesting depth, file symbol count, per-function callee count, class method count, file dependency count, DIP hints, exact clone detection, and near-clone detection.

**Duplicate detection** uses the `fingerprints` table. Exact clones share a `normhash` value across two or more files. Near-clone detection runs LSH banding over `simhash` values: with `n_bands = threshold + 1` bands, any pair within the configured Hamming distance is guaranteed to share at least one band, so no near-clones are missed.

**DIP hints** group files by module (namespace for C#, package directory for Go, file path for others) and flag modules that import a module with more inbound edges, filtered to same-language edges via the `ext` column on symbols.

**Suppression** is controlled by `.blerk` files placed in any directory. The file is TOML with a `suppress` key (list of rule names, or `["*"]` to suppress all) and an `exclude` key (glob patterns relative to the `.blerk` location). Suppression applies to the directory and all subdirectories.

## antislop

`blerk antislop` asks an LLM whether each untagged function or method looks confusing or pointless without extra context. It stores results as `symbol_tags` rows with `key='confusing'` and an optional `key='confusing_reason'`. Tagged symbols are excluded from future sweeps unless `--reset` is passed.

`--reset` clears all confusing tags under the target directory, ignoring `--ext` and `--exclude` filters, then exits. This lets you run a fresh sweep after refactoring.

## Database schema summary

| Table | Purpose |
|---|---|
| `files` | One row per tracked file. Holds path, hash, mtime, size, and git metadata. |
| `symbols` | One row per extracted symbol. Holds name, kind, line range, snippet, params, nesting depth, param count, description, and file extension. |
| `symbol_tags` | Key/value tags per symbol. Used for `confusing`, `confusing_reason`, and extractor-specific metadata. |
| `embeddings` | One row per (symbol, model) pair. Stores the float32 vector blob. |
| `fingerprints` | One row per (symbol, kind) pair. Stores `normhash` and `simhash` values for duplicate detection. |
| `symbol_refs` | Caller/callee pairs between symbols in the index. |
| `external_refs` | Calls from indexed symbols to external names not in the index. |
| `symbol_queue` | Pending symbolization work per file. |
| `git_queue` | Pending git enrichment work per file. |
| `description_queue` | Pending LLM description work per symbol. |
| `embedding_queue` | Pending embedding work per symbol. |
| `fingerprint_queue` | Pending fingerprinting work per symbol. |
| `daemon_status` | One row per daemon. Updated each poll cycle with queue depth, rate, ETA, and errors. |
| `schema_version` | Single-row table tracking the migration version. |

## Symbol extraction engines

Set `symbolizer.engine` in `config.toml` to choose an engine:

- **regexp** (default): regex patterns per language. Fast. No call refs. Supports Go, Python, JS/TS, C, C++, C#, Markdown.
- **treesitter**: AST-based. Slower to start (parses the full file). Accurate snippet boundaries. Extracts call refs (which symbol calls which). Same language set.

For unsupported file extensions, the tree-sitter extractor falls back to the regexp engine automatically.

## Configuration

`~/.blerk/config.toml` controls all tunables. Secrets (LLM API key) live separately in `~/.blerk/secrets.toml`. blerk merges secrets into the config at load time. This lets you check the main config into version control safely.

## File watching

watch-folder uses [watchdog](https://github.com/gorakhargosh/watchdog) with a single recursive observer per watched root. On Windows this uses `ReadDirectoryChangesW` natively. watch-folder debounces events (default 100 ms) to coalesce rapid writes into a single upsert.

At startup, watch-folder scans all files recursively before installing the watcher. This picks up any files that changed while blerk was not running. The scan loads and stacks `.gitignore` files. Child directories inherit ignore rules from their parent directories.
