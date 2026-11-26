"""Import verification tests for the cat_mcp package.

These tests verify that the package can be imported correctly and that
all public modules are accessible.
"""



def test_package_import() -> None:
    """Verify the cat_mcp package can be imported."""
    import cat_mcp  # noqa: F811

    assert cat_mcp.__version__ == "1.0.0"


def test_main_module_import() -> None:
    """Verify the __main__ module can be imported."""
    import cat_mcp.__main__  # noqa: F401


def test_server_module_import() -> None:
    """Verify the server module can be imported."""
    import cat_mcp.server  # noqa: F401


def test_config_module_import() -> None:
    """Verify the config module can be imported."""
    import cat_mcp.config  # noqa: F401


def test_schemas_module_import() -> None:
    """Verify the schemas module can be imported."""
    import cat_mcp.schemas  # noqa: F401


def test_errors_module_import() -> None:
    """Verify the errors module can be imported."""
    import cat_mcp.errors  # noqa: F401


def test_main_entrypoint() -> None:
    """Verify the CLI entry point works with --version."""
    from cat_mcp.__main__ import main

    result = main(["--version"])
    assert result == 0
