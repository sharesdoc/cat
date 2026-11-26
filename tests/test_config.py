"""Tests for configuration loading and validation.

Covers:
- Valid configuration loads correctly.
- Field-level validation rejects bad values.
- Duplicate project IDs are rejected.
- Negative limits are rejected.
- Anonymous public configuration is rejected.
- Environment variable overrides work.
- Missing configuration falls back to defaults.
"""
# 依据: Task-R-20261126012656435-P0

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from cat_mcp.config import (
    AppConfig,
    AuthConfig,
    LimitsConfig,
    ProjectEntry,
    ServerConfig,
    load_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_toml() -> str:
    """Return a valid TOML configuration string."""
    return """\
[server]
listen = "127.0.0.1:8765"
public_url = "https://cat.example.com/mcp"

[auth]
issuer = "https://identity.example.com"
audience = "cat"
require_auth = true

[[projects]]
id = "proj-a"
root = "/srv/cat/projects/a"
access = ["read", "write"]

[[projects]]
id = "proj-b"
root = "/srv/cat/projects/b"
access = ["read"]

[limits]
directory_depth = 8
page_entries = 200
max_tool_response_bytes = 65536
max_inline_write_bytes = 262144
max_search_seconds = 10
max_search_results = 100
workspace_idle_seconds = 7200
max_concurrent_per_subject = 4
backup_retention_days = 7

[security]
allow_shell = false
follow_symlinks = false
allow_recursive_delete = false
deny_sensitive_files = true
"""


@pytest.fixture
def valid_toml_path(valid_toml: str) -> Path:
    """Write a valid TOML to a temporary file and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".toml", delete=False
    )
    tmp.write(valid_toml.encode("utf-8"))
    tmp.close()
    yield Path(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# ServerConfig
# ---------------------------------------------------------------------------


class TestServerConfig:
    """ServerConfig field validation."""

    def test_listen_valid(self) -> None:
        cfg = ServerConfig(listen="0.0.0.0:8080")
        assert cfg.listen == "0.0.0.0:8080"

    def test_listen_invalid_format(self) -> None:
        with pytest.raises(ValidationError, match="host:port"):
            ServerConfig(listen="justahost")

    def test_listen_port_out_of_range(self) -> None:
        with pytest.raises(ValidationError, match="1-65535"):
            ServerConfig(listen="host:0")
        with pytest.raises(ValidationError, match="1-65535"):
            ServerConfig(listen="host:70000")

    def test_listen_port_non_numeric(self) -> None:
        with pytest.raises(ValidationError, match="numeric"):
            ServerConfig(listen="host:abc")

    def test_public_url_invalid_scheme(self) -> None:
        with pytest.raises(ValidationError, match="http"):
            ServerConfig(public_url="ftp://example.com")


# ---------------------------------------------------------------------------
# AuthConfig
# ---------------------------------------------------------------------------


class TestAuthConfig:
    """AuthConfig defaults."""

    def test_default_require_auth_true(self) -> None:
        cfg = AuthConfig()
        assert cfg.require_auth is True


# ---------------------------------------------------------------------------
# ProjectEntry
# ---------------------------------------------------------------------------


class TestProjectEntry:
    """ProjectEntry field validation."""

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not be empty"):
            ProjectEntry(id="", root="/path")

    def test_empty_root_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not be empty"):
            ProjectEntry(id="p1", root="")

    def test_default_access(self) -> None:
        entry = ProjectEntry(id="p1", root="/path")
        assert entry.access == ["read"]


# ---------------------------------------------------------------------------
# LimitsConfig
# ---------------------------------------------------------------------------


class TestLimitsConfig:
    """LimitsConfig non-negative validation."""

    def test_defaults(self) -> None:
        cfg = LimitsConfig()
        assert cfg.directory_depth == 8
        assert cfg.page_entries == 200
        assert cfg.max_tool_response_bytes == 65536

    def test_negative_limits(self) -> None:
        """Any negative limit value must be rejected."""
        fields = [
            "directory_depth",
            "page_entries",
            "max_tool_response_bytes",
            "max_inline_write_bytes",
            "max_search_seconds",
            "max_search_results",
            "workspace_idle_seconds",
            "max_concurrent_per_subject",
            "backup_retention_days",
        ]
        for field in fields:
            with pytest.raises(
                ValidationError, match="not be negative"
            ):
                LimitsConfig(**{field: -1})


# ---------------------------------------------------------------------------
# AppConfig cross-field validation
# ---------------------------------------------------------------------------


class TestAppConfig:
    """AppConfig cross-model validation."""

    def test_defaults(self) -> None:
        """Empty AppConfig uses all safe defaults."""
        cfg = AppConfig()
        assert cfg.server.listen == "127.0.0.1:8765"
        assert cfg.auth.require_auth is True
        assert cfg.security.allow_shell is False
        assert cfg.limits.directory_depth == 8

    def test_duplicate_project_ids_rejected(self) -> None:
        """Projects with the same ID must be rejected."""
        with pytest.raises(ValidationError, match="duplicate project id"):
            AppConfig(
                projects=[
                    ProjectEntry(id="dup", root="/a"),
                    ProjectEntry(id="dup", root="/b"),
                ]
            )

    def test_anonymous_public_rejected(self) -> None:
        """A public server with require_auth=false must be rejected."""
        with pytest.raises(
            ValidationError, match="must enable authentication"
        ):
            AppConfig(
                server=ServerConfig(
                    listen="0.0.0.0:8765",
                    public_url="https://cat.example.com",
                ),
                auth=AuthConfig(require_auth=False),
            )

    def test_localhost_anonymous_allowed(self) -> None:
        """Localhost with require_auth=false *is* allowed."""
        cfg = AppConfig(
            server=ServerConfig(
                listen="127.0.0.1:8765",
                public_url="http://127.0.0.1:8765",
            ),
            auth=AuthConfig(require_auth=False),
        )
        assert cfg.auth.require_auth is False


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Integration tests for load_config()."""

    def test_load_valid(self, valid_toml_path: Path) -> None:
        """A well-formed TOML file loads without error."""
        cfg = load_config(str(valid_toml_path))
        assert len(cfg.projects) == 2
        assert cfg.server.listen == "127.0.0.1:8765"
        assert cfg.server.public_url == "https://cat.example.com/mcp"
        assert cfg.auth.issuer == "https://identity.example.com"
        assert cfg.auth.require_auth is True
        assert cfg.limits.directory_depth == 8
        assert cfg.security.allow_shell is False

    def test_defaults_when_no_file(self) -> None:
        """load_config(None) with no TOML file returns defaults."""
        cfg = load_config()
        assert cfg.server.listen == "127.0.0.1:8765"
        assert cfg.auth.require_auth is True
        assert len(cfg.projects) == 0

    def test_file_not_found(self) -> None:
        """Specifying a non-existent path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/cat/config.toml")

    def test_invalid_toml_syntax(self) -> None:
        """Malformed TOML raises TOMLDecodeError."""
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".toml", delete=False
        )
        tmp.write(b"invalid [[[ toml {{{")
        tmp.close()
        path = Path(tmp.name)
        try:
            with pytest.raises(tomllib.TOMLDecodeError):
                load_config(str(path))
        finally:
            path.unlink()

    def test_duplicate_ids_in_file(self) -> None:
        """A config file with duplicate project IDs is rejected."""
        toml = """\
[[projects]]
id = "same"
root = "/a"

[[projects]]
id = "same"
root = "/b"
"""
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".toml", delete=False
        )
        tmp.write(toml.encode("utf-8"))
        tmp.close()
        path = Path(tmp.name)
        try:
            with pytest.raises(ValueError, match="duplicate project id"):
                load_config(str(path))
        finally:
            path.unlink()

    def test_negative_limits_in_file(self) -> None:
        """A config file with negative limits is rejected."""
        toml = """\
[limits]
directory_depth = -1
"""
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".toml", delete=False
        )
        tmp.write(toml.encode("utf-8"))
        tmp.close()
        path = Path(tmp.name)
        try:
            with pytest.raises(ValueError):
                load_config(str(path))
        finally:
            path.unlink()

    def test_anonymous_public_in_file(self) -> None:
        """A config file with require_auth=false is rejected."""
        toml = """\
[server]
listen = "0.0.0.0:8765"
public_url = "https://public.example.com"

[auth]
require_auth = false
"""
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".toml", delete=False
        )
        tmp.write(toml.encode("utf-8"))
        tmp.close()
        path = Path(tmp.name)
        try:
            with pytest.raises(
                ValueError, match="must enable authentication"
            ):
                load_config(str(path))
        finally:
            path.unlink()

    def test_env_overrides_string(self, valid_toml_path: Path) -> None:
        """Environment variables override TOML string values."""
        key = "CAT_SERVER__LISTEN"
        os.environ[key] = "0.0.0.0:9999"
        try:
            cfg = load_config(str(valid_toml_path))
            assert cfg.server.listen == "0.0.0.0:9999"
        finally:
            os.environ.pop(key, None)

    def test_env_overrides_bool(self, valid_toml_path: Path) -> None:
        """Environment variables override TOML bool values."""
        key = "CAT_SECURITY__ALLOW_SHELL"
        os.environ[key] = "true"
        try:
            cfg = load_config(str(valid_toml_path))
            assert cfg.security.allow_shell is True
        finally:
            os.environ.pop(key, None)

    def test_env_overrides_int(self, valid_toml_path: Path) -> None:
        """Environment variables override TOML int values."""
        key = "CAT_LIMITS__DIRECTORY_DEPTH"
        os.environ[key] = "42"
        try:
            cfg = load_config(str(valid_toml_path))
            assert cfg.limits.directory_depth == 42
        finally:
            os.environ.pop(key, None)

    def test_env_override_after_default(self) -> None:
        """Environment variable overrides on default config."""
        key = "CAT_LIMITS__PAGE_ENTRIES"
        os.environ[key] = "99"
        try:
            cfg = load_config()
            assert cfg.limits.page_entries == 99
        finally:
            os.environ.pop(key, None)

    def test_env_override_rejected_by_validation(self) -> None:
        """An env override that violates validation must raise."""
        key = "CAT_LIMITS__DIRECTORY_DEPTH"
        os.environ[key] = "-5"
        try:
            with pytest.raises(ValueError, match="not be negative"):
                load_config()
        finally:
            os.environ.pop(key, None)

    def test_irrelevant_env_ignored(self, valid_toml_path: Path) -> None:
        """Env vars without the CAT prefix are ignored."""
        os.environ["HOME"] = "/should/not/affect/config"
        os.environ["PATH"] = "/should/not/affect/config"
        try:
            cfg = load_config(str(valid_toml_path))
            assert cfg.server.listen == "127.0.0.1:8765"
        finally:
            pass  # HOME and PATH are real vars; don't pop them

    def test_unknown_section_env_ignored(
        self, valid_toml_path: Path
    ) -> None:
        """Env vars for unknown CAT sections are ignored."""
        key = "CAT_UNKNOWN__SOMETHING"
        os.environ[key] = "value"
        try:
            cfg = load_config(str(valid_toml_path))
            assert cfg is not None
        finally:
            os.environ.pop(key, None)
