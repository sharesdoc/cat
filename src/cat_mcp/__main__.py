"""CLI entry point for the cat-mcp server.

Parses command-line arguments, loads configuration from TOML (or built‑in
defaults), prints a non‑sensitive configuration summary, and starts the
server.
"""
# 依据: Task-R-20261126012656435-P0

from __future__ import annotations

import argparse
import sys


def _print_config_summary(cfg: object) -> None:
    """Print a non‑sensitive configuration summary to stderr.

    Excludes secret material; only shows high‑level settings the operator
    should verify on startup.
    """
    # 依据: Task-R-20261126012656435-P0
    server = getattr(cfg, "server", None)
    auth = getattr(cfg, "auth", None)
    limits = getattr(cfg, "limits", None)
    security = getattr(cfg, "security", None)
    projects = getattr(cfg, "projects", None)

    summary: list[str] = []
    summary.append("cat-mcp configuration:")
    if server is not None:
        summary.append(f"  listen:        {server.listen}")
    if auth is not None:
        summary.append(f"  auth required: {auth.require_auth}")
        summary.append(f"  auth issuer:   {auth.issuer}")
    if limits is not None:
        summary.append(f"  depth limit:   {limits.directory_depth}")
        summary.append(f"  page limit:    {limits.page_entries}")
    if security is not None:
        summary.append(f"  allow shell:   {security.allow_shell}")
    if projects is not None:
        summary.append(f"  projects:      {len(projects)} registered")

    print("\n".join(summary), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and start the Cat MCP server.

    Args:
        argv: Command-line argument list. If None, ``sys.argv`` is used.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    # 依据: Task-R-20261126012656435-P0
    parser = argparse.ArgumentParser(
        prog="cat-mcp",
        description="Cat — Remote software engineering workspace MCP server",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to configuration file (default: config.toml in search paths)",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        help="Show version information and exit",
    )

    args = parser.parse_args(argv)

    if args.version:
        from cat_mcp import __version__

        print(f"cat-mcp {__version__}")
        return 0

    # Load configuration
    try:
        from cat_mcp.config import load_config

        config = load_config(args.config)
    except FileNotFoundError:
        print(
            "cat-mcp: configuration file not found",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        # Error message is already sanitised (no internal paths)
        print(f"cat-mcp: configuration error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            "cat-mcp: failed to load configuration",
            file=sys.stderr,
        )
        # In production, log the full traceback; for CLI, keep it short.
        _ = exc  # silence unused-variable lint
        return 1

    _print_config_summary(config)

    # TODO: Start the MCP server
    # server = get_server(config)
    # server.run()

    print("cat-mcp: server not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
