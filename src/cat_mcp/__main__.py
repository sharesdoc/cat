"""CLI entry point for the cat-mcp server.

This module provides the command-line interface for starting the Cat
MCP server. It uses argparse to parse command-line arguments and
launches the server with the specified configuration.
"""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and start the Cat MCP server.

    Args:
        argv: Command-line argument list. If None, sys.argv is used.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
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

    # TODO: Load config and start the MCP server
    # server = get_server(config)
    # server.run()

    print("cat-mcp: server not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
