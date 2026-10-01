# blerk Architecture

blerk indexes source code into a SQLite database and makes it searchable by vector similarity. It also provides structural lint and LLM-based code quality analysis.

## Process layout

`blerk start` is **one process**. Every daemon runs as a supervised thread inside it.

```
blerk (hub)
├── watch-folder     watch_folder.py     file system watcher (one thread per watched folder)
├── symbolizer       symbolizer.py       symbol extractor (N threads)
├── git-enricher     git_enricher.py     git metadata fetcher
├── llm-describer    llm_describer.py    LLM description generator (one thread per [[llm]] entry)
├── embedder         embedder.py         vector embedding generator
├── fingerprinter    fingerprinter.py    duplicate-detection fingerprinter
└── knowledge-*      knowledge_*.py      extractor, dedup, refiner
```

Every daemon exposes `run(cfg, shutdown, ...)` taking a `threading.Event`, which is what makes this work: they were always written as loops over a shutdown event, so no scheduling logic had to change.

`hub.supervise(name, fn, shutdown)` runs one daemon and restarts it on an unhandled exception, with exponential backoff from 1s to 60s. A daemon that stays up for 30 seconds is considered stable and resets the backoff to 1s.

The tradeoff of a single process is deliberate: a crash in a native extension (tree-sitter) takes the whole hub down rather than one child. In exchange, shutdown cannot leak anything.

### Why it is not multi-process

It used to be. The hub spawned one subprocess per daemon and supervised it, with a `CoordinatorServer` on a UDP port routing wake-ups to `CoordinatorClient`s that registered themselves through `*.worker` files on disk.

That cost more than it bought:

- **Shutdown leaked.** `blerk stop` signals the hub, but on Windows `os.kill(pid, SIGTERM)` is `TerminateProcess`, so the hub died without running cleanup and every child survived. One session accumulated **94 orphaned daemons**, including three generations of `llm-describer` writing to the database at once.
- **`db._write_lock` did nothing.** It is a module-level `threading.Lock`, so it serialized writes within a process while the real writers were separate processes relying on `busy_timeout`. In one process the lock finally means what the code says.
- **The coordinator existed only to cross process boundaries.** 176 lines of sockets, a port file, worker files and liveness checks, replaced by a registry of `threading.Event`s.

`CoordinatorServer` and `CoordinatorClient` keep their names and method signatures, so daemon loops still call `client.wait(shutdown, poll)` and `client.notify(queue)` unchanged. Only the transport is different.

### Starting and stopping

`blerk start` refuses to start when `~/.blerk/blerk.pid` names a live process, and clears the file when it names a dead one. Without that check, repeated starts stacked whole sets of daemons.

`blerk stop` writes `~/.blerk/blerk.stop`, which the hub notices on its next poll (every 5s) and shuts down cleanly. Signals are the fallback for a hub that is wedged and not polling, not the primary mechanism, because they cannot be delivered gracefully on Windows.

All persistent coordination still goes through the SQLite database (`~/.blerk/blerk.db`). Wake-ups are now in-process.

## Data pipeline

Work flows through SQLite queue tables. Each daemon writes its output only. SQL triggers create the next queue entry automatically.

```
File system event
      |
      v
  files table  ──trigger──>  symbol_queue           ──>  symbolizer
               ──trigger──>  git_queue              ──>  git-enricher
                                  |
                                  v
                     symbols + code_blocks tables
                                  |
               ──trigger──>  code_block_describe_queue  ──>  llm-describer
               ──trigger──>  code_block_embed_queue     ──>  embedder
               ──trigger──>  fingerprint_queue          ──>  fingerprinter
                                  |
                                  v
                    embeddings / fingerprints tables
```

### Step by step

1. **watch-folder** detects a file creation or content change (SHA1 hash differs). It upserts the file into the `files` table.

2. Two SQL triggers fire on `files` insert/update:
   - `files_after_insert`: enqueues the file into `symbol_queue` and `git_queue`.
   - `files_after_update` (hash changed only): re-enqueues into `symbol_queue`.

