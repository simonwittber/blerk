# blerk - a local RAG for source code

blerk indexes source code into a local SQLite database and lets you search it with natural language. It watches folders, extracts symbols, adds git metadata, generates LLM descriptions, and stores vector embeddings for semantic search.

It also provides tools to counter slop: AI coding assistants generate code quickly, but that code can be long, repetitive, deeply nested, or just confusing without additional context. blerk gives you two tools to find and track these problems before they accumulate.

- **`blerk lint`** checks every indexed function against structural rules: line count, parameter count, nesting depth, duplicate detection, and several design hints. It uses a per-directory `.blerk` config file to suppress known false positives.
- **`blerk antislop`** asks an LLM whether each function looks confusing or pointless without extra context. It tags results in the index so you can query or filter on them later.

Run both regularly as you accept AI-generated code to keep the codebase from drifting toward unmaintainable complexity.

## Why blerk exists

The primary use case is as a context source for AI coding assistants via the MCP server.

An AI assistant that reads a codebase directly must scan directories, open files, and grep for symbols. This uses context window space fast and returns raw file content that the model must parse and reason about.

blerk inverts this. blerk pre-extracts, pre-describes, and pre-embeds all symbols before the assistant asks. When the assistant needs to understand something, it calls `search` or `browse`. It receives a compact list of relevant symbols with descriptions and signatures. No file reads, no grep, no directory traversal in the prompt.

This matters most for large codebases where the assistant cannot fit all the relevant code into context at once. Instead of reading ten files to find the right function, the assistant issues one search query and gets the answer directly. The assistant can use the saved context for actual reasoning.

The MCP tools blerk exposes are:

- `search` - semantic + keyword hybrid search over all indexed symbols. Returns the most relevant functions, methods, and types for a query.
- `browse` - lists all symbols in a directory with their signatures and line ranges. Useful for orienting in an unfamiliar package before doing targeted searches.
- `lint` - runs lint rules against the indexed codebase and returns violations.
- `antislop` - tags functions that look confusing or pointless.


## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally (for embeddings and optional LLM descriptions)


## Install

```
pip install .
```

This puts `blerk`, `blerk-query`, and the daemon entry points on your PATH.

## Setup

Run `blerk init` to configure blerk interactively.

```
blerk init
```

It checks whether Ollama is reachable and lists available models. It then asks for one or more watch folders, an LLM model, an embedding model, and an API key if the endpoint requires one. It writes `~/.blerk/config.toml` and `~/.blerk/secrets.toml`. If the embedding model is not available locally, it prints the `ollama pull` command to fetch it.

## Configure

Create `~/.blerk/config.toml`. At minimum, set the folders to index:

```toml
[watch]
folders = [
    "~/git/myproject",
]
```

Run `blerk` once with no config to generate a default file, or copy and edit the example below.

### Full example config

```toml
secrets_file = "~/.blerk/secrets.toml"

[db]
path = "~/.blerk/blerk.db"

[watch]
folders = ["~/git/myproject"]
debounce_ms = 100

[symbolizer]
engine = "treesitter"   # or "regexp" for faster, less accurate extraction
batch_size = 10
poll_ms = 1000
max_retries = 3
min_describe_lines = 5  # skip LLM description for symbols shorter than this
workers = 4             # number of parallel symbolizer processes

[git_enricher]
batch_size = 20
poll_ms = 2000
max_retries = 3

[llm]
endpoint = "http://localhost:11434"
model = "llama3.2"
batch_size = 5
poll_ms = 3000
max_retries = 3
max_context_chars = 16000
prompt_template = """Describe the following {kind} named "{name}" from {path}. Be concise and technical.

{context}"""

[embedder]
endpoint = "http://localhost:11434"
model = "nomic-embed-text"
batch_size = 10
poll_ms = 2000
max_retries = 3
max_embed_chars = 8000
vector_dim = 768

[reranker]
endpoint = "http://localhost:11434"
model = ""
enabled = false
# prompt = "Rank these code symbols by relevance to: ..."  # optional: override the default prompt

[antislop]
endpoint = "http://localhost:11434"
model = ""
api_key = ""
# prompt = "Does this {kind} look confusing...?"  # optional: override the default prompt
```

### LLM API key

If your LLM endpoint requires a key, put it in `~/.blerk/secrets.toml` (keep this out of version control):

```toml
[llm]
api_key = "sk-..."
```

## Pull the embedding model

```
ollama pull nomic-embed-text
```

Pull your chosen LLM model to generate descriptions:

```
ollama pull llama3.2
```

## Run

```
blerk
```

This starts the hub, which manages all daemons. On first run it scans your watched folders and indexes them. Leave it running in the background.

To index once and exit without watching for changes:

```
blerk-watch --scan
```

## MCP server

To use blerk as a context source for Claude or another MCP-compatible assistant, add it to your MCP config:

```json
{
  "mcpServers": {
    "blerk": {
      "command": "blerk-mcp",
      "args": []
    }
  }
}
```

Once connected, the assistant can call:

- `search(query, directory="", file_extensions=[], n=10)` — find symbols by meaning
- `browse(directory="", file_extensions=[], symbols=False)` — list files or symbols in a directory
- `detail(name, file_path="")` — show description, snippet, callers, and callees for a named symbol
- `deps(directory="")` — show the file-level dependency graph
- `lint(directory="", ...)` — run lint rules and return violations
- `antislop(directory="", n=50, reset=False, ...)` — tag confusing or pointless functions

