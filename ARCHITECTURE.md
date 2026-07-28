# blerk Architecture

blerk indexes source code into a SQLite database and makes it searchable via vector similarity. It is a Python port of [unirag](https://github.com/simonwittber/unirag).

## Process layout

```
blerk (hub)
├── blerk-watch      watch_folder.py   file system watcher
├── blerk-symbolize  symbolizer.py     symbol extractor
├── blerk-git        git_enricher.py   git metadata fetcher
├── blerk-describe   llm_describer.py  LLM description generator
└── blerk-embed      embedder.py       vector embedding generator
```

The hub spawns the five daemons as subprocesses. It monitors each child and restarts it on exit, with exponential backoff (1s to 60s). A child is considered stable after 30 seconds; a stable restart resets backoff to 1s.

Processes do **not** communicate with each other directly. All coordination goes through a shared SQLite database file (`~/.blerk/blerk.db`).

## Data pipeline

Work flows through four SQLite queue tables. SQL triggers fire automatically when data is written, so daemons only need to write their output and the next queue entry appears on its own.

```
File system event
      |
      v
  files table  ──trigger──>  symbol_queue  ──>  symbolizer
               ──trigger──>  git_queue     ──>  git-enricher
                                  |
                                  v
                            symbols table
                                  |
               ──trigger──>  description_queue  ──>  llm-describer
               ──trigger──>  embedding_queue    ──>  embedder
                                  |
               ──trigger──>  embedding_queue    ──>  embedder (second pass after description)
                                  |
                                  v
                           embeddings table
```

### Step by step

1. **watch-folder** detects a file create or content change (sha1 hash differs). It upserts the file into the `files` table.

2. Two SQL triggers fire on `files` insert/update:
   - `files_after_insert`: enqueues the file into `symbol_queue` and `git_queue`.
   - `files_after_update` (hash changed only): re-enqueues into `symbol_queue`.

3. **symbolizer** claims a batch from `symbol_queue`. For each file it runs either the regexp extractor (fast, no call refs) or the tree-sitter extractor (accurate, extracts call refs too). It replaces the file's `symbols` rows in a single transaction and writes `symbol_refs`.

4. **git-enricher** claims from `git_queue`. For each file it walks up to the enclosing `.git` directory, runs `git log -1 --format=%H|%an|%D`, and writes `git_commit`, `git_author`, `git_branch` back to `files`.

5. When a symbol is inserted, two triggers fire based on kind:
   - `symbols_description_insert`: fires for `function` and `method` only, enqueues into `description_queue`.
   - `symbols_embedding_insert`: fires for every kind except `heading`, enqueues into `embedding_queue`.

6. **llm-describer** claims from `description_queue`. It builds a prompt from the symbol's source context (surrounding file content with markers) and POSTs to an OpenAI-compatible `/v1/chat/completions` endpoint (Ollama, OpenAI, etc.). The response is written to `symbols.description`.

7. When `symbols.description` changes from NULL to a value, the `symbols_description_update` trigger fires and enqueues the symbol into `embedding_queue` again for a richer second-pass embedding.

8. **embedder** claims from `embedding_queue`. It builds an input string (`name[: description]\n\nsnippet`, truncated to `max_embed_chars`), POSTs to Ollama's native `/api/embeddings` endpoint, encodes the response as a little-endian float32 blob, and upserts into `embeddings(symbol_id, model)`.

## Queue mechanics

All queue tables share the same structure:

```sql
id        INTEGER PRIMARY KEY
<target>  INTEGER NOT NULL REFERENCES <parent>(id) ON DELETE CASCADE
status    TEXT    NOT NULL DEFAULT 'pending'   -- pending | processing | done | failed
priority  INTEGER NOT NULL DEFAULT 1
attempts  INTEGER NOT NULL DEFAULT 0
queued_at INTEGER NOT NULL DEFAULT (unixepoch())
error     TEXT
```

Each daemon runs this loop:

1. **Claim a batch**: `UPDATE <queue> SET status='processing' WHERE id IN (SELECT id FROM <queue> WHERE status='pending' ORDER BY priority DESC, id ASC LIMIT ?) RETURNING id, <target_col>`. This is a single atomic statement; no separate SELECT is needed.
2. **Process each row**: do the work.
3. **On success**: `mark_done` sets `status='done'`.
4. **On failure**: `requeue` increments `attempts`. If `attempts >= max_retries`, the row is marked `failed` and the daemon increments its failure counter. Otherwise the row goes back to `pending` with `priority=0` so it sinks below fresh work.
5. **On startup**: `recover_orphans` resets any `processing` rows to `pending`. This handles a crash or kill between claim and mark_done.

The `busy_timeout=5000` pragma means readers wait up to 5 seconds for the WAL writer to finish rather than erroring immediately.

## Embeddings and hybrid search

Vectors are stored as raw little-endian float32 blobs (4 bytes per dimension). The [sqlite-vec](https://github.com/asg017/sqlite-vec) extension provides `vec_distance_cosine(a, b)` which operates directly on these blobs using SIMD acceleration.

The query CLI (`blerk-query`) uses **Reciprocal Rank Fusion (RRF)** to combine two ranking signals:

- **Vector leg**: embed the query via Ollama, then rank all symbols by `vec_distance_cosine` ascending.
- **BM25 leg**: match the query text against the `symbols_fts` FTS5 virtual table (name, description, snippet), ranked by FTS5's built-in BM25.

Each symbol gets an RRF score from whichever legs it appears in:

```
score = sum(1 / (60 + rank + 1)  for each leg the symbol appears in)
```

Symbols appearing in both legs score higher than those in only one. Both legs overfetch by `5x` before fusion so that symbols near the boundary of one list are not unfairly penalized.

`heading` symbols are excluded from both legs by default. Results can be filtered to a specific file extension with `--ext .py` (repeatable). The score shown to the user is the fused RRF score (higher is better).

## Database schema summary

| Table | Purpose |
|---|---|
| `files` | One row per tracked file. Holds path, hash, mtime, size, and git metadata. |
| `symbols` | One row per extracted symbol (function, method, class, etc.) within a file. |
| `embeddings` | One row per (symbol, model) pair. Stores the float32 vector blob. |
| `symbol_refs` | Caller/callee pairs between symbols in the same or different files. |
| `symbol_queue` | Pending symbolization work per file. |
| `git_queue` | Pending git enrichment work per file. |
| `description_queue` | Pending LLM description work per symbol. |
| `embedding_queue` | Pending embedding work per symbol. |
| `daemon_status` | One row per daemon. Updated each poll cycle with queue depth, rate, ETA, and errors. |

## Symbol extraction engines

Two engines are available, selected by `symbolizer.engine` in `config.toml`:

- **regexp** (default): regex patterns per language. Fast. No call refs. Supports Go, Python, JS/TS, C, C++, C#, Markdown.
- **treesitter**: AST-based. Slower to start (parses the full file). Accurate snippet boundaries. Extracts call refs (which symbol calls which). Same language set.

For unsupported file extensions, the tree-sitter extractor falls back to the regexp engine automatically.

## Configuration

`~/.blerk/config.toml` controls all tunables. Secrets (LLM API key) live separately in `~/.blerk/secrets.toml` and are merged at load time so the main config can be checked into version control safely.

## File watching

watch-folder uses [watchdog](https://github.com/gorakhargosh/watchdog) with a single recursive observer per watched root. On Windows this uses `ReadDirectoryChangesW` natively. Events are debounced (default 100 ms) to coalesce rapid writes (e.g. editor save followed by formatter rewrite) into a single upsert.

At startup, watch-folder performs an initial recursive scan before installing the watcher, so files that changed while blerk was not running are picked up immediately. `.gitignore` files are loaded and stacked during the scan; child directories inherit parent ignore rules.
