"""Project registration, discovery, and authorized project listing.

Provides ProjectRegistry for managing registered projects from configuration
and querying which projects a given subject is authorized to access.  Also
defines the ``cat_list_projects`` MCP tool handler and a per-request context
var for propagating the authenticated subject through the ASGI middleware
chain.

Usage:
    from cat_mcp.projects import ProjectRegistry, _register_cat_list_projects

    registry = ProjectRegistry(state, config)
    _register_cat_list_projects(server, registry)
"""
# 依据: Task-R-20261126012656440-P0

from __future__ import annotations

import base64
import contextvars
import json
import logging
from pathlib import Path
from typing import Any

from cat_mcp.config import AppConfig
from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.state import StateManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-request subject context
# ---------------------------------------------------------------------------
# A ContextVar transports the authenticated subject (set by the ASGI auth
# middleware) into the MCP tool handler that runs in the same implicit task.
# This avoids threading ASGI scope through the SDK's handler layer.

# 依据: Task-R-20261126012656441-P0
_current_auth_scopes: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "cat_current_auth_scopes"
)

_current_subject: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cat_current_subject"
)


def set_current_subject(subject: str) -> None:
    """Set the current request's authenticated subject.

    Called by the ASGI middleware in :func:`create_app` after auth
    verification.
    """
    _current_subject.set(subject)


def get_current_subject() -> str:
    """Return the current authenticated subject.

    Raises:
        CatError: ``UNAUTHORIZED`` if no subject has been set (i.e. the
            request did not go through authentication).
    """
    try:
        return _current_subject.get()
    except LookupError:
        raise CatError(
            ErrorCode.UNAUTHORIZED,
            "No authenticated subject in request context",
        )


def set_current_auth_scopes(scopes: list[str] | None) -> None:
    """Set the current request's OAuth scopes.

    Called by the ASGI middleware in :func:`create_app` after auth
    verification.  Pass ``None`` when auth is bypassed (loopback /
    development mode).

    Args:
        scopes: OAuth scopes from the token, or ``None`` if auth is
            bypassed.
    """
    # 依据: Task-R-20261126012656441-P0
    _current_auth_scopes.set(scopes)


def get_current_auth_scopes() -> list[str] | None:
    """Return the current request's OAuth scopes.

    Returns:
        A list of scope strings if authentication was performed, or
        ``None`` if auth was bypassed (loopback / development mode).
    """
    # 依据: Task-R-20261126012656441-P0
    try:
        return _current_auth_scopes.get()
    except LookupError:
        return None


# ---------------------------------------------------------------------------
# ASGI middleware for subject context propagation
# ---------------------------------------------------------------------------


