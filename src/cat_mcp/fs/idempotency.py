"""幂等请求检查器。

提供 :class:`IdempotencyChecker` 用于在写操作执行前检查
``request_id`` 是否已存在，实现幂等语义：

- 同 request_id + 同 input_digest → 返回已有操作（幂等）
- 同 request_id + 不同 input_digest → 拒绝（REQUEST_ID_CONFLICT）
"""
# 依据: CAT-19

from __future__ import annotations

from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.state import StateManager


class IdempotencyChecker:
    """幂等请求检查器。

    在写操作执行前检查 ``request_id`` 是否已存在：

    - 同 ID + 同参数 → 返回已有结果（幂等）
    - 同 ID + 不同参数 → 拒绝（REQUEST_ID_CONFLICT）

    Args:
        state: 已初始化的 :class:`StateManager` 实例。
    """

    def __init__(self, state: StateManager) -> None:
        # 依据: CAT-19
        self._state = state

    def check_or_create(
        self,
        subject: str,
        workspace_id: str,
        request_id: str,
        path: str,
        input_digest: str | None = None,
    ) -> tuple[str, bool]:
        """幂等检查：确认 request_id 是否可复用或需创建新操作。

        Args:
            subject: 主体标识。
            workspace_id: 工作空间 ID。
            request_id: 请求标识（幂等判断键）。
            path: 操作目标工程相对路径。
            input_digest: 输入内容的 SHA-256 哈希。

        Returns:
            ``(operation_id, is_new)`` 元组。

        Raises:
            CatError: ``REQUEST_ID_CONFLICT`` — 同 request_id 但 input_digest 不同。
        """
        # 依据: CAT-19
        op, is_new = self._state.create_operation_idempotent(
            subject=subject,
            workspace_id=workspace_id,
            request_id=request_id,
            path=path,
            input_digest=input_digest,
        )
        return op["id"], is_new

    def get_operation_result(self, request_id: str) -> dict[str, Any]:
        """查询操作结果。

        按 ``request_id`` 查询操作记录，返回操作状态和全部字段。

        Args:
            request_id: 请求标识。

        Returns:
            操作记录字典。

        Raises:
            CatError: ``OPERATION_FAILED`` — 未找到对应操作。
        """
        # 依据: CAT-19
        op = self._state.get_operation(request_id)
        if op is None:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Operation not found for request_id {request_id!r}",
            )
        return op
