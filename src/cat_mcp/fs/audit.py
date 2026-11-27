"""Operation audit recorder.

Records read/write/failure/denied events without leaking file contents
or tokens.  Provides a global singleton accessor (``set_audit_logger`` /
``get_audit_logger``) and an MCP tool for querying the audit log.
"""
# 依据: CAT-27

from __future__ import annotations

import os
import logging
from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.state import StateManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class AuditLogger:
    """Operation audit recorder.

    Records read/write/failure/denied events and provides read-only
    query access to the persistent audit log.

    Key security constraints enforced by :meth:`log`:

    * The *path* must be a project-relative path (absolute paths are
      rejected).
    * No file content hashes are recorded (those belong to the operations
      table).
    * No tokens or credentials are ever logged.

    Args:
        state_manager: An initialised :class:`StateManager`.
    """

    def __init__(self, state_manager: StateManager) -> None:
        """Initialise the logger with a persistent state backend.

        Args:
            state_manager: StateManager instance (must be ready).
        """
        self._state = state_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(
        self,
        subject: str,
        project_id: str,
        action: str,
        outcome: str,
        path: str = "",
        workspace_id: str | None = None,
        bytes: int = 0,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an audit event.

        No file contents or tokens are ever written to the audit log.

        Args:
            subject: 操作主体标识。
            project_id: 所属工程 ID。
            action: 操作类型。预定义值：
                ``"read"``, ``"write.create"``, ``"write.replace"``,
                ``"delete"``, ``"list"``, ``"search"``, ``"denied"``。
            outcome: 操作结果：``"success"``, ``"failure"``, ``"denied"``。
            path: 工程相对路径。**必须为相对路径**，拒绝绝对路径。
            workspace_id: 可选的工作空间 ID。
            bytes: 操作涉及的数据量（字节数）。
            correlation_id: 可选的关联标识（如请求追踪 ID）。

        Returns:
            审计事件记录的字典表示。

        Raises:
            ValueError: 如果 *path* 是绝对路径。
        """
        # 安全约束：拒绝绝对路径，防止泄露文件系统结构
        # 依据: CAT-27
        if path and os.path.isabs(path):
            raise ValueError("Absolute path not allowed in audit log")

        return self._state.log_audit_event(
            subject=subject,
            project_id=project_id,
            action=action,
            workspace_id=workspace_id,
            canonical_relative_path=path,
            outcome=outcome,
            bytes=bytes,
            correlation_id=correlation_id,
        )

    def query(
        self,
        subject: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log entries, ordered by timestamp descending.

        Args:
            subject: 可选的主体过滤。
            project_id: 可选的工程 ID 过滤。
            limit: 最大返回条目数（默认 100，上限 1000）。

        Returns:
            审计事件记录的列表。
        """
        safe_limit = max(1, min(limit, 1000))
        return self._state.query_audit(
            subject=subject,
            project_id=project_id,
            limit=safe_limit,
        )


# ---------------------------------------------------------------------------
# Global accessors (singleton pattern)
# ---------------------------------------------------------------------------

_audit_logger: AuditLogger | None = None


def set_audit_logger(logger: AuditLogger) -> None:
    """Register the global :class:`AuditLogger` instance.

    Must be called during application initialisation before any tool
    handler attempts to use the audit logger.

    Args:
        logger: An initialised AuditLogger.
    """
    global _audit_logger
    _audit_logger = logger


def get_audit_logger() -> AuditLogger:
    """Return the global :class:`AuditLogger` instance.

    Raises:
        RuntimeError: If ``set_audit_logger`` has not been called yet.
    """
    if _audit_logger is None:
        raise RuntimeError("AuditLogger not initialised")
    return _audit_logger


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def _register_cat_audit_tools(server: Any, audit_logger: AuditLogger) -> None:
    """Register audit-related MCP tools (``cat_query_audit``).

    Args:
        server: ``MCPServer`` instance.
        audit_logger: Initialised :class:`AuditLogger`.
    """
    # 依据: CAT-27

    from cat_mcp.projects import get_current_subject
    from cat_mcp.server import cat_tool
    from cat_mcp.acl import get_access_checker

    @cat_tool(
        server,
        name="cat_query_audit",
        description=(
            "查询审计日志。支持按主体（subject）和工程（project_id）过滤，"
            "结果按时间倒序排列。只读，不修改任何状态。"
            "仅拥有 audit 权限的主体可查询。"
        ),
        read_only=True,
    )
    async def query_audit_handler(
        subject: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Handle ``cat_query_audit`` MCP tool call.

        Args:
            subject: 可选的主体过滤。不传则查询当前主体。
            project_id: 可选的工程 ID 过滤。必填。
            limit: 最大返回条目数（默认 100）。

        Returns:
            审计事件记录的列表，按时间倒序排列。
        """
        current_subject = get_current_subject()
        if not project_id:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "project_id is required for audit query",
            )

        # 校验主体是否有 audit 权限
        checker = get_access_checker()
        checker.check(current_subject, project_id, ".", "audit")

        # 如果客户端未指定 subject，强制限定为当前主体
        if subject is None:
            subject = current_subject

        return audit_logger.query(
            subject=subject,
            project_id=project_id,
            limit=limit,
        )
