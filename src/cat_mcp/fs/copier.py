"""单文件安全复制 MCP 工具。

提供 ``cat_copy_file`` MCP 工具，复制单个文件到同工程安全位置。
源文件不修改，目标文件通过 AtomicWriter 原子创建（no-replace）。

安全约束：
- SecurePathResolver 拒绝 symlink/special files（源和目的各解析一次）
- SensitiveFileChecker 拒绝敏感路径（源和目的都检查）
- AccessChecker 源 read + 目的 write 双授权
- WriteLockManager 双锁
- AtomicWriter 原子写入
- AuditLogger 审计记录
"""
# 依据: CAT-25

from __future__ import annotations

import logging
import os
import stat as stat_module
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# 复制缓冲区大小（64 KiB）
_COPY_CHUNK_SIZE = 65536


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_copy_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_copy_file`` 复制工具。"""
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import check_concurrency, get_quota_manager

    @cat_tool(
        server,
        name="cat_copy_file",
        description=(
            "复制单个文件到同工程的另一位置。源文件不变。"
            "目标位置不得已存在（no-replace）。"
            "仅复制普通文件，不递归复制目录。"
            "返回目标文件的版本哈希。"
        ),
        destructive=True,
    )
    async def copy_file_handler(
        workspace_id: str,
        source: str,
        destination: str,
        request_id: str | None = None,
        path_base: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_copy_file`` MCP 工具调用。"""
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

        try:
            check_concurrency(subject)

            if cwd_version is not None:
                from cat_mcp.fs.lock import get_lock_manager
                get_lock_manager().check_cwd_version(workspace, cwd_version)

            from cat_mcp.fs.paths import SecurePathResolver

            resolver = SecurePathResolver(project_root)

            from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker
            from cat_mcp.acl import get_access_checker

            # 解析 source + stat + 敏感 + ACL + 读内容 紧挨构成"resolve-read"最小窗口
            with resolver.resolve(source, base=base) as rp:
                source_resolved = rp.resolved_path
                st = rp.stat(follow_symlinks=False)

            # 只能复制普通文件
            if not stat_module.S_ISREG(st.st_mode):
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    "Can only copy regular files",
                )

            # 源文件大小检查（防止 OOM）
            _MAX_COPY_BYTES = 100 * 1024 * 1024  # 100 MiB
            if st.st_size > _MAX_COPY_BYTES:
                raise CatError(
                    ErrorCode.FILE_TOO_LARGE,
                    f"Source file too large ({st.st_size} > {_MAX_COPY_BYTES} bytes)",
                )

            checker = get_sensitive_checker()
            if checker.is_sensitive(source_resolved):
                raise CatError(ErrorCode.FORBIDDEN, "Source path is sensitive")

            access_checker = get_access_checker()
            if not access_checker.check(subject, project_id, source_resolved, "read"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized to read source")

            # 立即读取源文件（紧接 resolve，缩小 TOCTOU 窗口）
            content = _read_source(resolver, source_resolved)

            # 解析 destination（独立 resolve）
            with resolver.resolve(destination, base=base) as rp:
                dest_resolved = rp.resolved_path

            # 目的敏感 + ACL + 自身检查
            if checker.is_sensitive(dest_resolved):
                raise CatError(ErrorCode.FORBIDDEN, "Destination path is sensitive")
            if not access_checker.check(subject, project_id, dest_resolved, "write"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized to write destination")
            if source_resolved == dest_resolved:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Source and destination are the same",
                )

            # 目的已存在检查（no-replace）
            try:
                with resolver.resolve(destination, base=base) as rp:
                    rp.stat(follow_symlinks=False)
                raise CatError(
                    ErrorCode.FILE_ALREADY_EXISTS,
                    "Destination already exists",
                )
            except CatError as exc:
                if exc.error_code not in (ErrorCode.FILE_ALREADY_EXISTS,):
                    # FILE_NOT_FOUND 是预期的
                    if exc.error_code != ErrorCode.FILE_NOT_FOUND:
                        raise

            # 通过 AtomicWriter 写入目标
            from cat_mcp.fs.atomic import CommitStatus, get_atomic_writer

            atomic_writer = get_atomic_writer(project_root)

            ctx = atomic_writer.prepare_write(
                file_path=dest_resolved,
                content=content,
                workspace=workspace,
                request_id=request_id,
                subject=subject,
            )

            if ctx.status == CommitStatus.PREPARED:
                try:
                    atomic_writer.commit_write(ctx)
                except Exception:
                    atomic_writer.abort_write(ctx)
                    raise

            # 审计日志
            from cat_mcp.fs.audit import get_audit_logger

            try:
                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.copy",
                    outcome="success",
                    path=f"{source}→{destination}",
                    workspace_id=workspace_id,
                    bytes=len(content),
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for copy")

            logger.info(
                "Subject %s copied %s → %s (%d bytes)",
                subject, source, destination, len(content),
            )

            return {"version": ctx.file_hash}

        except Exception:
            if request_id:
                try:
                    state.update_operation_status(request_id, "failed")
                except Exception:
                    logger.warning("Failed to update operation status")
            try:
                from cat_mcp.fs.audit import get_audit_logger
                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.copy",
                    outcome="failure",
                    path=f"{source}→{destination}",
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit failure for copy")
            raise
        finally:
            get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _read_source(resolver: Any, path: str) -> bytes:
    """读取源文件全部内容为 bytes。

    Raises:
        CatError: 文件读取失败时。
    """
    buf = bytearray()

    with resolver.resolve(path) as rp:
        st = rp.stat(follow_symlinks=False)
        size = st.st_size

        fd = rp.open(os.O_RDONLY)
        try:
            while len(buf) < size:
                chunk = os.read(fd, min(_COPY_CHUNK_SIZE, size - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
        finally:
            os.close(fd)

    return bytes(buf)


def _resolve_base(path_base: str | None, workspace: dict[str, Any]) -> str:
    """解析 path_base 参数。"""
    if path_base is None or path_base == "cwd":
        return workspace.get("cwd", ".")
    if path_base == "root":
        return "."
    raise CatError(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid path_base: {path_base!r}; expected 'cwd' or 'root'",
    )