3. **symbolizer** claims a batch from `symbol_queue`. For each file it runs the tree-sitter extractor (accurate, extracts call refs). It replaces the file's `symbols` rows using `content_hash` (SHA-256[:16] of the snippet) for change detection: unchanged symbols get only position metadata updated; changed and new symbols get `code_blocks` rebuilt. The `chunk_symbol` function splits content into logical blocks using tree-sitter AST boundaries when content exceeds `max_embed_chars`, falling back to line-based splitting. Each block is a row in `code_blocks`.

4. **git-enricher** claims from `git_queue`. For each file it resolves the enclosing repository with `git rev-parse --git-common-dir`, runs `git log -1 --format=%H|%an|%D`, and upserts `git_commit`, `git_author` and `git_branch` into `git_files` keyed by `(repository_id, rel_path, git_branch)`.

5. Two triggers fire when the symbolizer inserts a `code_blocks` row:
   - `code_blocks_describe_symbol_insert`: fires only for block 0 of a `function` or `method`, enqueues into `code_block_describe_queue`. One description per symbol: a long method is split into several code blocks for embedding, and describing each block produced several near-identical descriptions of the whole method. Fields are never described, because a one-line declaration has nothing to summarise and asking for one produced padding about the enclosing class.
   - `code_blocks_embed_insert`: fires for all blocks except those with parent kind `heading`, enqueues into `code_block_embed_queue`.
   - `symbols_description_reembed`: fires when a symbol's description changes, and requeues that symbol's blocks for embedding. The describer finishes long after the embedder, so without this every vector was built before its description existed.
   - Fingerprinting is still triggered on `symbols` INSERT (for `function` and `method`).

6. **llm-describer** claims from `code_block_describe_queue`. It builds a prompt from the symbol's source context and POSTs to an OpenAI-compatible `/v1/chat/completions` endpoint. The reply goes through `tidy_description`, which flattens it to one paragraph, strips markdown, and caps it at `llm.max_description_chars`. It writes the result to `symbols.description` and to the description of the symbol's block 0.

   The context markers wrap the **whole symbol's** line range, not the block's. Using the block range meant a method split into several blocks was described from its first fragment.

   `build_context` sends the whole file when it is under `llm.max_context_chars`. A larger file has other symbol bodies stripped, and if that is still over budget, `_window_around_target` keeps the target and the lines nearest it. That last step matters: stripping keeps every non-symbol line, so a file dense with declarations still produced 25,000-character contexts, and the model server silently truncated the prompt, which can cut the target out entirely. The cap is enforced before the request rather than left to the server.

   The cap is load bearing, not cosmetic. Descriptions are appended to the embedding input, so a long formulaic answer swamps the code it describes and pulls every symbol's vector toward the same region. Asking a 7B model to "be concise" produced a median of 1,637 characters, nearly three times the size of the code being described; the prompt limit plus the hard cap brought the median to 196. A small model will not reliably obey a word limit, so the limit is enforced in code.

7. **embedder** claims from `code_block_embed_queue`. It builds an input string per block from the symbol header, the symbol's description, its callers and callees, the path, and the block content. It sends the whole claimed batch as one `/v1/embeddings` request, encodes each response as a little-endian float32 blob, and upserts into `embeddings(content_hash, model)` along with `input_hash`, a hash of the exact text embedded. If the batch request fails, each text is retried alone so one bad input cannot fail its neighbours.

   A block is skipped only when the stored vector's `input_hash` matches the text it would embed now. Matching on `content_hash` alone, as it once did, could not see that a description, a caller or a path had changed, because `content_hash` covers only the code; every such change was silently ignored, and `blerk reindex` requeued blocks only for the embedder to skip them. Every block of a symbol carries the symbol's description.

8. **fingerprinter** claims from `fingerprint_queue`. For each symbol it fetches block 0 content and computes two fingerprints:
   - `normhash`: SHA256 of the whitespace- and case-normalised content. Two functions with the same normhash are exact clones.
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

