"""Workspace 管理和虚拟 CWD 服务。

提供工作空间生命周期管理（打开、查询、设 CWD、关闭），
每工作空间绑定主体和工程，追踪 CWD 版本用于并发控制。

用法::

    from cat_mcp.workspace import WorkspaceManager

    mgr = WorkspaceManager(state, registry, checker)
    ws = mgr.open_workspace("user-abc", "demo-app")
    # ws = {workspace_id, cwd, cwd_version, expires_at, ...}
"""
# 依据: Task-R-20261126012656444-P0

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.state import StateManager
from cat_mcp.acl import AccessChecker
from cat_mcp.fs.paths import _validate_path_format, _normalize

logger = logging.getLogger(__name__)

# 默认工作空间闲置超时（秒）
_DEFAULT_IDLE_SECONDS = 7200


class WorkspaceManager:
    """工作空间管理器。

    管理工作空间的打开、查询、CWD 切换、关闭和过期清理。

    Args:
        state: 持久状态管理器（SQLite 后端）。
        access_checker: ACL 授权检查器。
        idle_seconds: 工作空间闲置超时秒数。
    """

    def __init__(
        self,
        state: StateManager,
        access_checker: AccessChecker,
        idle_seconds: int = _DEFAULT_IDLE_SECONDS,
    ) -> None:
        self._state = state
        self._checker = access_checker
        self._idle_seconds = idle_seconds

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def open_workspace(
        self,
        subject: str,
        project_id: str,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """为 *subject* 在 *project_id* 上打开一个新工作空间。

        校验步骤：
        1. 工程存在且启用
        2. subject 对该工程有 read 授权
        3. 创建 workspace（分配不可预测随机 ID，绑定主体和工程）

        Args:
            subject: 已认证的主体标识。
            project_id: 工程标识。
            cwd: 初始工作目录（默认工程根 ``"."``）。

        Returns:
            工作空间信息：workspace_id、cwd、cwd_version、expires_at。

        Raises:
            CatError (PROJECT_NOT_FOUND): 工程不存在或未启用。
            CatError (FORBIDDEN): 主体无该工程的授权。
        """
        # 1. 工程存在且启用
        project = self._state.get_project(project_id)
        if project is None or not project.get("enabled", 1):
            raise CatError(
                ErrorCode.PROJECT_NOT_FOUND,
                "Project not found or disabled",
            )

        # 2. 授权校验（至少需要 read）
        if not self._checker.check(subject, project_id, ".", "read"):
            raise CatError(
                ErrorCode.FORBIDDEN,
                "Not authorized to access this project",
            )

        # 3. 校验 cwd 参数格式（如果有）
        if cwd is not None:
            _validate_path_format(cwd)

        # 4. 创建 workspace
        initial_cwd = cwd if cwd else "."
        expires_at = self._compute_expiry()
        workspace = self._state.create_workspace(
            subject=subject,
            project_id=project_id,
            cwd=initial_cwd,
            project_generation=project.get("generation", 1),
            expires_at=expires_at,
        )
        if workspace is None:
            raise CatError(
                ErrorCode.INTERNAL_ERROR,
                "Failed to create workspace",
            )

        return self._format_workspace(workspace)

    def get_workspace(
        self,
        subject: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """查询 *workspace_id* 的当前状态。

        校验：
        1. workspace 存在
        2. workspace 属于该 subject
        3. workspace 未过期关闭

        Args:
            subject: 已认证的主体标识。
            workspace_id: 工作空间标识。

        Returns:
            工作空间信息。

        Raises:
            CatError (WORKSPACE_NOT_FOUND): 找不到工作空间或不属于该主体。
            CatError (WORKSPACE_EXPIRED): 工作空间已过期。
        """
        workspace = self._get_active_workspace(subject, workspace_id)
        return self._format_workspace(workspace)

    def set_cwd(
        self,
        subject: str,
        workspace_id: str,
        path: str,
        expected_cwd_version: int,
    ) -> dict[str, Any]:
        """切换工作空间的当前工作目录。

        使用 expected_cwd_version 做乐观锁控制（CAS）。

        Args:
            subject: 已认证的主体标识。
            workspace_id: 工作空间标识。
            path: 目标相对当前 CWD 的路径。
            expected_cwd_version: 预期当前 CWD 版本。

        Returns:
            更新后的工作空间信息。

        Raises:
            CatError (WORKSPACE_NOT_FOUND/EXPIRED): 工作空间不可用。
            CatError (CWD_CONFLICT): 版本不匹配。
            CatError (INVALID_PATH): 路径格式非法。
        """
        # 验证 workspace 归属
        workspace = self._get_active_workspace(subject, workspace_id)

        # 获取工程根以校验路径不越界
        project_id = workspace.get("project_id", "")
        project = self._state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        # 校验路径合法性（格式检查 + 不越根词法检查）
        _validate_path_format(path)
        current_cwd = workspace.get("cwd", ".")
        normalized = _normalize(path, base=current_cwd)
        # os.path.normpath 解析 .. 段，然后检查是否越根
        import os as _os
        normalized_real = _os.path.normpath(normalized)
        if normalized_real.startswith(".."):
            raise CatError(
                ErrorCode.PATH_OUT_OF_BOUNDS,
                "Path escapes project root",
            )

        # 执行 CAS 更新
        updated = self._state.update_workspace_cwd(
            id=workspace_id,
            cwd=path,
            expected_cwd_version=expected_cwd_version,
        )
        if updated is None:
            raise CatError(
                ErrorCode.CWD_CONFLICT,
                "CWD version mismatch; call get_workspace to refresh",
            )

        return self._format_workspace(updated)

    def close_workspace(
        self,
        subject: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """关闭 *workspace_id*。

        Args:
            subject: 已认证的主体标识。
            workspace_id: 工作空间标识。

        Returns:
            关闭确认回执。

        Raises:
            CatError (WORKSPACE_NOT_FOUND/EXPIRED): 不可用。
        """
        self._get_active_workspace(subject, workspace_id)
        closed = self._state.close_workspace(workspace_id)
        if not closed:
            raise CatError(
                ErrorCode.INTERNAL_ERROR,
                "Failed to close workspace",
            )
        return {
            "workspace_id": workspace_id,
            "status": "closed",
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_active_workspace(
        self,
        subject: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """获取并验证 *workspace_id* 对 *subject* 活跃。

        Returns:
            workspace 记录。

        Raises:
            CatError: 不存在、不属于该主体或已过期。
        """
        workspace = self._state.get_workspace(workspace_id)
        if workspace is None:
            raise CatError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                "Workspace not found",
            )

        # 归属校验
        if workspace.get("subject") != subject:
            raise CatError(
                ErrorCode.WORKSPACE_NOT_FOUND,
                "Workspace not found",
            )

        # 过期校验
        if workspace.get("status") != "active":
            raise CatError(
                ErrorCode.WORKSPACE_EXPIRED,
                "Workspace is not active",
            )

        # 授权重校验（防止打开后授权被撤销）
        project_id = workspace.get("project_id", "")
        if not self._checker.check(subject, project_id, ".", "read"):
            raise CatError(
                ErrorCode.FORBIDDEN,
                "Authorization revoked for this project",
            )

        expires_at_str = workspace.get("expires_at")
        if expires_at_str:
            expires_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if expires_dt < datetime.now(timezone.utc):
                raise CatError(
                    ErrorCode.WORKSPACE_EXPIRED,
                    "Workspace has expired",
                )

        return workspace

    def _compute_expiry(self) -> str:
        """计算工作空间到期时间。

        Returns:
            RFC3339 格式的到期时间戳。
        """
        expires = datetime.now(timezone.utc) + timedelta(seconds=self._idle_seconds)
        return expires.strftime("%Y-%m-%dT%H:%M:%S.") + f"{expires.microsecond // 1000:03d}Z"

    @staticmethod
    def _format_workspace(ws: dict[str, Any]) -> dict[str, Any]:
        """格式化 workspace 对外返回。

        Returns:
            暴露给客户端的字段（不暴露主机内部路径等敏感信息）。
        """
        return {
            "workspace_id": ws["id"],
            "project_id": ws.get("project_id", ""),
            "cwd": ws.get("cwd", "."),
            "cwd_version": ws.get("cwd_version", 1),
            "expires_at": ws.get("expires_at"),
            "status": ws.get("status", "active"),
        }


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_workspace_tools(
    server: Any,
    workspace_mgr: WorkspaceManager,
) -> None:
    """注册工作空间相关的 MCP 工具（``cat_open_workspace`` 等）。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: 已初始化的 :class:`WorkspaceManager`。
    """
    # 延迟导入避免循环
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    @cat_tool(
        server,
        name="cat_open_workspace",
        description="爲當前主體在指定工程上打開一個新的工作空間，獲取 workspace_id 和初始 cwd_version。",
        read_only=False,
    )
    async def open_workspace_handler(
        project_id: str,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        subject = get_current_subject()
        return workspace_mgr.open_workspace(subject, project_id, cwd)

    @cat_tool(
        server,
        name="cat_get_workspace",
        description="查詢當前工作空間狀態，包括 CWD、版本、過期時間等。",
        read_only=True,
    )
    async def get_workspace_handler(
        workspace_id: str,
    ) -> dict[str, Any]:
        subject = get_current_subject()
        return workspace_mgr.get_workspace(subject, workspace_id)

    @cat_tool(
        server,
        name="cat_set_cwd",
        description=(
            "切換當前工作空間的相對工作目錄。"
            "支持 '.'、'..' 和子目錄，但不得越出工程根。"
            "不切換系統進程工作目錄。"
        ),
        read_only=False,
    )
    async def set_cwd_handler(
        workspace_id: str,
        path: str,
        expected_cwd_version: int,
    ) -> dict[str, Any]:
        subject = get_current_subject()
        return workspace_mgr.set_cwd(subject, workspace_id, path, expected_cwd_version)

    @cat_tool(
        server,
        name="cat_close_workspace",
        description="關閉工作空間，釋放服務端資源。",
        read_only=False,
    )
    async def close_workspace_handler(
        workspace_id: str,
    ) -> dict[str, Any]:
        subject = get_current_subject()
        return workspace_mgr.close_workspace(subject, workspace_id)