class SubjectContextMiddleware:
    """ASGI middleware that extracts the authenticated subject and OAuth
    scopes from the ASGI scope and stores them in per-request context vars.

    Must be placed **after** ``AuthMiddleware`` in the middleware chain so
    that ``scope["user"]`` and ``scope["auth_info"]`` are already populated.

    Usage::

        app = SubjectContextMiddleware(app)
    """

    def __init__(self, app: Any) -> None:
        """Wrap an ASGI application.

        Args:
            app: The downstream ASGI application (typically wrapped with
                auth middleware already).
        """
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI entry point — propagate subject and scopes if present."""
        # 依据: Task-R-20261126012656441-P0
        if scope["type"] == "http":
            subject: str | None = scope.get("user")
            if subject:
                set_current_subject(subject)

            # Propagate OAuth scopes from auth_info for ACL intersection
            auth_info: dict | None = scope.get("auth_info")
            if auth_info:
                scopes: list[str] | None = auth_info.get("scopes")
                set_current_auth_scopes(scopes)
            else:
                # Auth bypassed (loopback / development) — set None so ACL
                # skips OAuth scope intersection
                set_current_auth_scopes(None)
        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# Pagination helpers (offset-based cursor encoded as base64 JSON)
# ---------------------------------------------------------------------------


def _encode_cursor(offset: int) -> str:
    """Encode an integer offset into an opaque cursor string."""
    return base64.b64encode(json.dumps({"o": offset}).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    """Decode an opaque *cursor* back to an integer offset.

    Raises:
        CatError (INVALID_ARGUMENT): the cursor is malformed.
    """
    try:
        payload = json.loads(base64.b64decode(cursor))
        return int(payload["o"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise CatError(ErrorCode.INVALID_ARGUMENT, "Invalid cursor")


# ---------------------------------------------------------------------------
# ProjectRegistry
# ---------------------------------------------------------------------------


class ProjectRegistry:
    """Manages registered projects and subject-level authorization.

    On initialisation, imports projects declared in the configuration into
    the persistent state database (upsert by ID).  Provides query methods
    that respect the grant-based authorisation model.
    """

    def __init__(self, state: StateManager, config: AppConfig) -> None:
        """Initialize registry and import config-registered projects.

        Args:
            state: Persistent state manager (SQLite-backed).
            config: Application configuration (``projects`` list).
        """
        # 依据: Task-R-20261126012656440-P0
        self._state = state
        self._config = config
        self._import_config_projects()

    # ------------------------------------------------------------------
    # Configuration import
    # ------------------------------------------------------------------

    def _import_config_projects(self) -> None:
        """Import or update projects declared in the configuration.

        Existing projects are updated (display name, root path); new
        projects are created.  The project ID is the stable, immutable
        key supplied by configuration.

        Access scopes declared in the configuration are also synchronised
        to the grants table for a system-level subject ("*admin*").
        """
        # 依据: Task-R-20261126012656440-P0
        for entry in self._config.projects:
            display_name = _derive_display_name(entry.id, entry.root)

            existing = self._state.get_project(entry.id)
            if existing is not None:
                self._state.update_project(
                    id=entry.id,
                    display_name=display_name,
                    root_path=entry.root,
                )
            else:
                self._state.create_project(
                    id=entry.id,
                    display_name=display_name,
                    root_path=entry.root,
                )

            # Synchronise access scopes from configuration into grants.
            # Uses a fixed pseudo-subject ("*config*") so operator-declared
            # grants can be distinguished from runtime grants.
            # 依据: Task-R-20261126012656440-P0
            scopes = ",".join(entry.access) if entry.access else "read"
            existing_grants = self._state.get_grants("*config*", entry.id)
            if not existing_grants:
                self._state.create_grant(
                    subject="*config*",
                    project_id=entry.id,
                    scopes=scopes,
                )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def list_projects(
        self,
        subject: str,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return projects the *subject* is authorised to access.

        Args:
            subject: Authenticated user identifier (from token or loopback
                context).
            query: Optional case-insensitive filter on ``display_name`` or
                ``id``.
            cursor: Opaque pagination cursor (first page: ``None``).
            limit: Maximum entries per page (1–200, default 20).

        Returns:
            A dict with keys ``projects`` (list of project info dicts),
            ``has_more`` (bool), and ``next_cursor`` (str or ``None``).

        Each project dict contains:
            - ``id``: Stable project identifier.
            - ``display_name``: Human-readable name.
            - ``root_relative``: Directory identifier (not an absolute path).
            - ``access``: Sorted list of granted access scopes.

        Raises:
            CatError (INVALID_ARGUMENT): if *limit* is out of range.
        """
        # 依据: Task-R-20261126012656440-P0
        if limit < 1 or limit > 200:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"limit must be between 1 and 200, got {limit}",
            )

        all_projects = self._state.list_projects()

        # Build authorised list — each project must have at least one grant
        # for the subject and be enabled.
        authorised: list[dict[str, Any]] = []

        for proj in all_projects:
            if not proj.get("enabled", 1):
                continue

            grants = self._state.get_grants(subject, proj["id"])
            if not grants:
                continue

            # Collect unique scopes across all grants
            scopes: set[str] = set()
            for g in grants:
                for s in g.get("scopes", "read").split():
                    scopes.add(s)

            root_relative = _derive_root_relative(proj)
            authorised.append(
                {
                    "id": proj["id"],
                    "display_name": proj.get(
                        "display_name", proj["id"]
                    ),
                    "root_relative": root_relative,
                    "access": sorted(scopes),
                }
            )

        # Apply query filter
        if query:
            q_lower = query.lower()
            authorised = [
                p
                for p in authorised
                if q_lower in p["display_name"].lower()
                or q_lower in p["id"].lower()
            ]

        # Paginate
        offset = _decode_cursor(cursor) if cursor else 0
        end = offset + limit
        page = authorised[offset:end]
        has_more = end < len(authorised)

        return {
            "projects": page,
            "has_more": has_more,
            "next_cursor": _encode_cursor(end) if has_more else None,
        }

    def get_project(self, project_id: str, subject: str | None = None) -> dict[str, Any] | None:
        """Look up a project by its stable ID.

        When *subject* is provided the call also verifies that the
        subject has a valid grant for the project; if not, returns
        ``None``.

        .. warning::
            Callers that already performed an authorisation check may
            pass ``subject=None``, but this should be rare.

        Returns:
            The project record as a dict, or ``None`` if not found or
            not authorised.
        """
        # 依据: Task-R-20261126012656440-P0
        row = self._state.get_project(project_id)
        if row is None:
            return None
        if subject is not None:
            grants = self._state.get_grants(subject, project_id)
            if not grants:
                return None
        return row

    # ------------------------------------------------------------------
    # Discovery (optional — stub)
    # ------------------------------------------------------------------

    def discover_projects(self) -> list[dict[str, Any]]:
        """Scan the configured discovery root for candidate directories.

        .. note::
            Discovery returns **candidates** that require explicit
            administrative approval before they become accessible.
            This feature is not yet fully wired and requires a
            ``discovery_root`` configuration field (out of scope for
            CAT-08).

        Returns:
            An empty list (stub implementation).
        """
        # 依据: Task-R-20261126012656440-P0 — 发现不自动授权
        logger.debug("discover_projects() called but not yet implemented")
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _derive_display_name(project_id: str, root_path: str) -> str:
    """Derive a human-readable display name from config fields.

    Prefers the last path component of *root_path*; falls back to
    *project_id*.
    """
    try:
        name = Path(root_path).name
        if name:
            return name
    except Exception:
        pass
    return project_id


