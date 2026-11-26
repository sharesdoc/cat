# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — Unreleased

### Added

- Initial project scaffold with pyproject.toml and uv-based build system.
- CLI entry point with argparse argument parsing.
- MCP server shell (MCPServer class with tool registration).
- Configuration loading skeleton (dataclass-based config model).
- Pydantic schema definitions for MCP tool input/output models.
- Unified error code enumeration and error response structure.
- Test harness with pytest and import verification tests.
- CI workflow (GitHub Actions) with Python 3.12/3.13, Ruff linting, and pytest.
- Community documentation: README (en/zh-CN), CONTRIBUTING, SECURITY, CHANGELOG, THIRD_PARTY_NOTICES.
- Configuration example derived from system design specification.
- Standard Python .gitignore and .python-version files.
