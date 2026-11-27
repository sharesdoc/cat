"""单文件历史版本安全恢复工具。

提供 ``cat_restore_file`` MCP 写入工具，从备份版本或回收区恢复文件。
恢复操作是新的写入操作，通过 AtomicWriter 安全提交并审计。

安全约束：
- 检查 restore 权限（写权限）
- 验证 revision_id 归属当前主体
- 检查当前文件版本：已修改或已存在时不覆盖
- 授权备份读取后通过 AtomicWriter 提交
- 恢复是新操作并审计记录
- 过期/损坏/未决拒绝
"""
# 依据: CAT-32

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# .cat_backup 子目录名
_BACKUP_DIR = ".cat_backup"
_TRASH_DIR = ".cat_trash"


def _restore_file(
    state: Any,
    subject: str,
    project_root: str,
    project_id: str,
    path: str,
    *,
    revision_id: str | None = None,
    trash_path: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """从历史版本或回收区恢复文件。

    两种恢复模式互斥：
    1. revision_id：从备份版本（.cat_backup）恢复
    2. trash_path：从回收区（.cat_trash）恢复

    Args:
        state: StateManager 实例。
        subject: 主体标识。
        project_root: 项目根目录。
        path: 目标恢复路径。
        revision_id: revision ID（从 list_changes 获取）。
        trash_path: 回收区中的路径（从 list_trash 获取）。
        request_id: 可选幂等键。

    Returns:
        包含恢复结果的字典。

    Raises:
        CatError: 参数校验或恢复失败时。
    """
    from cat_mcp.fs.paths import SecurePathResolver
    from cat_mcp.fs.sensitive import is_sensitive as _check_sensitive
    from cat_mcp.fs.atomic import CommitStatus, get_atomic_writer
    from cat_mcp.fs.quota import get_quota_manager, check_concurrency
    from cat_mcp.acl import get_access_checker
    from cat_mcp.fs.audit import get_audit_logger

    if not revision_id and not trash_path:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "Either revision_id or trash_path is required",
        )
    if revision_id and trash_path:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "revision_id and trash_path are mutually exclusive",
        )

    # 路径安全检查
    resolver = SecurePathResolver(project_root)
    try:
        resolved = resolver.resolve(path)
    except CatError:
        raise
    except Exception as exc:
        raise CatError(ErrorCode.INVALID_PATH, f"Invalid path: {exc}")

    if _check_sensitive(resolved.resolved_path):
        raise CatError(ErrorCode.UNSAFE_PATH, f"Path {path!r} is sensitive")

    target_path = resolved.resolved_path

    # 检查当前文件状态：已存在且被修改时不覆盖
    current_exists = os.path.isfile(target_path)
    current_hash = None

    if current_exists:
        try:
            with open(target_path, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()
        except OSError as exc:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Cannot read current file: {exc}",
            )

    # --- 模式 1：从 revision 恢复 ---
    source_path: str | None = None
    source_hash: str | None = None
    restore_source: str = ""

    if revision_id:
        conn = state.get_connection()
        rev_row = conn.execute(
            "SELECT * FROM revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if rev_row is None:
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                f"Revision {revision_id!r} not found",
            )

        rev = dict(rev_row)
        op_row = conn.execute(
            "SELECT * FROM operations WHERE id = ?", (rev["operation_id"],)
        ).fetchone()
        if op_row is None:
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                f"Operation for revision {revision_id!r} not found",
            )

        operation = dict(op_row)
        if operation["subject"] != subject:
            raise CatError(
                ErrorCode.FORBIDDEN,
                "Revision does not belong to this subject",
            )

        before_ref = rev.get("before_blob_ref", "")
        if not before_ref:
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                "No backup available for this revision",
            )

        # 安全检查：验证备份路径在 .cat_backup 目录内
        backup_candidate = os.path.join(project_root, _BACKUP_DIR, before_ref)
        real_backup = os.path.realpath(backup_candidate)
        real_cat_backup = os.path.realpath(os.path.join(project_root, _BACKUP_DIR))
        if not real_backup.startswith(real_cat_backup + "/"):
            raise CatError(
                ErrorCode.PATH_OUT_OF_BOUNDS,
                "Backup path is outside .cat_backup directory",
            )

        if not os.path.isfile(real_backup):
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                f"Backup file not found: {before_ref}",
            )

        source_path = real_backup
        restore_source = f"revision:{revision_id}"

    # --- 模式 2：从回收区恢复 ---
    elif trash_path:
        trash_full = os.path.join(project_root, _TRASH_DIR, trash_path)
        # 安全检查：防止遍历 .cat_trash 外部
        real_trash = os.path.realpath(trash_full)
        expected_trash = os.path.realpath(os.path.join(project_root, _TRASH_DIR))
        if not real_trash.startswith(expected_trash + "/"):
            raise CatError(
                ErrorCode.PATH_OUT_OF_BOUNDS,
                "Trash path is outside .cat_trash directory",
            )

        if not os.path.isfile(real_trash):
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                f"Trash file not found: {trash_path}",
            )

        source_path = real_trash
        restore_source = f"trash:{trash_path}"

    if source_path is None:
        raise CatError(ErrorCode.INTERNAL_ERROR, "Restore source not determined")

    # 读取备份文件内容
    try:
        with open(source_path, "rb") as f:
            source_data = f.read()
    except OSError as exc:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            f"Cannot read restore source: {exc}",
        )

    if not source_data:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            "Restore source is empty or corrupted",
        )

    source_hash = hashlib.sha256(source_data).hexdigest()

    # 版本冲突检查：如果文件已存在且内容不同于备份源，不覆盖
    if current_exists:
        if current_hash and current_hash != source_hash:
            raise CatError(
                ErrorCode.VERSION_CONFLICT,
                f"Target file {path!r} has been modified since revision. "
                "Restore refused to avoid overwriting changes. "
                "Use cat_write_file to force overwrite if needed.",
            )

    # ACL 授权校验
    access_checker = get_access_checker()
    if not access_checker.check(subject, project_id, path, "write"):
        raise CatError(
            ErrorCode.FORBIDDEN,
            "Not authorized to restore to this path",
        )

    # 使用 AtomicWriter 写入
    atomic_writer = get_atomic_writer(project_root)

    # 并发槽位获取
    check_concurrency(subject)

    # 幂等键（包含 path 防止不同目标被错误去重）
    idempotent_input = (revision_id or trash_path or "") + "|" + path
    final_request_id = request_id or f"restore_{hashlib.sha256(idempotent_input.encode()).hexdigest()[:16]}"

    try:
        ctx = atomic_writer.prepare_write(
            file_path=path,
            content=source_data,
            subject=subject,
            request_id=final_request_id,
        )

        # 幂等命中：status 不是 PREPARED 说明是已有操作的重复请求
        if ctx.status == CommitStatus.PREPARED:
            try:
                atomic_writer.commit_write(ctx)
            except Exception:
                atomic_writer.abort_write(ctx)
                raise

        # 审计日志
        try:
            audit = get_audit_logger()
            audit.log(
                subject=subject,
                project_id=project_id,
                action="restore",
                outcome="success",
                path=path,
                bytes=len(source_data),
                correlation_id=request_id,
            )
        except Exception:
            logger.warning("Failed to log audit event for restore")

        return {
            "path": path,
            "restore_source": restore_source,
            "size": len(source_data),
            "sha256": source_hash,
            "operation_id": ctx.operation_id,
        }
    finally:
        get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# MCP 工具注册
# ---------------------------------------------------------------------------


def _register_cat_restore_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册文件恢复工具（``cat_restore_file``）。

    Args:
        server: MCPServer 实例。
        workspace_mgr: WorkspaceManager 实例。
        state: StateManager 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    @cat_tool(
        server,
        name="cat_restore_file",
        description=(
            "从历史版本或回收区恢复文件。"
            "支持 revision_id（从 .cat_backup/ 备份恢复）和 "
            "trash_path（从 .cat_trash/ 回收区恢复）两种模式。"
            "两者互斥。如果目标文件已被修改则拒绝覆盖。"
            "恢复操作作为新的写入操作审计记录。"
        ),
        destructive=True,
    )
    async def restore_file_handler(
        workspace_id: str,
        path: str,
        revision_id: str | None = None,
        trash_path: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """恢复文件。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        return _restore_file(
            state=state,
            subject=subject,
            project_root=project_root,
            project_id=project_id,
            path=path,
            revision_id=revision_id,
            trash_path=trash_path,
            request_id=request_id,
        )
