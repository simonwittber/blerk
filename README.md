# blerk - a local RAG for source code

blerk indexes source code into a local SQLite database and lets you search it with natural language. It watches folders, extracts symbols, adds git metadata, generates LLM descriptions, and stores vector embeddings for semantic search.

## Why blerk exists

The primary use case is as a context source for AI coding assistants via the MCP server.

An AI assistant that reads a codebase directly must scan directories, open files, and grep for symbols. This uses context window space fast and returns raw file content that the model must parse and reason about.

blerk inverts this. blerk pre-extracts, pre-describes, and pre-embeds all symbols before the assistant asks. When the assistant needs to understand something, it calls `search` or `browse`. It receives a compact list of relevant symbols with descriptions and signatures. No file reads, no grep, no directory traversal in the prompt.

This matters most for large codebases where the assistant cannot fit all the relevant code into context at once. Instead of reading ten files to find the right function, the assistant issues one search query and gets the answer directly. The assistant can use the saved context for actual reasoning.

The MCP tools blerk exposes are:

- `search` - semantic + keyword hybrid search over all indexed symbols. Returns the most relevant functions, methods, and types for a query.
- `browse` - lists all symbols in a directory with their signatures and line ranges. Useful for orienting in an unfamiliar package before doing targeted searches.

blerk also supports linting. The `blerk lint` command checks functions and methods in the indexed codebase against rules such as line count, parameter count, and nesting depth. Use `--exclude PATTERN` (repeatable) to skip paths that match a glob, for example `--exclude "*Generated*"`.

The `blerk confusing` command asks an LLM whether each function looks confusing or pointless without extra context. It tags results in the index so you can query them later. It accepts the same `--exclude PATTERN` flag to skip generated or vendor paths.

You can also use blerk directly from the command line for human-readable search output.


## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally (for embeddings and optional LLM descriptions)


## Install

```
pip install .
```

This puts `blerk`, `blerk-query`, and the daemon entry points on your PATH.

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

This starts the hub, which manages all five daemons. On first run it scans your watched folders and indexes them. Leave it running in the background.

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

## Symbol extraction engines

blerk offers two extraction engines, set by `symbolizer.engine` in config:

- **regexp** (default): fast regex-based extraction. No call-graph data. Good for quick setup.
- **treesitter**: AST-based. Accurate snippet boundaries and caller/callee relationships. Supports Go, Python, JS/TS, C, C++, C#. Falls back to regexp for other file types.

## Daemons

The hub manages five background processes. You can also run them individually:

| Command | Role |
|---|---|
| `blerk-watch` | Watches folders, hashes files, upserts into DB |
| `blerk-symbolize` | Extracts symbols from changed files (runs as N workers, set by `symbolizer.workers`) |
| `blerk-git` | Enriches files with git commit/author/branch |
| `blerk-describe` | Calls LLM to generate symbol descriptions |
| `blerk-embed` | Generates vector embeddings via Ollama |

Each daemon writes a heartbeat row to the `daemon_status` table every poll cycle, including queue depth, rate, and ETA.

## Commands

| Command | Description |
|---|---|
| `blerk query "..."` | Search indexed symbols with natural language |
| `blerk browse [--dir PATH]` | List symbols in a directory |
| `blerk detail <name>` | Show full detail for a symbol by exact name |
| `blerk deps [--dir PATH]` | Show the file-level dependency graph |
| `blerk lint [--exclude PATTERN]` | Check functions against lint rules (line count, params, nesting) |
| `blerk confusing [--exclude PATTERN]` | Tag functions that look confusing or pointless using an LLM |
| `blerk tags [--dir PATH]` | List all tag keys and values in the index |
| `blerk rescan [PATH]` | Re-queue files for symbolization |
| `blerk purge [--dry-run]` | Remove DB records for files that match ignore patterns |
| `blerk status` | Show daemon status and queue depths |
| `blerk add <path>` | Add a folder to the watch list |
| `blerk remove <path>` | Remove a folder from the watch list and purge its DB records and queue entries |


## Development

```
pip install -e ".[test]"
pytest tests/
```
