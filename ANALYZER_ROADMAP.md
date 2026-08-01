# blerk analyze — LLM Pattern Detection (One-Shot CLI)

## Goal

Add a `blerk analyze` command that reads symbols from the DB, checks each one against a configurable pattern rubric using an LLM, and prints structured findings. It runs on demand and writes results back to the DB so they can be queried later.

---

## 1. Database Changes

Add one new table to the schema in `blerk/db.py`:

```sql
CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    symbol_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    rule        TEXT    NOT NULL,
    severity    TEXT    NOT NULL,  -- error | warning | info
    message     TEXT    NOT NULL,
    confidence  REAL    NOT NULL,  -- 0.0 to 1.0
    analyzed_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (symbol_id, rule)
);

CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(rule);
```

The `UNIQUE (symbol_id, rule)` constraint lets repeated runs update findings rather than duplicate them.

---

## 2. Config Changes

Add an `[analyzer]` section to `config.toml` defaults in `blerk/config.py`:

```toml
[analyzer]
min_lines     = 5        # skip symbols shorter than this
kinds         = ["function", "method"]
extensions    = []       # empty means all indexed extensions
confidence    = 0.7      # minimum confidence to record a finding
max_context_callers  = 3  # how many callers to include in context
max_context_callees  = 5  # how many callees to include in context

[[analyzer.rules]]
name     = "no_cache_key_mutation"
severity = "error"
description = """
The function builds a cache key from a value that it also mutates or that a callee mutates.
This causes stale cache reads after the mutation.
"""

[[analyzer.rules]]
name     = "unbounded_cache_growth"
severity = "warning"
description = """
The function adds items to a cache or collection with no eviction, expiry, or size limit.
This can cause unbounded memory growth at runtime.
"""

[[analyzer.rules]]
name     = "n_plus_one_query"
severity = "warning"
description = """
The function calls a data-access or query method inside a loop.
Each iteration sends a separate query to the database or network.
"""

[[analyzer.rules]]
name     = "sync_call_in_async_context"
severity = "error"
description = """
The function calls a blocking synchronous method from an async context.
This can stall the event loop or thread pool.
"""
```

---

## 3. Prompt Design

Build one prompt per symbol. The prompt has three parts.

**Part 1: Context block**

```
Symbol: {name} ({kind}) in {file_path}, line {line}

--- snippet ---
{snippet}
--- end snippet ---

Description: {description or "none"}

Callers (up to N): {comma-separated caller names or "none"}
Callees (up to N): {comma-separated callee names or "none"}
```

**Part 2: Rubric block**

List each rule as a numbered item with its name and description. Tell the LLM to check the symbol against every rule.

**Part 3: Output instruction**

```
Return a JSON array. Each item must have these fields:
  "rule"       — the rule name from the list above
  "severity"   — "error", "warning", or "info"
  "message"    — one sentence explaining the finding
  "confidence" — a number from 0.0 to 1.0

Return an empty array if no rules apply.
Return only the JSON array. No explanation. No markdown fences.
```

---

## 4. CLI Command: `blerk analyze`

Add `blerk_cmd/analyze.py`. Register it as a subcommand in `cmd/main.py`.

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--dir` | (none) | Restrict to files under this path substring |
| `--ext` | (none, repeatable) | Filter by file extension (e.g. `.cs`) |
| `--rule` | (none, repeatable) | Run only the named rules |
| `--min-confidence` | from config | Override minimum confidence |
| `--limit` | 0 (all) | Process at most N symbols |
| `--output` | `text` | Output format: `text` or `json` |
| `--no-save` | false | Print findings but do not write to DB |

### Execution flow

1. Load config and open the DB.
2. Query symbols that match `kinds`, `extensions`, `--dir`, `--ext`, and `min_lines`. Join with `files` for path and with `symbols` for description.
3. For each symbol, query `symbol_refs` to get up to `max_context_callers` callers and `max_context_callees` callees by name.
4. Build the prompt using the template in section 3.
5. POST to the LLM endpoint (same `endpoint`, `model`, `api_key` as `[llm]` config, or a separate `[analyzer]` override).
6. Parse the JSON response. Skip items below `confidence` threshold.
7. Write findings to the `findings` table (upsert on `symbol_id, rule`).
8. Print a summary after all symbols are processed.

Process symbols in batches of `batch_size` (default 10). Print progress to stderr so stdout stays clean for `--output json`.

---

## 5. Output Format

### Text (default)

```
FINDINGS  (42 symbols checked, 7 findings)

error  no_cache_key_mutation      [0.92]  Assets/Cache.cs:34  CacheManager.Set
       The method builds a key from Item.Id, which it also modifies before returning.

warning  n_plus_one_query         [0.85]  Assets/Loader.cs:78  AssetLoader.Load
         The method calls Repository.Find inside a foreach loop.
```

Group by severity (errors first), then by file path.

### JSON

Emit a JSON array of finding objects. Each object includes `rule`, `severity`, `message`, `confidence`, `symbol_name`, `file_path`, and `line`.

---

## 6. Files to Create or Change

| Path | Action |
|---|---|
| `blerk/db.py` | Add `findings` table to `SCHEMA` |
| `blerk/config.py` | Add `Analyzer` dataclass and `[analyzer]` defaults |
| `blerk_cmd/analyze.py` | New file: CLI handler |
| `cmd/main.py` | Register `analyze` subcommand |

---

## 7. Out of Scope (for now)

- A persistent `blerk-analyze` daemon with its own queue.
- A `blerk findings` query command to browse stored results.
- Per-file or per-project rule overrides.
- Streaming progress bars.