def _derive_root_relative(proj: dict[str, Any]) -> str:
    """Derive a safe relative identifier for the project root.

    Returns the last component of the stored ``root_path``, or the
    project ID as a fallback.  Never exposes the full absolute path.
    """
    root_path = proj.get("root_path", "")
    try:
        relative = Path(root_path).name
        if relative:
            return relative
    except Exception:
        pass
    return proj.get("id", "unknown")


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def _register_cat_list_projects(
    server: Any,
    registry: ProjectRegistry,
) -> None:
    """Register the ``cat_list_projects`` MCP tool on *server*.

    The tool returns the list of projects the currently authenticated
    subject is authorised to access.  Supports optional name filtering
    (``query``), offset-based pagination (``cursor``, ``limit``).

    Args:
        server: ``MCPServer`` instance.
        registry: Initialised :class:`ProjectRegistry`.
    """
    # 依据: Task-R-20261126012656440-P0

    # Lazy import to avoid circular dependency at module level
    from cat_mcp.server import cat_tool

    @cat_tool(
        server,
        name="cat_list_projects",
        description=(
            "返回当前授权主体可访问的工程列表。支持分页和名称查询过滤。"
        ),
        read_only=True,
    )
    async def list_projects_handler(
        query: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Handle ``cat_list_projects`` MCP tool call.

        Retrieves the authenticated subject from the per-request context
        var, then delegates to the registry.
        """
        # 依据: Task-R-20261126012656440-P0
        subject = get_current_subject()
        return registry.list_projects(
            subject=subject,
            query=query,
            cursor=cursor,
            limit=limit or 20,
        )
