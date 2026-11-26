"""Configuration loading and validation.

Provides Pydantic-based configuration models and TOML loading for the
Cat MCP server.  Real (non-example) configuration files live outside the
repository.  Environment variables can override TOML values using
CAT_SECTION__KEY syntax.

Usage:
    from cat_mcp.config import load_config
    cfg = load_config("/path/to/config.toml")
"""
# 依据: Task-R-20261126012656435-P0

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class ServerConfig(BaseModel):
    """Server-level network configuration.

    Attributes:
        listen: Host and port to bind to (``host:port``).
        public_url: Public-facing URL of this server.
    """

    listen: str = "127.0.0.1:8765"
    public_url: str = "https://cat.example.com/mcp"

    @field_validator("listen")
    @classmethod
    def _check_listen_format(cls, v: str) -> str:
        """Validate listen is host:port and port is 1-65535."""
        # 依据: Task-R-20261126012656435-P0
        if ":" not in v:
            raise ValueError(f"listen must be in host:port format, got {v!r}")
        port_str = v.rsplit(":", 1)[-1]
        if not port_str.isdigit():
            raise ValueError(f"port must be numeric, got {port_str!r}")
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise ValueError(f"port must be 1-65535, got {port}")
        return v

    @field_validator("public_url")
    @classmethod
    def _check_public_url(cls, v: str) -> str:
        """Validate public_url is a valid HTTP(S) URL."""
        # 依据: Task-R-20261126012656435-P0
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"public_url must start with http:// or https://, got {v!r}"
            )
        return v


class AuthConfig(BaseModel):
    """Authentication and authorisation configuration.

    Attributes:
        issuer: OAuth/OIDC issuer URL for token validation.
        audience: Expected token audience.
        require_auth: Whether authentication is required.  Must be ``True``
            when the server is publicly reachable.
    """

    issuer: str = "https://identity.example.com"
    audience: str = "cat"
    require_auth: bool = True


class ProjectEntry(BaseModel):
    """A single registered project.

    Attributes:
        id: Unique project identifier (must be non-empty).
        root: Absolute path to the project root directory.
        access: List of allowed access scopes (defaults to ``["read"]``).
    """

    id: str = ""
    root: str = ""
    access: list[str] = ["read"]

    @field_validator("id")
    @classmethod
    def _check_id_nonempty(cls, v: str) -> str:
        """Reject empty project IDs."""
        # 依据: Task-R-20261126012656435-P0
        if not v.strip():
            raise ValueError("project id must not be empty")
        return v.strip()

    @field_validator("root")
    @classmethod
    def _check_root_nonempty(cls, v: str) -> str:
        """Reject empty project root paths."""
        # 依据: Task-R-20261126012656435-P0
        if not v.strip():
            raise ValueError("project root must not be empty")
        return v.strip()


class LimitsConfig(BaseModel):
    """Operational limits and thresholds.

    All values must be non‑negative integers.
    """

    directory_depth: int = 8
    page_entries: int = 200
    max_tool_response_bytes: int = 65536
    max_inline_write_bytes: int = 262144
    max_search_seconds: int = 10
    max_search_results: int = 100
    workspace_idle_seconds: int = 7200
    max_concurrent_per_subject: int = 4
    backup_retention_days: int = 7

    @field_validator(
        "directory_depth",
        "page_entries",
        "max_tool_response_bytes",
        "max_inline_write_bytes",
        "max_search_seconds",
        "max_search_results",
        "workspace_idle_seconds",
        "max_concurrent_per_subject",
        "backup_retention_days",
    )
    @classmethod
    def _check_non_negative(cls, v: int, info: Any) -> int:
        """Reject negative limit values."""
        # 依据: Task-R-20261126012656435-P0
        if v < 0:
            raise ValueError(
                f"{info.field_name} must not be negative, got {v}"
            )
        return v


class SecurityConfig(BaseModel):
    """Security-related configuration flags.

    Attributes:
        allow_shell: Whether shell execution is allowed.
        follow_symlinks: Whether symlink following is allowed.
        allow_recursive_delete: Whether recursive directory deletion is
            allowed.
        deny_sensitive_files: Whether access to sensitive files (e.g.
            credentials, key material) is denied.
    """

    allow_shell: bool = False
    follow_symlinks: bool = False
    allow_recursive_delete: bool = False
    deny_sensitive_files: bool = True


class AppConfig(BaseModel):
    """Top-level application configuration.

    Attributes:
        server: Server-level network configuration.
        auth: Authentication and authorisation configuration.
        projects: Registered project entries.
        limits: Operational limits.
        security: Security configuration flags.
    """

    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    projects: list[ProjectEntry] = Field(default_factory=list)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @model_validator(mode="after")
    def _reject_anonymous_public(self) -> AppConfig:
        """Refuse public (non-local) servers that disable authentication.

        A public server without authentication is a security risk — reject
        the configuration with a clear, non‑revealing error.
        """
        # 依据: Task-R-20261126012656435-P0
        if not self.auth.require_auth:
            public_url = self.server.public_url
            # Only reject if the URL is not a typical loopback / local
            # address.  This catches the obvious case; production-grade
            # checks would also resolve DNS, but that is out of scope.
            if not self._is_local_url(public_url):
                raise ValueError(
                    "public servers must enable authentication "
                    "(auth.require_auth = true)"
                )
        return self

    @model_validator(mode="after")
    def _check_unique_project_ids(self) -> AppConfig:
        """Reject configurations with duplicate project IDs."""
        # 依据: Task-R-20261126012656435-P0
        seen: set[str] = set()
        for entry in self.projects:
            pid = entry.id
            if pid in seen:
                raise ValueError(f"duplicate project id: {pid!r}")
            seen.add(pid)
        return self

    @staticmethod
    def _is_local_url(url: str) -> bool:
        """Heuristic: is *url* likely a local/loopback address?"""
        # 依据: Task-R-20261126012656435-P0
        local_patterns = (
            "127.0.0.1",
            "localhost",
            "0.0.0.0",
            "::1",
            "[::1]",
        )
        return any(p in url for p in local_patterns)


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


