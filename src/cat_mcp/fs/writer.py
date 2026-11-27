"""单文件创建和完整写入 MCP 工具。

提供 ``cat_write_file`` MCP 工具，支持新建（create）和替换（replace）两种模式。
新建不覆盖已有文件，替换需指定旧版本哈希。

安全约束：
- SecurePathResolver 拒绝 symlink/special files
- SensitiveFileChecker 拒绝敏感文件写入
- AccessChecker 授权校验
- QuotaManager 并发配额 + 内容大小检查
- WriteLockManager 锁内版本校验（消除 TOCTOU）
- AtomicWriter 原子写入 + 崩溃恢复
- IdempotencyChecker 幂等去重
- AuditLogger 审计记录（相对路径，不泄露文件系统结构）

编码约束：
- 使用 ``errors="surrogateescape"`` 避免 surrogate 字符导致
  UnicodeEncodeError；由 AtomicWriter 确保内容与磁盘写入一致。
"""
# 依据: CAT-20

from __future__ import annotations

import logging
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_write_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_write_file`` 写入工具。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例。
        state: ``StateManager`` 实例。
    """
    # 延迟导入避免循环依赖
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    # ------------------------------------------------------------------
    # cat_write_file
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_write_file",
        description=(
            "创建一个新文件或以指定版本替换已有文件。"
            "mode='create' 时目标文件必须不存在；mode='replace' 时必须传 expected_version。"
            "仅支持 UTF-8 文本内容。返回写入后的版本哈希和操作 ID。"
        ),
        destructive=True,
    )
    async def cat_write_file_handler(
        workspace_id: str,
        path: str,
        content: str,
        mode: str = "create",
        expected_version: str | None = None,
        path_base: str | None = None,
        request_id: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_write_file`` MCP 工具调用。

        处理流程：
          1. 获取 subject, 校验 workspace 归属
          2. 获取 project_id, project_root
          3. 确定 base 路径
          4. 并发配额检查 + content 大小校验
          5. 校验 mode 参数
          6. 安全路径解析 + 编码 content + 敏感文件检查 + ACL 授权
          7. AtomicWriter.prepare_write（锁内版本校验）→ commit_write
          8. 审计日志记录（使用工程相对路径）
          9. 返回 version hash + operation_id

        Args:
            workspace_id: 工作空间标识。
            path: 写入目标路径（相对工程根或 path_base）。
            content: 文件内容（UTF-8 文本）。
            mode: "create"（新建）或 "replace"（替换）。
            expected_version: 替换模式必须传旧版本哈希。
            path_base: "cwd" 或 "root"。
            request_id: 幂等请求 ID。
            cwd_version: 可选 CWD 版本校验。

        Returns:
            {"version": str, "operation_id": str}

        Raises:
            CatError: 各类业务错误。
        """
        # 1. 获取认证主体
        subject = get_current_subject()

        # 2. 获取工作空间（校验归属/过期）
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        # 3. 获取工程根路径
        project = state.get_project(project_id)
        if project is None:
            raise CatError(
                ErrorCode.PROJECT_NOT_FOUND,
                "Project not found",
            )

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(
                ErrorCode.INTERNAL_ERROR,
                "Project root path not configured",
            )

        # 4. 校验 content 大小（在 encode 前检查字符串大小）
        from cat_mcp.fs.quota import get_quota_manager, check_response_size, check_concurrency

        check_response_size(content)

        # 5. 校验 mode 参数
        if mode not in ("create", "replace"):
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"mode must be 'create' or 'replace', got {mode!r}",
            )

        # 5a. replace 模式必须传 expected_version
        if mode == "replace" and expected_version is None:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "expected_version is required when mode='replace'",
            )

        # 6. 编码 content 为 UTF-8 bytes（safe-encode 避免 surrogate 崩溃）
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            raise CatError(
                ErrorCode.UNSUPPORTED_ENCODING,
                "Content contains invalid surrogate characters",
            )

        # 7. 确定 path_base（默认 CWD）
        if path_base is None:
            path_base = "cwd"
        base = _resolve_base(path_base, workspace)

        # 创建 SecurePathResolver
        from cat_mcp.fs.paths import SecurePathResolver

        resolver = SecurePathResolver(project_root)

        # 7. 单次安全解析路径，并在同一 resolve 块内完成 create 模式检查
        with resolver.resolve(path, base=base) as rp:
            resolved_path = rp.resolved_path

            # create 模式：检查目标是否已存在（复用本次 resolve，避免二次 resolve）
            if mode == "create":
                try:
                    rp.stat(follow_symlinks=False)
                    # stat 无异常 → 文件/目录已存在
                    raise CatError(
                        ErrorCode.FILE_ALREADY_EXISTS,
                        f"File already exists: {path}",
                    )
                except CatError as exc:
                    if exc.error_code == ErrorCode.FILE_NOT_FOUND:
                        pass  # 不存在，可以创建
                    elif exc.error_code in (
                        ErrorCode.FILE_ALREADY_EXISTS,
                        ErrorCode.IS_DIRECTORY,
                    ):
                        raise
                    else:
                        # 其他错误（越界、不安全路径等）正常传播
                        raise

        # 8. 敏感文件检查
        from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker

        if get_sensitive_checker().is_sensitive(resolved_path):
            raise CatError(
                ErrorCode.FORBIDDEN,
                "Access to sensitive file is denied",
            )

        # 9. ACL 授权校验
        from cat_mcp.acl import get_access_checker

        access_checker = get_access_checker()
        if not access_checker.check(subject, project_id, resolved_path, "write"):
            raise CatError(
                ErrorCode.FORBIDDEN,
                "Not authorized to write to this file",
            )

        # 10. 可选的 CWD 版本校验
        if cwd_version is not None:
            from cat_mcp.fs.lock import get_lock_manager

            lock_mgr = get_lock_manager()
            lock_mgr.check_cwd_version(workspace, cwd_version)

        # 11. 获取 AtomicWriter 并执行原子写入（prepare 内已获取锁+版本校验）
        from cat_mcp.fs.atomic import CommitStatus, get_atomic_writer

        atomic_writer = get_atomic_writer(project_root)

        # 并发槽位获取
        check_concurrency(subject)

        # 原子写入块
        try:
            ctx = atomic_writer.prepare_write(
                file_path=path,
                content=content_bytes,
                workspace=workspace,
                request_id=request_id,
                subject=subject,
                expected_version=expected_version if mode == "replace" else None,
            )

            # 幂等命中：status 不是 PREPARED 说明是已有操作的重复请求
            # 此时临时文件和锁已在 prepare_write 内部清理，跳过 commit/abort
            if ctx.status == CommitStatus.PREPARED:
                try:
                    atomic_writer.commit_write(ctx)
                except Exception:
                    atomic_writer.abort_write(ctx)
                    raise

            # 12. 审计日志（使用工程相对路径，不泄露绝对路径）
            from cat_mcp.fs.audit import get_audit_logger

            try:
                audit = get_audit_logger()
                audit.log(
                    subject=subject,
                    project_id=project_id,
                    action=f"write.{mode}",
                    outcome="success",
                    path=path,  # 工程相对路径
                    workspace_id=workspace_id,
                    bytes=len(content_bytes),
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for write.%s", mode)

            # 13. 返回结果
            return {
                "version": ctx.file_hash,
                "operation_id": ctx.operation_id,
            }
        finally:
            # 14. 释放并发配额
            get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _resolve_base(path_base: str | None, workspace: dict[str, Any]) -> str:
    """解析 ``path_base`` 参数为工程相对路径。

    Args:
        path_base: ``"cwd"``（默认）、``"root"`` 或 ``None``。
        workspace: Workspace 字典。

    Returns:
        基准路径字符串。
    """
    if path_base is None or path_base == "cwd":
        return workspace.get("cwd", ".")
    if path_base == "root":
        return "."
    raise CatError(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid path_base: {path_base!r}; expected 'cwd' or 'root'",
    )
