"""MCP server initialization and ASGI application factory.

Replaces the CAT-01 placeholder with the official SDK v2 MCPServer.
Implements Streamable HTTP transport via Starlette/ASGI and provides
a unified tool-registration decorator with consistent error handling,
schema generation, and readOnlyHint/destructiveHint annotations.
"""
# 依据: Task-R-20261126012656436-P0

from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from cat_mcp import __version__
from cat_mcp.config import AppConfig
from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unified response helpers
# ---------------------------------------------------------------------------

def _success(
    data: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build a success response in Cat's unified format.

    Args:
        data: Response payload.
        request_id: Optional correlation identifier.

    Returns:
        A dict with ``ok``, ``request_id``, and ``data`` keys.
    """
    return {"ok": True, "request_id": request_id, "data": data}


def _failure(
    error_code: str,
    message: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build an error response in Cat's unified format.

    Args:
        error_code: Machine-readable error code (see :class:`ErrorCode`).
        message: Human-readable description.
        request_id: Optional correlation identifier.

    Returns:
        A dict with ``ok``, ``request_id``, and ``error.keys``.
    """
    return {
        "ok": False,
        "request_id": request_id,
        "error": {"code": error_code, "message": message},
    }


# ---------------------------------------------------------------------------
# Unified tool registration decorator
# ---------------------------------------------------------------------------


def cat_tool(
    server: MCPServer,
    *,
    name: str,
    description: str | None = None,
    read_only: bool = False,
    destructive: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a Cat MCP tool with unified error handling and annotations.

    The decorated function receives validated arguments matching its type
    annotations (auto-generated input schema).  Return values are
    automatically wrapped into Cat's unified response format
    (``{ok, request_id, data?, error?: {code, message}}``).  Expected
    exceptions (:class:`CatError`) and unexpected exceptions are caught and
    surfaced as structured error responses with ``isError=True`` at the MCP
    protocol level.

    Args:
        server: MCPServer instance to register the tool on.
        name: Tool name (``cat_`` prefix required by convention).
        description: Human-readable tool description.
        read_only: Hint to clients that this tool does not modify state.
        destructive: Hint to clients that this tool may perform destructive
            updates.

    Returns:
        A decorator that registers the function and returns the original
        function unchanged.
    """
    # 依据: Task-R-20261126012656436-P0
    annotations: ToolAnnotations | None = None
    if read_only or destructive:
        annotations = ToolAnnotations(
            read_only_hint=read_only or None,
            destructive_hint=destructive or None,
        )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Inner decorator that wraps *fn* and registers it on *server*."""

        @functools.wraps(fn)
        async def handler(**kwargs: Any) -> CallToolResult:
            """Thin handler: validate, call business logic, map result."""
            try:
                result = fn(**kwargs)
                if inspect.iscoroutine(result):
                    result = await result

                # Allow returning CallToolResult directly for tools that
                # need fine-grained control (e.g. streaming progress).
                if isinstance(result, CallToolResult):
                    return result

                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                _success(data=result),
                                ensure_ascii=False,
                            ),
                        )
                    ],
                )
            except CatError as exc:
                # 依据: Task-R-20261126012656436-P0 — isError for business errors
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                _failure(
                                    error_code=exc.error_code.value,
                                    message=exc.message,
                                ),
                                ensure_ascii=False,
                            ),
                        )
                    ],
                    is_error=True,
                )
            except Exception as exc:
                logger.exception("Tool %r raised an unexpected exception", name)
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                _failure(
                                    error_code=ErrorCode.INTERNAL_ERROR.value,
                                    message=str(exc),
                                ),
                                ensure_ascii=False,
                            ),
                        )
                    ],
                    is_error=True,
                )

        # structured_output=False: the handler returns a fully-formed
        # CallToolResult with text content; let the SDK pass it through
        # without attempting structured-content validation.
        server.add_tool(
            handler,
            name=name,
            description=description,
            annotations=annotations,
            structured_output=False,
        )
        return fn  # return the original function, not the wrapper

    return decorator


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _register_cat_get_capabilities(server: MCPServer, config: AppConfig) -> None:
    """Register the ``cat_get_capabilities`` introspection tool.

    Args:
        server: MCPServer instance.
        config: Application configuration (limits read from here).
    """
    # 依据: Task-R-20261126012656436-P0

    @cat_tool(
        server,
        name="cat_get_capabilities",
        description="获取服务器协议版本、已注册工具列表及运行限额。只读，不修改任何状态。",
        read_only=True,
    )
    async def get_capabilities() -> dict[str, Any]:
        """Return protocol version, registered tools, and limit settings."""
        # 依据: Task-R-20261126012656436-P0
        tools = await server.list_tools()
        limits = config.limits

        return {
            "protocol_version": "2025-11-05",
            "server_name": server.name,
            "server_version": __version__,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "annotations": {
                        "read_only_hint": (
                            t.annotations.read_only_hint
                            if t.annotations
                            else None
                        ),
                        "destructive_hint": (
                            t.annotations.destructive_hint
                            if t.annotations
                            else None
                        ),
                    },
                }
                for t in tools
            ],
            "limits": {
                "directory_depth": limits.directory_depth,
                "page_entries": limits.page_entries,
                "max_tool_response_bytes": limits.max_tool_response_bytes,
                "max_inline_write_bytes": limits.max_inline_write_bytes,
                "max_search_seconds": limits.max_search_seconds,
                "max_search_results": limits.max_search_results,
                "workspace_idle_seconds": limits.workspace_idle_seconds,
                "max_concurrent_per_subject": limits.max_concurrent_per_subject,
            },
        }


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: AppConfig) -> "Starlette":
    """Create and configure the Cat MCP server ASGI application.

    Initialises an ``MCPServer`` instance, registers all Cat tools, and
    returns a Starlette ASGI application serving the Streamable HTTP
    transport.

    Args:
        config: Application configuration (limits, auth, security, etc.).

    Returns:
        A configured Starlette ASGI application.
    """
    # 依据: Task-R-20261126012656436-P0
    server = MCPServer(
        name="cat-mcp",
        version=__version__,
    )

    _register_cat_get_capabilities(server, config)
    # Future tools are registered here as _register_*() calls.

    return server.streamable_http_app()