## Embeddings

blerk never loads model weights. Every embedding is an HTTP POST to an OpenAI-compatible `/v1/embeddings` endpoint, which `blerk/embedding.py` is the only place to build.

That is a deliberate constraint, not an accident of history. An in-process model is fine inside a long-lived daemon and terrible everywhere else: `blerk query` is a fresh process that exits immediately, so it reloaded the weights for one short string on every search, and every MCP search paid that again as a subprocess. Pushing the model behind a URL puts it in something that stays warm, and removes torch and its dependency tree from the install.

It also means the whole tool speaks one protocol. The describer and the reranker already used OpenAI-compatible `/v1/chat/completions`.

Ollama serves `/v1/embeddings` natively, as do vllm, LM Studio and the hosted providers. The only settings are `embedder.endpoint`, `embedder.model`, and an optional `embedder.api_key` for endpoints that need one. There is no backend switch, no device selection, and no model cache to configure.

Hardware placement belongs to the server, not to blerk. To run embeddings on CPU while a chat model uses the GPU, set `num_gpu` to 0 in an Ollama Modelfile for the embedding model.

If the endpoint is unset, unreachable, or returns an error, the failure names the endpoint and how to fix it. Nothing silently degrades, because a wrong vector space produces plausible-looking rankings that are quietly wrong.

## Hybrid search

