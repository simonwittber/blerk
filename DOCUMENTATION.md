# blerk documentation

## Why blerk exists

An AI assistant that reads a codebase directly must scan directories, open files, and grep for symbols. This uses context window space fast and returns raw file content that the model must parse and reason about.

blerk inverts this. blerk pre-extracts, pre-describes, and pre-embeds all symbols before the assistant asks. When the assistant needs to understand something, it calls `search` or `browse`. It receives a compact list of relevant symbols with descriptions and signatures. No file reads, no grep, no directory traversal in the prompt.

This matters most for large codebases where the assistant cannot fit all the relevant code into context at once. Instead of reading ten files to find the right function, the assistant issues one search query and gets the answer directly. The assistant can use the saved context for actual reasoning.

## Configuration

Create or edit `~/.blerk/config.toml`. At minimum, set the folders to index:

```toml
[watch]
folders = [
    "~/git/myproject",
]
```

### Full example config

```toml
secrets_file    = "~/.blerk/secrets.toml"
analyzers_file  = "~/.blerk/analyzers.toml"

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
```

### LLM API key

If your LLM endpoint requires a key, put it in `~/.blerk/secrets.toml` (keep this out of version control):

```toml
[llm]
api_key = "sk-..."
```

### Pull embedding and LLM models

```
ollama pull nomic-embed-text
ollama pull llama3.2
```

## Commands

### Query

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
| `--verbose` | off | Show full output with code block content and scores |
| `--config PATH` | `~/.blerk/config.toml` | Path to config file |

### Example query output

```
[1] function Debouncer.add
path: /home/user/git/myproject/watcher.py
lines: 45-58
score: 0.941
def add(self, path, event):
    with self._lock:
        self._pending[path] = event
        ...

[2] method Debouncer._fire
path: /home/user/git/myproject/watcher.py
lines: 60-68
score: 0.887
```

### Browse

```
blerk browse [--dir PATH]
```

Lists all symbols in a directory with their signatures and line ranges.

### Show

```
blerk show <target> [--file PATH] [--lines N]
```

Show source code for a file or symbol by exact name or path substring. If multiple symbols share the name, all matches are shown with headers.

### Detail

```
blerk detail <name>
```

Show full detail for a symbol by exact name, including description, code blocks, callers, and callees.

### Deps

```
blerk deps [--dir PATH]
```

Show the file-level dependency graph.

### Lint

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

#### Suppression with .blerk files

Place a `.blerk` file in any directory to control lint behaviour for that directory and all subdirectories. The file uses TOML format.

```toml
# Suppress specific rules
suppress = ["long_function", "too_many_params"]

# Suppress all rules
suppress = ["*"]

# Exclude files from linting entirely (* wildcard supported)
exclude = ["*.generated.py", "migrations/*"]
```

### Analyze

```
blerk analyze [--dir PATH]
```

Runs configurable LLM rule checks against indexed symbols. You define rules as plain text descriptions in `~/.blerk/analyzers.toml`. Results are stored as findings with severity, message, and confidence scores.

### Similar

```
blerk similar <directory> [--threshold N] [--ext EXT]
```

Finds function-level code candidates for refactoring via the DRY (Don't Repeat Yourself) principle. Scans functions in the specified directory (excluding test code) and groups semantically similar implementations into clusters. Default threshold is 0.1 (0=identical, 1=orthogonal).

### Service

```
blerk service install [--config PATH]    # Install as system service
blerk service uninstall                   # Remove system service
blerk service status                      # Show service status
```

Manage blerk as a persistent system service with auto-restart and boot startup.

**Platform-specific installation and logging:**

**Linux (systemd):**
- Requires `sudo`: `sudo blerk service install`
- View logs: `journalctl -u blerk` or `journalctl -u blerk -f` (follow)
- Service runs at boot via systemd

**macOS (launchd):**
- Install as user service: `blerk service install`
- View logs: `tail -f ~/.blerk/blerk.log` (stdout) or `tail -f ~/.blerk/blerk.error.log` (stderr)
- Service runs at boot via launchd

**Windows (Task Scheduler):**
- Requires administrator: run Command Prompt as Administrator, then `blerk service install`
- View logs: Event Viewer > Windows Logs > Application (search for "blerk" errors)
- Service runs at next system boot

Service auto-restarts on failure (10-second delay) and respects editable installs (`pip install -e .`).

### Tags

```
blerk tags [--dir PATH]
```

List all tag keys and values in the index.

### Status

```
blerk status
```

Show daemon status and queue depths.

### Rescan

```
blerk rescan [PATH]
```

Re-queue files for symbolization.

### Purge

```
blerk purge [--dry-run]
```

Remove DB records for files that match ignore patterns.

### Add / Remove folders

```
blerk add <path>              # Add a folder to the watch list
blerk remove <path>           # Remove a folder from the watch list and purge its records
```

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

## Symbol extraction

blerk uses tree-sitter for all symbol extraction. Supported languages: Go, Python, JS/TS, C, C++, C#. The extractor produces accurate snippet boundaries and caller/callee relationships for use in lint and search.

## Development

```bash
python -m venv venv
source venv/bin/activate          # On Windows: venv\Scripts\activate
pip install -e ".[test]"
pytest tests/
```
