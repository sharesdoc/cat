"""Pydantic models for MCP tool input and output schemas.

This module defines the request and response models used by Cat MCP
tools. These models provide type validation and documentation for
tool parameters and return values.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AccessScope(str, Enum):
    """Access scope for project permissions."""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    DISCOVER = "discover"
    WORKSPACE = "workspace"
    AUDIT = "audit"


class PathBase(str, Enum):
    """Path base resolution mode."""

    CWD = "cwd"
    ROOT = "root"


class SearchMode(str, Enum):
    """Text search mode."""

    LITERAL = "literal"
    REGEX = "regex"


class ProjectInfo(BaseModel):
    """Information about a registered project.

    Attributes:
        id: Unique project identifier.
        display_name: Human-readable project name.
        root_relative: Relative path identifier.
        access: List of granted access scopes.
    """

    id: str = Field(..., description="Unique project identifier")
    display_name: str = Field("", description="Human-readable project name")
    root_relative: str = Field("", description="Root-relative path identifier")
    access: list[AccessScope] = Field(
        default_factory=list, description="Granted access scopes"
    )


class WorkspaceInfo(BaseModel):
    """Information about an active workspace session.

    Attributes:
        workspace_id: Unique workspace identifier.
        project_id: Associated project identifier.
        cwd: Current working directory.
        cwd_version: Version counter for the current working directory.
        expires_at: Expiration timestamp.
    """

    workspace_id: str = Field(..., description="Unique workspace identifier")
    project_id: str = Field(..., description="Associated project identifier")
    cwd: str = Field("/", description="Current working directory")
    cwd_version: int = Field(0, description="Current working directory version")
    expires_at: datetime | None = Field(None, description="Expiration timestamp")


class FileEntry(BaseModel):
    """A file or directory entry in a listing.

    Attributes:
        name: Entry name.
        type: Entry type (file, directory, symlink).
        size: File size in bytes (None for directories).
        mtime: Last modification timestamp.
    """

    name: str = Field(..., description="Entry name")
    type: str = Field("file", description="Entry type")
    size: int | None = Field(None, description="File size in bytes")
    mtime: datetime | None = Field(None, description="Last modification time")


class ErrorResponse(BaseModel):
    """Standard error response structure.

    Attributes:
        error_code: Machine-readable error code.
        message: Human-readable error description.
        details: Additional error context.
    """

    error_code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error description")
    details: dict[str, Any] | None = Field(
        None, description="Additional error context"
    )