def _parse_env_key(key: str) -> tuple[str, str] | None:
    """Parse a ``CAT_SECTION__KEY`` environment variable name.

    Returns ``(section, key)`` on success, ``None`` if the name does not
    match the expected pattern.
    """
    # 依据: Task-R-20261126012656435-P0
    m = re.match(r"^CAT_([A-Z]+)__(.+)$", key)
    if not m:
        return None
    return (m.group(1).lower(), m.group(2).lower())


def _convert_env_value(raw: str, target_type: type) -> Any:
    """Convert a raw env‑var string to *target_type*.

    Supports ``bool`` (accepts ``true``/``false``/``1``/``0``/``yes``/``no``,
    case‑insensitive), ``int``, and ``str``.
    """
    # 依据: Task-R-20261126012656435-P0
    if target_type is bool:
        return raw.lower() in ("true", "1", "yes")
    if target_type is int:
        return int(raw, 10)
    return raw


def _apply_env_overrides(cfg: AppConfig) -> None:
    """Apply environment-variable overrides to *cfg* in place.

    Variables must match ``CAT_SECTION__KEY`` (e.g. ``CAT_SERVER__LISTEN``,
    ``CAT_AUTH__ISSUER``).  Nested keys use double underscore as the
    separator.  Project entries cannot be set through environment variables.

    Type conversion is inferred from the current attribute value on the
    model instance (bool, int, or str).  Values that fail conversion are
    silently skipped.
    """
    # 依据: Task-R-20261126012656435-P0
    section_map: dict[str, BaseModel] = {
        "server": cfg.server,
        "auth": cfg.auth,
        "limits": cfg.limits,
        "security": cfg.security,
    }

    for env_key, raw in sorted(os.environ.items()):
        parsed = _parse_env_key(env_key)
        if parsed is None:
            continue
        section_name, key_name = parsed

        obj = section_map.get(section_name)
        if obj is None:
            continue
        if not hasattr(obj, key_name):
            continue

        current = getattr(obj, key_name)
        try:
            if isinstance(current, bool):
                typed_val = raw.lower() in ("true", "1", "yes")
            elif isinstance(current, int):
                typed_val = int(raw, 10)
            else:
                typed_val = raw
        except (ValueError, TypeError):
            continue

        setattr(obj, key_name, typed_val)


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def _search_config_paths() -> Path | None:
    """Search standard locations for a ``config.toml`` file.

    Order: current working directory, then ``XDG_CONFIG_HOME/cat/``,
    then ``~/.config/cat/``.
    """
    # 依据: Task-R-20261126012656435-P0
    candidates: list[Path] = []

    # Current working directory
    candidates.append(Path.cwd() / "config.toml")

    # XDG_CONFIG_HOME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "cat" / "config.toml")
    else:
        candidates.append(Path.home() / ".config" / "cat" / "config.toml")

    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from a TOML file with env‑var overrides.

    Resolution order:
    1. If *config_path* is given and the file exists, parse it.
    2. Otherwise search standard paths (``./config.toml``,
       ``XDG_CONFIG_HOME/cat/config.toml``, ``~/.config/cat/config.toml``).
    3. If no file is found, start from built‑in defaults.
    4. Apply environment variable overrides (``CAT_SECTION__KEY``).
    5. Validate the assembled configuration (rejects are raised as
       :class:`ValueError`).

    Args:
        config_path: Optional explicit path to a TOML configuration file.

    Returns:
        A validated :class:`AppConfig` instance.

    Raises:
        FileNotFoundError: *config_path* was given but does not exist.
        ValueError: Configuration failed validation.
        tomllib.TOMLDecodeError: TOML syntax is invalid.
    """
    # 依据: Task-R-20261126012656435-P0
    raw: dict[str, Any] = {}

    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(
                "configuration file not found"  # no path disclosure
            )
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    else:
        found = _search_config_paths()
        if found is not None:
            with found.open("rb") as fh:
                raw = tomllib.load(fh)

    # --- Build config objects from raw dict (supports partial TOML) ---
    server_kw = raw.get("server", {})
    auth_kw = raw.get("auth", {})
    limits_kw = raw.get("limits", {})
    security_kw = raw.get("security", {})

    projects_raw: list[dict[str, Any]] = raw.get("projects", [])
    if isinstance(projects_raw, dict):
        projects_raw = [projects_raw]

    cfg = AppConfig(
        server=ServerConfig(**server_kw),
        auth=AuthConfig(**auth_kw),
        projects=[ProjectEntry(**p) for p in projects_raw],
        limits=LimitsConfig(**limits_kw),
        security=SecurityConfig(**security_kw),
    )

    # --- Environment variable overrides ---
    _apply_env_overrides(cfg)

    # --- Re-validate after env overrides ---
    # Pydantic model_validator runs at construction time, but env overrides
    # mutate attributes *after* construction.  Force re-validation so that
    # cross-field checks (anonymous public, duplicate IDs) are re-applied.
    return AppConfig.model_validate(cfg.model_dump())
