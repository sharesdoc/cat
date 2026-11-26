"""ACL 授权模块：主体→工程→路径→动作 四级授权校验，deny-by-default。

提供 :class:`AccessChecker` 进行每次调用的授权重验。每次调用独立校验，
不缓存授权结果，不泄露拒绝原因细节。

用法::

    from cat_mcp.acl import AccessChecker

    checker = AccessChecker(state)
    ok = checker.check(subject, project_id, "/some/path", "read")
"""
# 依据: Task-R-20261126012656441-P0

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cat_mcp.state import StateManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 动作定义
# ---------------------------------------------------------------------------

ACTIONS: frozenset[str] = frozenset({
    "discover",
    "read",
    "write",
    "delete",
    "restore",
    "audit",
    "workspace",
})


# ---------------------------------------------------------------------------
# AccessChecker
# ---------------------------------------------------------------------------


class AccessChecker:
    """主体→工程→路径→动作 四级授权校验，deny-by-default。

    每次调用都重验授权，不缓存授权结果。不泄露拒绝原因细节（统一返回
    ``False``）。

    Args:
        state: 持久状态管理器（SQLite 后端）。
    """

    def __init__(self, state: StateManager) -> None:
        """初始化 AccessChecker。

        Args:
            state: 持久状态管理器。
        """
        # 依据: Task-R-20261126012656441-P0
        self._state = state

    # ------------------------------------------------------------------
    # 核心校验
    # ------------------------------------------------------------------

    def check(
        self,
        subject: str,
        project_id: str,
        path: str,
        action: str,
    ) -> bool:
        """检查 *subject* 是否能在 *project_id* 的 *path* 上执行 *action*。

        校验顺序（全部通过才返回 ``True``，任一不通过立即返回 ``False``）：

        1. *action* 是否在已定义的动作集中
        2. 工程是否存在
        3. 工程是否启用（``enabled`` 字段）
        4. *subject* + *project_id* 是否存在有效 grant
        5. *action* 是否在该 grant 的 ``scopes`` 内
        6. *path* 是否以该 grant 的某个 ``allowed_prefixes`` 开头
        7. OAuth scope 与 grant scopes 取交集（auth 未绕过时）

        Args:
            subject: 已认证的主体标识（来自 JWT ``sub`` 或 loopback 上下文）。
            project_id: 工程标识。
            path: 请求的路径（可为空字符串 ``""``）。
            action: 请求的动作（见 :data:`ACTIONS`）。

        Returns:
            ``True`` 当所有校验通过，否则 ``False``。
        """
        # 依据: Task-R-20261126012656441-P0
        # 1. action 合法性
        if action not in ACTIONS:
            return False

        # 2-3. 工程存在且启用
        project: dict[str, Any] | None = self._state.get_project(project_id)
        if project is None:
            return False
        if not project.get("enabled", 1):
            return False

        # 4. subject + project_id 有效 grant
        grants: list[dict[str, Any]] = self._state.get_grants(subject, project_id)
        if not grants:
            return False

        # 获取 OAuth scopes（可能为 None 表示 auth 绕过 / 本地开发）
        from cat_mcp.projects import get_current_auth_scopes

        oauth_scopes: list[str] | None = get_current_auth_scopes()
        oauth_scope_set: set[str] | None = (
            set(oauth_scopes) if oauth_scopes is not None else None
        )

        # 逐 grant 校验
        for grant in grants:
            # 5. action 在 grant scopes 内
            grant_scopes: set[str] = set(grant.get("scopes", "read").split())
            if action not in grant_scopes:
                continue

            # 6. OAuth scope 交集：auth 未绕过时 action 必须在 OAuth scopes 内
            if oauth_scope_set is not None and action not in oauth_scope_set:
                continue

            # 7. 路径前缀校验
            allowed_raw: str = grant.get("allowed_prefixes", "/")
            for prefix in allowed_raw.split(","):
                prefix_stripped = prefix.strip()
                if path.startswith(prefix_stripped):
                    return True

        # deny-by-default
        return False

    def check_workspace(
        self,
        subject: str,
        workspace_id: str,
        action: str,
    ) -> bool:
        """检查 *subject* 是否能对 *workspace_id* 执行 *action*。

        校验顺序：

        1. *action* 合法性
        2. workspace 存在
        3. workspace 属于该 *subject*
        4. workspace 状态为 ``active``
        5. workspace 未过期
        6. OAuth scope 校验（auth 未绕过时）

        Args:
            subject: 已认证的主体标识。
            workspace_id: Workspace 标识。
            action: 请求的动作。

        Returns:
            ``True`` 当所有校验通过，否则 ``False``。
        """
        # 依据: Task-R-20261126012656441-P0
        if action not in ACTIONS:
            return False

        workspace: dict[str, Any] | None = self._state.get_workspace(workspace_id)
        if workspace is None:
            return False

        # 归属校验
        if workspace.get("subject") != subject:
            return False

        # 状态校验
        if workspace.get("status") != "active":
            return False

        # 过期校验
        expires_at: str | None = workspace.get("expires_at")
        if expires_at:
            now = _now_str()
            if expires_at < now:
                return False

        # OAuth scope 校验
        from cat_mcp.projects import get_current_auth_scopes

        oauth_scopes: list[str] | None = get_current_auth_scopes()
        if oauth_scopes is not None and action not in oauth_scopes:
            return False

        return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_str() -> str:
    """Return current UTC time as RFC3339 millisecond string.

    Example: ``2026-11-27T01:55:30.123Z``
    """
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
