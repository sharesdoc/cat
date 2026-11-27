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
import os
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from cat_mcp import __version__
from cat_mcp.config import AppConfig
from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.state import StateManager

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
                                    message="Internal server error",  # 脱敏，详情见日志
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
# AccessChecker singleton (module-level for tool handler access)
# ---------------------------------------------------------------------------

_access_checker: Any = None


def get_access_checker() -> Any:
    """Return the global ``AccessChecker`` instance.

    Raises:
        RuntimeError: If ``create_app`` has not been called yet.
    """
    # 依据: Task-R-20261126012656441-P0
    if _access_checker is None:
        raise RuntimeError(
            "AccessChecker not initialised — call create_app() first"
        )
    return _access_checker


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
# Write tools (CAT-19)
# ---------------------------------------------------------------------------


def _register_cat_write_tools(
    server: MCPServer,
    idempotency_checker: "IdempotencyChecker",
) -> None:
    """Register write-related MCP tools (``cat_get_operation``).

    Args:
        server: MCPServer instance.
        idempotency_checker: Initialised :class:`IdempotencyChecker`.
    """
    # 依据: CAT-19

    from cat_mcp.projects import get_current_subject

    @cat_tool(
        server,
        name="cat_get_operation",
        description=(
            "查询指定 request_id 对应的写入操作状态和结果。"
            "用于断线重连后确认请求是否已执行、结果如何。"
            "只读，不修改任何状态。"
        ),
        read_only=True,
    )
    async def get_operation_handler(
        request_id: str,
    ) -> dict[str, Any]:
        """Return the operation record for a given *request_id*."""
        subject = get_current_subject()
        return idempotency_checker.get_operation_result(request_id, subject=subject)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    config: AppConfig,
    *,
    state: StateManager | None = None,
    db_path: str = "/var/lib/cat/state.db",
) -> "Starlette":
    """Create and configure the Cat MCP server ASGI application.

    Initialises an ``MCPServer`` instance, registers all Cat tools, and
    returns a Starlette ASGI application serving the Streamable HTTP
    transport.  Authentication middleware is applied if enabled in the
    configuration.

    Args:
        config: Application configuration (limits, auth, security, etc.).
        state: Optional pre-configured ``StateManager``.  If omitted, one
            is created from *db_path*.
        db_path: Path to the SQLite state database used when *state* is
            not provided.

    Returns:
        A configured Starlette ASGI application.
    """
    # 依据: Task-R-20261126012656436-P0
    # 依据: Task-R-20261126012656440-P0
    server = MCPServer(
        name="cat-mcp",
        version=__version__,
    )

    _register_cat_get_capabilities(server, config)

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656440-P0
    manager = state if state is not None else StateManager(db_path=db_path)

    # ------------------------------------------------------------------
    # Project registry
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656440-P0
    from cat_mcp.projects import (
        SubjectContextMiddleware,
        ProjectRegistry,
        _register_cat_list_projects,
    )

    registry = ProjectRegistry(manager, config)
    _register_cat_list_projects(server, registry)

    # ------------------------------------------------------------------
    # AccessChecker (ACL)
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656441-P0
    from cat_mcp.acl import AccessChecker

    global _access_checker
    _access_checker = AccessChecker(manager)

    # ------------------------------------------------------------------
    # QuotaManager (统一配额)
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656460-P0
    from cat_mcp.fs.quota import QuotaManager, set_quota_manager

    _quota_mgr = QuotaManager(config.limits)
    set_quota_manager(_quota_mgr)

    # ------------------------------------------------------------------
    # Write lock manager (CAT-17) — concurrency control for write operations
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656449-P0
    from cat_mcp.fs.lock import WriteLockManager, set_lock_manager

    _lock_mgr = WriteLockManager()
    set_lock_manager(_lock_mgr)

    # ------------------------------------------------------------------
    # Atomic writer (CAT-18) — crash-safe atomic file writes
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656450-P0
    from cat_mcp.fs.atomic import AtomicWriter, set_atomic_writer
    from cat_mcp.fs.paths import SecurePathResolver
    from cat_mcp.fs.lock import get_lock_manager

    for project_entry in config.projects:
        _project_root = os.path.realpath(project_entry.root)
        _atomic_writer = AtomicWriter(
            project_root=_project_root,
            state_manager=manager,
            resolver=SecurePathResolver(_project_root),
            lock_manager=get_lock_manager(),
        )
        set_atomic_writer(_project_root, _atomic_writer)
        # 启动时执行崩溃恢复（冻结路径记录日志）
        _frozen = _atomic_writer.recover(startup=True)
        if _frozen:
            logger.warning(
                "AtomicWriter recovery: %d path(s) frozen IN_DOUBT "
                "in project %r: %s",
                len(_frozen),
                project_entry.id,
                [f["path"] for f in _frozen],
            )

    # ------------------------------------------------------------------
    # Idempotency checker (CAT-19) — idempotent request handling
    # ------------------------------------------------------------------
    # 依据: CAT-19
    from cat_mcp.fs.idempotency import IdempotencyChecker

    _idempotency_checker = IdempotencyChecker(manager)
    _register_cat_write_tools(server, _idempotency_checker)

    # ------------------------------------------------------------------
    # Audit logger (CAT-27) — read/write/failure/denied event recording
    # ------------------------------------------------------------------
    # 依据: CAT-27
    from cat_mcp.fs.audit import AuditLogger, set_audit_logger, _register_cat_audit_tools

    _audit_logger = AuditLogger(manager)
    set_audit_logger(_audit_logger)
    _register_cat_audit_tools(server, _audit_logger)

    # ------------------------------------------------------------------
    # Workspace manager
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656444-P0
    from cat_mcp.workspace import WorkspaceManager, _register_cat_workspace_tools

    _workspace_mgr = WorkspaceManager(manager, _access_checker, idle_seconds=config.limits.workspace_idle_seconds)
    _register_cat_workspace_tools(server, _workspace_mgr)

    # ------------------------------------------------------------------
    # Directory listing (CAT-13)
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656445-P0
    from cat_mcp.fs.directory import _register_cat_directory_tools

    _register_cat_directory_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # File reader (CAT-14) — stat and segmented read
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656446-P0
    from cat_mcp.fs.reader import _register_cat_reader_tools

    _register_cat_reader_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Text search (CAT-16) — bounded full-text search
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656448-P0
    from cat_mcp.fs.searcher import _register_cat_search_text

    _register_cat_search_text(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # File finder (CAT-15) — glob-based file search
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656447-P0
    from cat_mcp.fs.finder import _register_cat_finder_tools

    _register_cat_finder_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # File writer (CAT-20) — single-file create/replace
    # ------------------------------------------------------------------
    # 依据: CAT-20
    from cat_mcp.fs.writer import _register_cat_write_tools as _register_cat_write_file_tools

    _register_cat_write_file_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Directory operations (CAT-23) — mkdir
    # ------------------------------------------------------------------
    # 依据: CAT-23
    from cat_mcp.fs.directory_ops import _register_cat_directory_ops_tools

    _register_cat_directory_ops_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # File move (CAT-24) — move/rename within project
    # ------------------------------------------------------------------
    # 依据: CAT-24
    from cat_mcp.fs.mover import _register_cat_move_tools

    _register_cat_move_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Trash / soft delete (CAT-26) — move to .cat_trash
    # ------------------------------------------------------------------
    # 依据: CAT-26
    from cat_mcp.fs.trash import _register_cat_trash_tools

    _register_cat_trash_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # File editor (CAT-21) — precise local edit
    # ------------------------------------------------------------------
    # 依据: CAT-21
    from cat_mcp.fs.editor import _register_cat_edit_tools

    _register_cat_edit_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # File copy (CAT-25) — copy single file
    # ------------------------------------------------------------------
    # 依据: CAT-25
    from cat_mcp.fs.copier import _register_cat_copy_tools

    _register_cat_copy_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Patch apply (CAT-22) — strict unified diff
    # ------------------------------------------------------------------
    # 依据: CAT-22
    from cat_mcp.fs.patcher import _register_cat_patch_tools

    _register_cat_patch_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Project info (CAT-29) — build file discovery / tech stack
    # ------------------------------------------------------------------
    # 依据: CAT-29
    from cat_mcp.fs.project_scanner import _register_cat_project_info_tools

    _register_cat_project_info_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Git tools (CAT-30) — read-only git status/diff
    # ------------------------------------------------------------------
    # 依据: CAT-30
    from cat_mcp.fs.git_tools import _register_cat_git_tools

    _register_cat_git_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # Chunked I/O (CAT-33) — chunked upload/download
    # ------------------------------------------------------------------
    # 依据: CAT-33
    from cat_mcp.fs.chunked_io import _register_cat_chunked_io_tools

    _register_cat_chunked_io_tools(server, _workspace_mgr, manager)

    # ------------------------------------------------------------------
    # History tools (CAT-31) — change history query / file diff
    # ------------------------------------------------------------------
    # 依据: CAT-31
    from cat_mcp.fs.history import _register_cat_history_tools

    _register_cat_history_tools(server, _workspace_mgr, manager)

    # Build the Streamable HTTP ASGI app
    app = server.streamable_http_app()

    # ------------------------------------------------------------------
    # Integrate authentication middleware
    # ------------------------------------------------------------------
    # 依据: Task-R-20261126012656437-P0
    from cat_mcp.auth import (
        create_auth_middleware,
        create_protected_resource_route,
    )

    # Subject context middleware — inner layer: runs AFTER auth has set
    # scope["user"] / scope["auth_info"] so it can propagate them into
    # request-local context vars for MCP tool handlers.
    # 依据: Task-R-20261126012656441-P0
    app = SubjectContextMiddleware(app)

    # Auth middleware — outer layer: runs first on each request.  Handles
    # Bearer token validation, Host header checks, and loopback bypass.
    # 依据: Task-R-20261126012656441-P0
    app = create_auth_middleware(app, config)

    # Append OAuth Protected Resource Metadata endpoint (RFC 9728) using
    # the SDK's standard route factory.
    # The route is registered at a path like
    # ``/.well-known/oauth-protected-resource/mcp``.
    try:
        resource_routes = create_protected_resource_route(config)
        if resource_routes:
            from starlette.routing import Router

            router = Router()
            for route in resource_routes:
                router.routes.append(route)

            # Save reference to streaming app before potentially rebinding
            _inner_app = app

            async def combined_app(scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] == "http" and scope.get("path", "").startswith(
                    "/.well-known/"
                ):
                    await router(scope, receive, send)
                else:
                    await _inner_app(scope, receive, send)

            app = combined_app  # type: ignore[assignment]
    except Exception:
        logger.warning(
            "Failed to set up OAuth protected resource metadata route; "
            "continuing without it"
        )

    return app