The embedder stores vectors as raw little-endian float32 blobs (4 bytes per dimension). The [sqlite-vec](https://github.com/asg017/sqlite-vec) extension provides `vec_distance_cosine(a, b)`, which operates directly on these blobs using SIMD acceleration.

The query CLI (`blerk query`) uses **Reciprocal Rank Fusion (RRF)** to combine three ranking signals:

- **Vector leg**: embed the query through the endpoint, then rank all symbols by `vec_distance_cosine` over `embeddings → code_blocks → symbols`.
- **BM25 symbol leg**: match the query text against `symbols_fts` (name and description), ranked by FTS5's built-in BM25.
- **BM25 content leg**: match the query text against `code_blocks_fts` (block content), ranked by FTS5's built-in BM25.

Each symbol gets an RRF score from whichever legs it appears in:

```
score = sum(1 / (60 + rank + 1)  for each leg the symbol appears in)
```

Symbols that appear in multiple legs score higher. All legs fetch 20x more results before fusion.

## Reindexing

blerk provides two symmetric reindexing commands for different pipeline stages:

- **`blerk rescan`**: Re-queue files for **symbol extraction** (symbolizer daemon). Use when you want to re-parse source code for symbols (e.g., after upgrading tree-sitter language parsers).
- **`blerk reindex`**: Re-queue code blocks for **embedding** (embedder daemon). Use when you change the embedding model.

### Embedding model changes

When you change the model name, the old embeddings are stale because different models produce different vector spaces, often with different dimensions. blerk stores embeddings keyed by `(content_hash, model)` so both old and new embeddings coexist, but only the configured model's embeddings are used for search.

**Re-embedding** is required when you change `embedder.model`, for example `nomic-embed-text` to `mxbai-embed-large`.

blerk provides three ways to trigger re-embedding:

1. **During `blerk init` (automatic)**: If you reconfigure and change the embedding model, `blerk init` detects the change and offers to re-queue all blocks.

2. **`blerk reindex` command**: Re-queue specific files or directories:
   ```bash
   blerk reindex path/to/dir
   blerk reindex --all
   blerk reindex path/to/dir --ext .py --ext .go
   ```

3. **Manual SQL** (advanced):
   ```sql
   DELETE FROM code_block_embed_queue;
   INSERT INTO code_block_embed_queue(block_id, priority, queued_at)
   SELECT id, 1, unixepoch() FROM code_blocks;
   ```

After re-queuing, the embedder daemon processes the queue and populates embeddings for the new model. Old embeddings remain in the database but are unused and can be manually cleaned up with `DELETE FROM embeddings WHERE model='old_model_name'`.

## Lint

`blerk lint` runs structural rules against the indexed codebase. `build_scope` fills a shared `_lint_files` temporary table once, using the same `scope_filters` matcher as every other command. All rules join that table instead of repeating path scans of their own.

Rules include: function line count, parameter count, nesting depth, file symbol count, per-function callee count, class method count, file dependency count, DIP hints, exact clone detection, and near-clone detection.

**Duplicate detection** uses the `fingerprints` table. Exact clones share a `normhash` value across two or more files. Near-clone detection runs LSH banding over `simhash` values: with `n_bands = threshold + 1` bands, any pair within the configured Hamming distance is guaranteed to share at least one band, so no near-clones are missed.

**DIP hints** group files by module (namespace for C#, package directory for Go, file path for others) and flag modules that import a module with more inbound edges, filtered to same-language edges via the `ext` column on symbols.

**Suppression** is controlled by `.blerk` files placed in any directory. The file is TOML with a `suppress` key (list of rule names, or `["*"]` to suppress all) and an `exclude` key (glob patterns relative to the `.blerk` location). Suppression applies to the directory and all subdirectories.

## Paths and scope

Every path in the index is absolute and uses forward slashes. `watch-folder` stores them that way, so nothing downstream has to cope with two separators.

One place decides what a path string means. `blerk/paths.py` holds the mechanical helpers and depends on nothing else, so `blerk/config.py` and `blerk/ignore_match.py` can use it without importing `blerk_cmd`:

| Function | Job |
|---|---|
| `to_slash` | Forward slashes, no trailing slash. Never touches the filesystem. |
| `resolve_path` | The real absolute path when it exists on disk, otherwise the input as written. |
| `is_absolute` | True for POSIX and Windows drive-letter forms, so indexed Windows paths read as absolute on POSIX too. |
| `resolve_root` | The `watch.folders` entry containing a directory. Longest match wins for nested roots. |
| `to_relative` | A stored path with the root prefix stripped. |

`blerk_cmd/util.py` builds the SQL side on top of those. `Scope` describes what a command may look at: a directory, extensions, excludes, the index root, and a `loose` flag. `scope_filters` turns a `Scope` into WHERE fragments and parameters, and takes the column name so the same code serves `path` and a joined `f.path`. `scope_clause` formats the fragments for appending after `WHERE 1=1`.

No command writes its own directory filter. That rule exists because the codebase previously had eleven of them and no two agreed, which is what made `search` and `browse` disagree about whether `blerk` should match `blerk_cmd`.

### How a directory argument is read

An **absolute** directory matches as an anchored prefix: `path = ? OR path LIKE ? || '/%'`. Anchoring at a path boundary is what stops `blerk` matching `blerk_cmd`, and unlike a floating `%x%` it can use an index on `path`.

A **relative** directory joins to the index root, which is resolved once from the working directory. It does not join to the process working directory, so the same argument means the same thing whichever subdirectory you run from.

The directory is optional everywhere. Omitted, the scope is the whole index root.

### Widening

`scope_readings` returns the readings to try for a directory argument, most specific first:

1. Anchored to the index root. This is what a repo-relative path means.
2. Root dropped, so the argument matches any stored path ending in it. A fragment such as `Scripts` resolves here.
3. A plain substring. The only reading that can match part of a path segment.

`search`, `show` and `detail` all walk that same list and stop at the first reading that matches. `search` prints a line saying when a widened reading is what matched, so a loose hit is never mistaken for an exact scope.

When nothing matches at all, the output names both the scope searched and the index root. An empty directory and a mistyped one are then distinguishable, which they were not before.

### Output

Results print relative to the index root: `blerk_cmd/query.py:52`, not the full absolute path. That is the same string used for reading and editing files, and it costs far less context.

## Database schema summary

| Table | Purpose |
|---|---|
| `files` | One row per unique file **content**, keyed by SHA1 hash. Holds only the hash and size. Identical content anywhere in the index shares one row. |
| `file_paths` | One row per physical path on disk. Holds the absolute forward-slash `path`, `mtime`, and the `file_id` of its content. This is the only table with a `path` column, so every directory filter joins through it. |
| `repository` | One row per git repository, keyed by the common git root. |
| `git_files` | Links a file's content to a repository and branch. Holds `rel_path` (relative to the worktree root), `git_commit`, `git_author`, and `git_enriched_at`. |
| `symbols` | One row per extracted symbol. Holds name, kind, line range, content_hash, params, nesting depth, param count, description, and file extension. No snippet column. |
| `code_blocks` | One or more rows per symbol. Holds block_index, content, start/end line, and optional description. Used for embedding and LLM description. |
| `symbol_tags` | Key/value tags per symbol. Used for extractor-specific metadata. |
| `embeddings` | One row per (content_hash, model) pair. Stores the float32 vector blob and `input_hash`, a hash of the full text it was built from. |
| `fingerprints` | One row per (symbol, kind) pair. Stores `normhash` and `simhash` values for duplicate detection. |
| `symbol_refs` | Caller/callee pairs between symbols in the index. |
| `external_refs` | Calls from indexed symbols to external names not in the index. |
| `symbol_queue` | Pending symbolization work per file. |
| `git_queue` | Pending git enrichment work per file. |
| `code_block_describe_queue` | Pending LLM description work per code block. |
| `code_block_embed_queue` | Pending embedding work per code block. |
| `fingerprint_queue` | Pending fingerprinting work per symbol. |
| `daemon_status` | One row per daemon. Updated each poll cycle with queue depth, rate, ETA, and errors. |
| `schema_version` | Single-row table tracking the migration version. |

## Symbol extraction engine

blerk uses tree-sitter for all symbol extraction. The extractor is AST-based, produces accurate snippet boundaries, and extracts call refs (which symbol calls which). Supported languages: Go, Python, JS/TS, C, C++, C#. Files with unsupported extensions return no symbols.

### Call reference resolution

The extractor qualifies call targets within the same file using three strategies, applied in order:

1. **Declared-type member access** (C# only): field and parameter type annotations are read from the AST (`field_declaration` → `variable_declaration type:`). A call `_engine.Execute()` inside a class with `private EngineA _engine` resolves to `EngineA.Execute`.

2. **Receiver-based method calls** (Go only): the receiver variable and type are read from each `method_declaration`. A call `s.Process()` inside `func (s *Service) Run()` resolves to `Service.Process`, even when another type in the same file also has a `Process` method.

3. **Class-aware bare calls**: a `(class_prefix, short_name)` dict maps `(ClassA, Update)` → `ClassA.Update`. A bare call `Update()` inside `ClassA.Run` resolves to `ClassA.Update` without collision from `ClassB.Update`. Ambiguous short names (same name in multiple classes) fall through to the next strategy.

4. **Unique short-name lookup**: if a short name appears exactly once in the file, the call is qualified unconditionally.

Calls that cannot be resolved are emitted with their short name and stored in `external_refs`. Promotion to `symbol_refs` occurs when the callee is indexed and its fully qualified name matches exactly. Ambiguous short-name promotion is not attempted.

## Configuration

`~/.blerk/config.toml` controls all tunables. Secrets (LLM API key) live separately in `~/.blerk/secrets.toml`. blerk merges secrets into the config at load time. This lets you check the main config into version control safely.

## File watching

watch-folder uses [watchdog](https://github.com/gorakhargosh/watchdog) with a single recursive observer per watched root. On Windows this uses `ReadDirectoryChangesW` natively. watch-folder debounces events (default 100 ms) to coalesce rapid writes into a single upsert.

At startup, watch-folder scans all files recursively before installing the watcher. This picks up any files that changed while blerk was not running. The scan loads and stacks `.gitignore` files. Child directories inherit ignore rules from their parent directories.
