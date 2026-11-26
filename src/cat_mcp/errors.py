"""Unified error codes and error response structures.

This module defines a standard set of error codes used across all Cat
MCP tools. Every tool returns errors using these codes and the standard
ErrorResponse schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Standardized error codes for Cat MCP responses.

    Each error code is a machine-readable string that clients can use
    to programmatically handle error conditions.
    """

    # Authentication and authorization errors
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    INVALID_TOKEN = "INVALID_TOKEN"

    # Project and workspace errors
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_EXPIRED = "WORKSPACE_EXPIRED"
    CWD_CONFLICT = "CWD_CONFLICT"

    # File operation errors
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_ALREADY_EXISTS = "FILE_ALREADY_EXISTS"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    PATH_OUT_OF_BOUNDS = "PATH_OUT_OF_BOUNDS"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    INVALID_PATH = "INVALID_PATH"
    UNSAFE_PATH = "UNSAFE_PATH"
    IS_DIRECTORY = "IS_DIRECTORY"
    NOT_DIRECTORY = "NOT_DIRECTORY"

    # Input validation errors
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    MISSING_ARGUMENT = "MISSING_ARGUMENT"
    UNSUPPORTED_ENCODING = "UNSUPPORTED_ENCODING"

    # Operation errors
    OPERATION_FAILED = "OPERATION_FAILED"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"
    SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"

    # Server errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class CatError(Exception):
    """Base exception for Cat MCP errors.

    Attributes:
        error_code: Machine-readable error code.
        message: Human-readable error description.
        details: Additional error context.
    """

    def __init__(
        self,
        error_code: ErrorCode,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize a CatError.

        Args:
            error_code: The error code for this error.
            message: Human-readable description.
            details: Optional additional context.
        """
        self.error_code = error_code
        self.message = message or error_code.value
        self.details = details
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Convert this error to a dictionary for serialization.

        Returns:
            A dict with error_code, message, and optional details.
        """
        result: dict[str, Any] = {
            "error_code": self.error_code.value,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result
