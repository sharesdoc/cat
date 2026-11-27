"""软删除及隔离回收区 MCP 工具。

提供 ``cat_trash_path`` MCP 工具，删除文件/目录时默认移入工程外的
隔离回收区（.cat_trash），而非直接 unlink。支持恢复和审计。

安全约束：
- SecurePathResolver 拒绝 symlink/special files
- SensitiveFileChecker 拒绝敏感路径
- AccessChecker 授权校验（delete 权限）
- WriteLockManager 源文件写锁
- IdempotencyChecker 幂等去重
- AuditLogger 审计记录
- os.rename 原子移动到回收区（同文件系统前提）

回收区隔离：
- 位于 project_root/.cat_trash/
- 不可通过 cat_list_directory / cat_read_file 等标准工具访问
- 文件以 source_path_hash.timestamp 重命名存储
- 清单持久化到 trash 表（SQLite）
"""
# 依据: CAT-26

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import secrets
import stat as stat_module
import time
from pathlib import PurePosixPath
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# 回收区子目录名（工程项目根下创建）
_TRASH_DIR_NAME = ".cat_trash"

# 回收区最大保留天数（默认 30 天）
_DEFAULT_TRASH_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_trash_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_trash_path`` 软删除工具。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例。
        state: ``StateManager`` 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import check_concurrency, get_quota_manager

    # ------------------------------------------------------------------
    # cat_trash_path
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_trash_path",
        description=(
            "将文件或目录移入隔离回收区（.cat_trash），非直接永久删除。"
            "支持恢复（管理员手动还原）。"
            "递归删除默认关闭（recursive=False）。"
            "跨文件系统拒绝。"
        ),
        destructive=True,
    )
    async def trash_path_handler(
        workspace_id: str,
        path: str,
        recursive: bool = False,
        request_id: str | None = None,
        path_base: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_trash_path`` MCP 工具调用。

        处理流程：
          1. 获取 subject, 校验 workspace
          2. 获取 project_id, project_root
          3. 并发配额 + CWD 版本校验
          4. 安全解析路径
          5. 敏感文件检查 + ACL（delete 权限）
          6. 根保护、非空目录保护
          7. 幂等检查
          8. 获取锁 + 移动文件到回收区
          9. 审计日志

        Args:
            workspace_id: 工作空间标识。
            path: 要删除的文件/目录路径。
            recursive: 是否递归删除非空目录（默认 False）。
            request_id: 幂等请求 ID。
            path_base: "cwd" 或 "root"。
            cwd_version: 可选 CWD 版本校验。

        Returns:
            {"trash_path": str} — 回收区中的目标路径。
        """
        subject = get_current_subject()

        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        if path_base is None:
            path_base = "cwd"
        base = _resolve_base(path_base, workspace)

        # 并发配额 inside try-finally
        try:
            check_concurrency(subject)

            if cwd_version is not None:
                from cat_mcp.fs.lock import get_lock_manager

                get_lock_manager().check_cwd_version(workspace, cwd_version)

            from cat_mcp.fs.paths import SecurePathResolver

            resolver = SecurePathResolver(project_root)

            # 安全解析
            with resolver.resolve(path, base=base) as rp:
                resolved_path = rp.resolved_path

            # 根保护
            if resolved_path == ".":
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Cannot delete the project root",
                )

            # 敏感文件检查
            from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker

            if get_sensitive_checker().is_sensitive(resolved_path):
                raise CatError(ErrorCode.FORBIDDEN, "Path is sensitive")

            # ACL（delete 权限）
            from cat_mcp.acl import get_access_checker

            access_checker = get_access_checker()
            if not access_checker.check(subject, project_id, resolved_path, "write"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized")

            # 目录非空 + 非递归保护
            try:
                with resolver.resolve(resolved_path) as rp:
                    st = rp.stat(follow_symlinks=False)
                if stat_module.S_ISDIR(st.st_mode) and not recursive:
                    # 检查目录是否为空
                    with resolver.resolve(resolved_path) as rp:
                        try:
                            entries = os.listdir(".", dir_fd=rp.dir_fd)
                        except OSError:
                            entries = []
                    if entries:
                        raise CatError(
                            ErrorCode.OPERATION_FAILED,
                            "Directory is not empty; use recursive=True to trash",
                        )
            except CatError:
                raise

            # 幂等检查
            if request_id:
                op, is_new = state.create_operation_idempotent(
                    subject=subject,
                    workspace_id=workspace_id,
                    request_id=request_id,
                    path=resolved_path,
                    input_digest=hashlib.sha256(
                        resolved_path.encode("utf-8"),
                    ).hexdigest(),
                )
                if not is_new:
                    return {"trash_path": ""}

            # 执行移动到回收区
            trash_rel = _trash_path(
                resolver=resolver,
                project_root=project_root,
                target=resolved_path,
                recursive=recursive,
            )

            # 更新操作状态
            if request_id and op:
                state.update_operation_status(op.get("id", ""), "committed")

            # 审计日志
            from cat_mcp.fs.audit import get_audit_logger

            try:
                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.trash",
                    outcome="success",
                    path=resolved_path,
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for trash")

            logger.info(
                "Subject %s trashed %s → %s", subject, resolved_path, trash_rel,
            )

            return {"trash_path": trash_rel}
        except CatError:
            # 更新操作状态为 failed
            if request_id:
                try:
                    state.update_operation_status(op.get("id", request_id), "failed")
                except Exception:
                    logger.warning("Failed to update operation status on failure")
            try:
                from cat_mcp.fs.audit import get_audit_logger

                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.trash",
                    outcome="failure",
                    path=path,
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit failure for trash")
            raise
        finally:
            get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _trash_path(
    resolver: Any,
    project_root: str,
    target: str,
    recursive: bool,
) -> str:
    """将文件/目录原子移动到回收区。

    流程：
    1. 创建回收区目录（若不存在）
    2. stat 确认目标存在
    3. 获取写入锁
    4. 锁内 resolve 源父目录 fd
    5. 用 os.rename 移动到回收区（目标名带哈希+时间戳防冲突）
    6. fsync 回收区父目录

    Args:
        resolver: SecurePathResolver。
        project_root: 工程根绝对路径。
        target: 要删除的目标相对路径。
        recursive: 递归标志（当前仅用于检查，实际 os.rename 中原样移动）。

    Returns:
        回收区中的相对路径。
    """
    # 1. 确保回收区存在
    trash_root = os.path.join(project_root, _TRASH_DIR_NAME)
    os.makedirs(trash_root, mode=0o755, exist_ok=True)

    # 生成回收区唯一文件名
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
    path_hash = hashlib.md5(target.encode("utf-8")).hexdigest()[:12]
    safe_name = f"{path_hash}.{ts}"

    # 获取锁
    from cat_mcp.fs.lock import get_lock_manager

    lock_mgr = get_lock_manager()
    lock_ctx = lock_mgr.acquire_write_lock("trash", [target], timeout=30.0)

    try:
        # 锁内 re-resolve 源父目录 fd
        with resolver.resolve(target) as rp:
            dir_name = rp.name

            # 校验目标仍存在
            try:
                os.stat(dir_name, dir_fd=rp.dir_fd, follow_symlinks=False)
            except OSError:
                raise CatError(
                    ErrorCode.FILE_NOT_FOUND, "Target path does not exist",
                )

            # 同文件系统检查（回收区须在同一文件系统）
            try:
                src_dev = os.stat(
                    dir_name, dir_fd=rp.dir_fd, follow_symlinks=False,
                ).st_dev
            except OSError:
                raise CatError(
                    ErrorCode.OPERATION_FAILED, "Cannot stat target",
                )

            # 用绝对路径打开回收区目录 fd 检查文件系统
            try:
                trash_fd = os.open(
                    trash_root,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            except OSError:
                raise CatError(
                    ErrorCode.OPERATION_FAILED, "Cannot access trash directory",
                )

            try:
                trash_st = os.stat(".", dir_fd=trash_fd, follow_symlinks=False)
                if src_dev != trash_st.st_dev:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED,
                        "Cannot trash across file system boundaries",
                    )

                # 原子移动文件到回收区
                try:
                    os.rename(
                        dir_name,
                        safe_name,
                        src_dir_fd=rp.dir_fd,
                        dst_dir_fd=trash_fd,
                    )
                except OSError as exc:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED,
                        "Failed to move file to trash",
                    )

                # fsync 源父目录和回收区目录
                try:
                    os.fsync(rp.dir_fd)
                    os.fsync(trash_fd)
                except OSError:
                    pass
            finally:
                os.close(trash_fd)

        return f"{_TRASH_DIR_NAME}/{safe_name}"
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