The assistant decides when to call these based on what it needs. No prompting required.

## Query

```
blerk query "how does the debouncer work"
```

Options:

| Flag | Default | Description |
|---|---|---|
| `-n N` | 10 | Number of results to return |
| `--dir PATH` | (all) | Restrict to a directory path substring |
| `--ext EXT` | (all) | Restrict to a file extension, e.g. `--ext .py`. Repeatable. |
| `--tag KEY=VALUE` | (all) | Filter by symbol tag, e.g. `--tag visibility=public`. Repeatable. |
| `--refs` | off | Show callers and callees for each result |
| `--verbose` | off | Show full output with snippets and scores |
| `--config PATH` | `~/.blerk/config.toml` | Path to config file |

### Example output

```
[1] function Debouncer.add
path: /home/user/git/myproject/watcher.py
lines: 45-58
score: 0.941
snippet:
def add(self, path, event):
    with self._lock:
        self._pending[path] = event
        ...

[2] method Debouncer._fire
path: /home/user/git/myproject/watcher.py
lines: 60-68
score: 0.887
```

## Lint

```
blerk lint [--dir PATH] [--exclude PATTERN]
```

Checks every indexed function and method against structural rules. Violations are printed with file, line, rule name, and a short description.

| Rule | Flag | Default | Description |
|---|---|---|---|
| `long_function` | `--max-lines N` | 40 | Function body exceeds N lines |
| `god_file` | `--max-symbols N` | 20 | File contains more than N symbols |
| `too_many_params` | `--max-params N` | 4 | Function has more than N parameters |
| `deep_nesting` | `--max-nesting N` | 3 | Function nesting depth exceeds N |
| `high_fan_out` | `--max-callees N` | 8 | Function calls more than N distinct others |
| `fat_class` | `--max-methods N` | 10 | Class or struct has more than N methods |
| `wide_module` | `--max-deps N` | 10 | File calls into more than N other files |
| `exact_clone` | `--max-clone-distance N` | 3 | Function body is identical to one in another file |
| `near_clone` | `--max-clone-distance N` | 3 | Function body is nearly identical (SimHash distance) |
| `dip_hint` | `--dip-threshold N` | 3 | Module depends on a lower-level module |
| `unused_symbol` | `--unused` | off | Function has no callers (opt-in) |
| `static_symbol` | `--statics` | off | Symbol is static (opt-in) |

### Suppression with .blerk files

Place a `.blerk` file in any directory to control lint behaviour for that directory and all subdirectories. The file uses TOML format.

```toml
# Suppress specific rules
suppress = ["long_function", "too_many_params"]

# Suppress all rules
suppress = ["*"]

# Exclude files from linting entirely (* wildcard supported)
exclude = ["*.generated.py", "migrations/*"]
```

## antislop

```
blerk antislop [--dir PATH] [--ext EXT] [-n N]
```

Asks an LLM whether each untagged function looks confusing or pointless without additional context. Tags results as `confusing=true` or `confusing=false` in the index. The `blerk lint` output includes a confusing count at the end of each run.

To clear all tags and start fresh:

```
blerk antislop --reset
```

This removes all confusing tags under the current directory regardless of `--ext` or `--exclude` filters.

## Symbol extraction engines

blerk offers two extraction engines, set by `symbolizer.engine` in config:

- **regexp** (default): fast regex-based extraction. No call-graph data. Good for quick setup.
- **treesitter**: AST-based. Accurate snippet boundaries and caller/callee relationships. Supports Go, Python, JS/TS, C, C++, C#. Falls back to regexp for other file types.

## Daemons

The hub manages background processes. You can also run them individually:

| Command | Role |
|---|---|
| `blerk-watch` | Watches folders, hashes files, upserts into DB |
| `blerk-symbolize` | Extracts symbols from changed files (runs as N workers, set by `symbolizer.workers`) |
| `blerk-git` | Enriches files with git commit/author/branch |
| `blerk-describe` | Calls LLM to generate symbol descriptions |
| `blerk-embed` | Generates vector embeddings via Ollama |
| `blerk-fingerprint` | Computes normhash and SimHash fingerprints for duplicate detection |

Each daemon writes a heartbeat row to the `daemon_status` table every poll cycle, including queue depth, rate, and ETA.

## Commands

| Command | Description |
|---|---|
| `blerk query "..."` | Search indexed symbols with natural language |
| `blerk browse [--dir PATH]` | List symbols in a directory |
| `blerk detail <name>` | Show full detail for a symbol by exact name |
| `blerk deps [--dir PATH]` | Show the file-level dependency graph |
| `blerk lint [--dir PATH] [--exclude PATTERN]` | Check functions against lint rules |
| `blerk antislop [--dir PATH] [-n N] [--reset]` | Tag functions that look confusing or pointless |
| `blerk tags [--dir PATH]` | List all tag keys and values in the index |
| `blerk rescan [PATH]` | Re-queue files for symbolization |
| `blerk purge [--dry-run]` | Remove DB records for files that match ignore patterns |
| `blerk status` | Show daemon status and queue depths |
| `blerk add <path>` | Add a folder to the watch list |
| `blerk remove <path>` | Remove a folder from the watch list and purge its records |


## Development

```
pip install -e ".[test]"
pytest tests/
```
