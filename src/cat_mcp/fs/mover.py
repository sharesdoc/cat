"""文件与目录安全移动重命名 MCP 工具。

提供 ``cat_move_path`` MCP 工具，支持同工程移动或改名，
默认不覆盖目标，跨设备拒绝。

安全约束：
- SecurePathResolver 拒绝 symlink/special files（源和目的各解析一次）
- SensitiveFileChecker 拒绝敏感路径
- AccessChecker 源+目的双授权校验
- WriteLockManager 双锁（源+目的，排序避免死锁）
- IdempotencyChecker 幂等去重
- AuditLogger 审计记录
- os.rename(dir_fd=...) 原子 rename，无替换语义
"""
# 依据: CAT-24

from __future__ import annotations

import errno
import hashlib
import logging
import os
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_move_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_move_path`` 移动/重命名工具。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例。
        state: ``StateManager`` 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import check_concurrency, get_quota_manager

    # ------------------------------------------------------------------
    # cat_move_path
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_move_path",
        description=(
            "将文件或目录从源路径移动到目标路径（同工程内重命名或移动）。"
            "默认不覆盖已有目标（no-replace）。"
            "支持文件和目录，但跨文件系统时拒绝操作。"
            "返回操作 ID。"
        ),
        destructive=True,
    )
    async def move_path_handler(
        workspace_id: str,
        source: str,
        destination: str,
        request_id: str | None = None,
        path_base: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_move_path`` MCP 工具调用。

        处理流程：
          1. 获取 subject, 校验 workspace
          2. 获取 project_id, project_root
          3. 并发配额检查 + CWD 版本校验
          4. 安全解析 source 和 destination
          5. 敏感文件检查 + ACL 双授权
          6. 源非根校验、同文件系统校验、非自身子树校验
          7. 双锁获取 + 源版本校验 + 目标不存在校验
          8. os.rename 原子移动
          9. 审计日志

        Args:
            workspace_id: 工作空间标识。
            source: 源路径（相对工程根）。
            destination: 目标路径（相对工程根）。
            request_id: 幂等请求 ID。
            path_base: "cwd" 或 "root"。
            cwd_version: 可选 CWD 版本校验。

        Returns:
            {"operation_id": str}
        """
        subject = get_current_subject()

        # workspace
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        # project root
        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        # path_base
        if path_base is None:
            path_base = "cwd"
        base = _resolve_base(path_base, workspace)

        # 并发配额（放在 try 块内，确保 finally 总能释放）
        try:
            check_concurrency(subject)

            # CWD 版本校验
            if cwd_version is not None:
                from cat_mcp.fs.lock import get_lock_manager

                get_lock_manager().check_cwd_version(workspace, cwd_version)

            from cat_mcp.fs.paths import SecurePathResolver

            resolver = SecurePathResolver(project_root)

            # 安全解析 source（仅获取 resolved_path，不持有 fd）
            with resolver.resolve(source, base=base) as rp:
                source_resolved = rp.resolved_path

            # 源不能是工程根
            if source_resolved == ".":
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Cannot move the project root directory",
                )

            # 安全解析 destination（仅获取 resolved_path）
            with resolver.resolve(destination, base=base) as rp:
                dest_resolved = rp.resolved_path

            # 敏感文件检查（源和目的都检查）
            from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker

            sensitive_checker = get_sensitive_checker()
            if sensitive_checker.is_sensitive(source_resolved):
                raise CatError(ErrorCode.FORBIDDEN, "Source path is sensitive")
            if sensitive_checker.is_sensitive(dest_resolved):
                raise CatError(ErrorCode.FORBIDDEN, "Destination path is sensitive")

            # ACL 双授权
            from cat_mcp.acl import get_access_checker

            access_checker = get_access_checker()
            if not access_checker.check(subject, project_id, source_resolved, "write"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized for source")
            if not access_checker.check(subject, project_id, dest_resolved, "write"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized for destination")

            # 源不能是自身的子树
            if source_resolved != "." and dest_resolved.startswith(
                source_resolved + "/"
            ):
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Cannot move a directory into itself",
                )

            # 幂等检查 + 操作记录创建
            op_id: str = request_id or ""
            if request_id:
                op, is_new = state.create_operation_idempotent(
                    subject=subject,
                    workspace_id=workspace_id,
                    request_id=request_id,
                    path=f"{source}→{destination}",
                    input_digest=hashlib.sha256(
                        f"{source}|{destination}".encode("utf-8"),
                    ).hexdigest(),
                )
                if not is_new:
                    return {"operation_id": op.get("id", "")}
                op_id = op.get("id", request_id)

            # 执行移动（锁内 resolve + rename）
            try:
                _move_path(resolver, source_resolved, dest_resolved)
            except Exception:
                # 移动失败 → 更新操作状态为 failed
                if request_id and op_id:
                    state.update_operation_status(op_id, "failed")
                raise

            # 移动成功 → 更新操作状态为 committed
            if request_id and op_id:
                state.update_operation_status(op_id, "committed")

            # 审计日志
            from cat_mcp.fs.audit import get_audit_logger

            try:
                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.move",
                    outcome="success",
                    path=f"{source}→{destination}",
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for move")

            logger.info(
                "Subject %s moved %s → %s", subject, source, destination,
            )

            return {"operation_id": request_id or ""}
        except CatError:
            try:
                from cat_mcp.fs.audit import get_audit_logger

                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.move",
                    outcome="failure",
                    path=f"{source}→{destination}",
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit failure for move")
            raise
        finally:
            get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _move_path(
    resolver: Any,
    source_path: str,
    dest_path: str,
) -> None:
    """执行安全的文件/目录移动。

    在锁内 resolve 源和目的的父目录 fd，检查同文件系统，
    校验目标不存在后执行 os.rename。

    Args:
        resolver: SecurePathResolver 实例。
        source_path: 源相对路径。
        dest_path: 目的相对路径。

    Raises:
        CatError: 移动失败时。
    """
    # 获取双锁（按路径排序避免死锁）
    from cat_mcp.fs.lock import get_lock_manager

    lock_mgr = get_lock_manager()
    lock_paths = sorted([source_path, dest_path])
    lock_ctx = lock_mgr.acquire_write_lock(
        "mover", lock_paths, timeout=30.0,
    )

    try:
        # 锁内 resolve 源和目的的父目录 fd
        with resolver.resolve(source_path) as src_rp:
            source_name = src_rp.name

            # 检查同文件系统
            try:
                src_st = os.stat(
                    source_name, dir_fd=src_rp.dir_fd, follow_symlinks=False,
                )
            except OSError:
                raise CatError(
                    ErrorCode.FILE_NOT_FOUND, "Source path does not exist",
                )

            with resolver.resolve(dest_path) as dst_rp:
                dest_name = dst_rp.name

                # 同文件系统检查
                try:
                    dst_dev_st = os.stat(
                        ".", dir_fd=dst_rp.dir_fd, follow_symlinks=False,
                    )
                except OSError:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED, "Cannot stat destination",
                    )
                if src_st.st_dev != dst_dev_st.st_dev:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED,
                        "Cannot move across file system boundaries",
                    )

                # 目标不存在检查（no-replace）
                try:
                    os.stat(dest_name, dir_fd=dst_rp.dir_fd, follow_symlinks=False)
                    raise CatError(
                        ErrorCode.FILE_ALREADY_EXISTS,
                        "Destination already exists",
                    )
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise CatError(
                            ErrorCode.OPERATION_FAILED, str(exc),
                        )
                    # ENOENT = 不存在，合法

                # 源存在检查（锁内二次确认）
                try:
                    os.stat(source_name, dir_fd=src_rp.dir_fd, follow_symlinks=False)
                except OSError:
                    raise CatError(
                        ErrorCode.FILE_NOT_FOUND, "Source not found",
                    )

                # 原子移动
                try:
                    os.rename(
                        source_name,
                        dest_name,
                        src_dir_fd=src_rp.dir_fd,
                        dst_dir_fd=dst_rp.dir_fd,
                    )
                except OSError as exc:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED,
                        "Failed to move source to destination",
                    )

                # fsync 源和目标父目录
                try:
                    os.fsync(src_rp.dir_fd)
                    if src_rp.dir_fd != dst_rp.dir_fd:
                        os.fsync(dst_rp.dir_fd)
                except OSError:
                    pass

    finally:
        lock_mgr.release_write_lock(lock_ctx.lock_id)


def _resolve_base(path_base: str | None, workspace: dict[str, Any]) -> str:
    """解析 path_base 参数为工程相对路径。"""
    if path_base is None or path_base == "cwd":
        return workspace.get("cwd", ".")
    if path_base == "root":
        return "."
    raise CatError(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid path_base: {path_base!r}; expected 'cwd' or 'root'",
    )


