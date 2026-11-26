"""Configuration loading and validation.

This module provides configuration classes and loading utilities for the
Cat MCP server. Configuration is loaded from TOML files with environment
variable overrides for sensitive values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ServerConfig:
    """Server-level configuration.

    Attributes:
        listen: Host and port to bind to.
        public_url: Public-facing URL of this server.
    """

    listen: str = "127.0.0.1:8765"
    public_url: str = "https://cat.example.com/mcp"


@dataclass
class AuthConfig:
    """Authentication and authorization configuration.

    Attributes:
        issuer: OAuth issuer URL for token validation.
        audience: Expected token audience.
        require_auth: Whether authentication is required.
    """

    issuer: str = "https://identity.example.com"
    audience: str = "cat"
    require_auth: bool = True


@dataclass
class ProjectEntry:
    """A single registered project entry.

    Attributes:
        id: Unique project identifier.
        root: Absolute path to the project root directory.
        access: List of allowed access scopes.
    """

    id: str = ""
    root: str = ""
    access: list[str] = field(default_factory=lambda: ["read"])


@dataclass
class LimitsConfig:
    """Operational limits and thresholds.

    Attributes:
        directory_depth: Maximum directory traversal depth.
        page_entries: Maximum entries per page.
        max_tool_response_bytes: Maximum tool response size.
        max_inline_write_bytes: Maximum inline write payload size.
        max_search_seconds: Search timeout in seconds.
        max_search_results: Maximum search result count.
        workspace_idle_seconds: Workspace idle timeout.
        max_concurrent_per_subject: Max concurrent workspaces per subject.
        backup_retention_days: Backup retention period.
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


@dataclass
class SecurityConfig:
    """Security-related configuration.

    Attributes:
        allow_shell: Whether shell execution is allowed.
        follow_symlinks: Whether symlink following is allowed.
        allow_recursive_delete: Whether recursive deletion is allowed.
        deny_sensitive_files: Whether sensitive file access is denied.
    """

    allow_shell: bool = False
    follow_symlinks: bool = False
    allow_recursive_delete: bool = False
    deny_sensitive_files: bool = True


@dataclass
class AppConfig:
    """Top-level application configuration.

    Attributes:
        server: Server configuration.
        auth: Authentication configuration.
        projects: Registered project entries.
        limits: Operational limits.
        security: Security configuration.
    """

    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    projects: list[ProjectEntry] = field(default_factory=list)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from a TOML file.

    If no path is given, returns default configuration. Environment
    variable overrides are applied for sensitive values.

    Args:
        config_path: Optional path to a TOML configuration file.

    Returns:
        An AppConfig instance with loaded or default values.
    """
    config = AppConfig()

    if config_path and os.path.isfile(config_path):
        # TODO: Parse TOML configuration file and merge into config
        pass

    return config
