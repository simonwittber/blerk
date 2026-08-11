# blerk - antislop context source for source code

Stop wasting tokens reading files. blerk pre-indexes your codebase into a local SQLite database, so when an AI assistant needs to understand code, it doesn't scan directories or grep. It calls a single search query and gets back a compact list of relevant symbols with descriptions and signatures.

For large codebases, this cuts context usage by 80%+. The assistant spends tokens on reasoning, not file I/O.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally (for embeddings and optional LLM descriptions)

## Quick start

```bash
git clone https://github.com/simonwittber/blerk.git
cd blerk
python -m venv venv
source venv/bin/activate          # On Windows: venv\Scripts\activate
pip install -e .
blerk init
```

This indexes your watched folders, starts background daemons, and watches for changes.

## MCP tools

Add blerk to your MCP config to give Claude access to your codebase:

```json
{
  "mcpServers": {
    "blerk": {
      "command": "blerk-mcp"
    }
  }
}
```

Once connected, the assistant can call:
- `search(query)` — find relevant symbols by meaning
- `browse(directory)` — list files or symbols
- `show(target)` — show source code
- `detail(name)` — show full symbol info with callers/callees
- `deps(directory)` — file-level dependency graph

## Command-line tools

```
blerk query "how does the debouncer work"     # Search
blerk browse [--dir PATH]                      # List symbols
blerk show <target>                            # Show source
blerk lint [--dir PATH]                        # Check code quality
blerk status                                   # Daemon status
```

## Code quality

- **[`blerk lint`](DOCUMENTATION.md#lint)** — structural checks: line count, nesting depth, parameter count, duplicates, dependencies
- **[`blerk analyze`](DOCUMENTATION.md#analyze)** — configurable LLM rule checks against indexed symbols
- **[`blerk similar`](DOCUMENTATION.md#similar)** — find DRY violations: semantically similar functions worth consolidating

Fingerprinting (simhash, normhash) runs automatically during indexing for exact/near clone detection.

See [DOCUMENTATION.md](DOCUMENTATION.md) for full reference.
