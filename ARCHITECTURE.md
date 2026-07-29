# blerk Architecture

blerk indexes source code into a SQLite database and makes it searchable by vector similarity.

## Process layout

```
blerk (hub)
├── blerk-watch      watch_folder.py   file system watcher
├── blerk-symbolize  symbolizer.py     symbol extractor
├── blerk-git        git_enricher.py   git metadata fetcher
├── blerk-describe   llm_describer.py  LLM description generator
└── blerk-embed      embedder.py       vector embedding generator
```

The hub spawns the five daemons as subprocesses. It monitors each child and restarts it on exit, with exponential backoff (1s to 60s). The hub considers a child stable after 30 seconds. A stable restart resets backoff to 1s.

Processes do **not** communicate with each other directly. All coordination goes through a shared SQLite database file (`~/.blerk/blerk.db`).

## Data pipeline

Work flows through four SQLite queue tables. Each daemon writes its output only. SQL triggers create the next queue entry automatically.

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

1. **watch-folder** detects a file creation or content change (SHA1 hash differs). It upserts the file into the `files` table.

2. Two SQL triggers fire on `files` insert/update:
   - `files_after_insert`: enqueues the file into `symbol_queue` and `git_queue`.
   - `files_after_update` (hash changed only): re-enqueues into `symbol_queue`.

3. **symbolizer** claims a batch from `symbol_queue`. For each file it runs either the regexp extractor (fast, no call refs) or the tree-sitter extractor (accurate, extracts call refs too). It replaces the file's `symbols` rows in a single transaction. It writes `symbol_refs` for calls to indexed symbols, and `external_refs` for calls to external names not in the index.

4. **git-enricher** claims from `git_queue`. For each file it walks up to the enclosing `.git` directory, runs `git log -1 --format=%H|%an|%D`, and writes `git_commit`, `git_author`, `git_branch` back to `files`.

5. Two triggers fire when the symbolizer inserts a symbol:
   - `symbols_description_insert`: fires for `function` and `method` only, enqueues into `description_queue`.
   - `symbols_embedding_insert`: fires for every kind except `heading`, enqueues into `embedding_queue`.

6. **llm-describer** claims from `description_queue`. It builds a prompt from the symbol's source context (surrounding file content with markers) and POSTs to an OpenAI-compatible `/v1/chat/completions` endpoint (Ollama, OpenAI, etc.). It writes the response to `symbols.description`.

7. When `symbols.description` changes from NULL to a value, the `symbols_description_update` trigger fires and enqueues the symbol into `embedding_queue` again for a richer second-pass embedding.

8. **embedder** claims from `embedding_queue`. It builds an input string (`name[: description]\n\nsnippet`) and truncates it to `max_embed_chars`. It then POSTs to Ollama's native `/api/embeddings` endpoint, encodes the response as a little-endian float32 blob, and upserts into `embeddings(symbol_id, model)`.

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

1. **Claim a batch**: `UPDATE <queue> SET status='processing' WHERE id IN (SELECT id FROM <queue> WHERE status='pending' ORDER BY priority DESC, id ASC LIMIT ?) RETURNING id, <target_col>`. This is a single atomic statement. The daemon does not need a separate SELECT.
2. **Process each row**: do the work.
3. **On success**: `mark_done` deletes the row.
4. **On failure**: `requeue` increments `attempts`. If `attempts >= max_retries`, requeue marks the row as `failed` and the daemon increments its failure counter. Otherwise the row goes back to `pending` with `priority=0` so it sinks below fresh work.
5. **On startup**: `recover_orphans` resets any `processing` rows to `pending`. This handles a crash or kill between claim and mark_done.

The `busy_timeout=5000` pragma makes readers wait up to 5 seconds for the WAL writer to finish instead of returning an error immediately.

## Embeddings and hybrid search

The embedder stores vectors as raw little-endian float32 blobs (4 bytes per dimension). The [sqlite-vec](https://github.com/asg017/sqlite-vec) extension provides `vec_distance_cosine(a, b)`, which operates directly on these blobs using SIMD acceleration.

The query CLI (`blerk-query`) uses **Reciprocal Rank Fusion (RRF)** to combine two ranking signals:

- **Vector leg**: embed the query via Ollama, then rank all symbols by `vec_distance_cosine` ascending.
- **BM25 leg**: match the query text against the `symbols_fts` FTS5 virtual table (name, description, snippet), ranked by FTS5's built-in BM25.

Each symbol gets an RRF score from whichever legs it appears in:

```
score = sum(1 / (60 + rank + 1)  for each leg the symbol appears in)
```

Symbols that appear in both legs score higher than those in only one. Both legs fetch 5x more results before fusion. This prevents symbols near the boundary of one leg from losing score unfairly.

Both legs exclude `heading` symbols by default. Use `--ext .py` (repeatable) to filter results to a specific file extension. The CLI shows the fused RRF score. Higher is better.

## Database schema summary

| Table | Purpose |
|---|---|
| `files` | One row per tracked file. Holds path, hash, mtime, size, and git metadata. |
| `symbols` | One row per extracted symbol (function, method, class, etc.) within a file. |
| `embeddings` | One row per (symbol, model) pair. Stores the float32 vector blob. |
| `symbol_refs` | Caller/callee pairs between symbols in the same or different files. |
| `external_refs` | Calls from indexed symbols to external names (library functions, APIs) not in the index. |
| `symbol_queue` | Pending symbolization work per file. |
| `git_queue` | Pending git enrichment work per file. |
| `description_queue` | Pending LLM description work per symbol. |
| `embedding_queue` | Pending embedding work per symbol. |
| `daemon_status` | One row per daemon. Updated each poll cycle with queue depth, rate, ETA, and errors. |

## Symbol extraction engines

Set `symbolizer.engine` in `config.toml` to choose an engine:

- **regexp** (default): regex patterns per language. Fast. No call refs. Supports Go, Python, JS/TS, C, C++, C#, Markdown.
- **treesitter**: AST-based. Slower to start (parses the full file). Accurate snippet boundaries. Extracts call refs (which symbol calls which). Same language set.

For unsupported file extensions, the tree-sitter extractor falls back to the regexp engine automatically.

## Configuration

`~/.blerk/config.toml` controls all tunables. Secrets (LLM API key) live separately in `~/.blerk/secrets.toml`. blerk merges secrets into the config at load time. This lets you check the main config into version control safely.

## File watching

watch-folder uses [watchdog](https://github.com/gorakhargosh/watchdog) with a single recursive observer per watched root. On Windows this uses `ReadDirectoryChangesW` natively. watch-folder debounces events (default 100 ms) to coalesce rapid writes (for example, an editor save followed by a formatter rewrite) into a single upsert.

At startup, watch-folder scans all files recursively before installing the watcher. This picks up any files that changed while blerk was not running. The scan loads and stacks `.gitignore` files. Child directories inherit ignore rules from their parent directories.
