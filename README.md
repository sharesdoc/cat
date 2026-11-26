# Cat

**Cat** is a remote software engineering workspace MCP server. It provides
authorized AI coding assistants with direct, deterministic read and write
access to projects on a remote server through the Model Context Protocol (MCP).

Cat does not run AI models, replace IDE or version control tools, or execute
arbitrary shell commands. It is a focused file system service designed for
real-world AI-assisted software engineering workflows.

## Features (planned)

- Project discovery and registration
- Directory browsing with pagination and filtering
- Single-file reading with line-range and byte-range support
- Precise file editing (text match, line range, unified diff)
- Concurrent workspace isolation with versioned CWD
- Search with ripgrep (optional) and standard library fallback
- Audit trail and change history
- OAuth2-based authentication and authorization

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

### Installation

```bash
# Clone the repository
git clone https://github.com/username/cat.git
cd cat

# Create a virtual environment and install dependencies
uv sync

# Run tests
uv run pytest

# Start the server (when implemented)
uv run cat-mcp --config config.example.toml
```

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
