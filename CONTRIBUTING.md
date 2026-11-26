# Contributing to Cat

Thank you for considering contributing to Cat! We welcome contributions
of all kinds, including bug reports, feature suggestions, documentation
improvements, and code changes.

## Getting Started

1. Fork the repository and clone your fork.
2. Create a virtual environment and install development dependencies:
   ```bash
   uv sync --dev
   ```
3. Create a new branch for your changes:
   ```bash
   git checkout -b your-feature-branch
   ```

## Development Guidelines

### Code Style

- This project follows [Ruff](https://docs.astral.sh/ruff/) linting rules.
- Run `uv run ruff check src/` before committing.
- All Python files must include file-level docstrings.
- Use type annotations for all function signatures.

### Testing

- Write tests for any new functionality.
- Run the full test suite with `uv run pytest`.
- Ensure all tests pass before submitting a pull request.

### Commit Messages

- Use clear, descriptive commit messages.
- Reference issues and pull requests where appropriate.

## Pull Request Process

1. Ensure your code passes all linting and tests.
2. Update documentation if your changes affect public APIs or behavior.
3. Submit a pull request with a clear description of the changes.

## Code of Conduct

Please be respectful and constructive in all interactions. This project
is maintained by volunteers, and a welcoming community benefits everyone.

## Questions

If you have questions about contributing, please open a discussion or
issue on GitHub.
