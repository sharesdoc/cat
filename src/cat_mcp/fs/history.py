"""变更历史查询与文件差异对比工具。

提供 ``cat_list_changes`` 和 ``cat_diff_file`` 两个 MCP 只读工具，
从持久化 Operation/Revision/Audit 表中查询修改历史并对比文件差异。

安全约束：
- 查询绑定授权范围（subject+workspace）
- 差异内容经敏感文件过滤
- 输出有界（分页 + 大小限制）
- 仅查询不修改任何状态
"""
# 依据: CAT-31

from __future__ import annotations

import difflib
import logging
import os
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# 默认分页大小
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500

# 差异输出最大行数
_MAX_DIFF_LINES = 5000

# 差异上下文行数默认值
_DEFAULT_CONTEXT_LINES = 3


def _list_changes(
    state: Any,
    subject: str,
    path: str | None = None,
    project_id: str | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """查询修改历史记录。

    从 operations 和 audit_log 表中查询指定路径或项目的修改历史。

    Args:
        state: StateManager 实例。
        subject: 主体标识（仅查询该主体的记录）。
        path: 可选路径过滤。
        project_id: 可选项目 ID 过滤。
        limit: 最大返回记录数。
        offset: 分页偏移量。

    Returns:
        包含历史记录列表的字典。

    Raises:
        CatError: 参数无效时。
    """
    if limit < 1 or limit > _MAX_PAGE_SIZE:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"limit must be between 1 and {_MAX_PAGE_SIZE}",
        )
    if offset < 0:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "offset must be >= 0",
        )

    # 从 operations 表查询
    conn = state.get_connection()
    conditions: list[str] = ["subject = ?"]
    params: list[Any] = [subject]

    if path is not None:
        conditions.append("path = ?")
        params.append(path)
    if project_id is not None:
        conditions.append("workspace_id IN (SELECT id FROM workspaces WHERE project_id = ?)")
        params.append(project_id)

    where = " AND ".join(conditions)

    # 获取总数
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM operations WHERE {where}",
        params,
    ).fetchone()
    total = count_row[0] if count_row else 0

    # 获取分页数据
    rows = conn.execute(
        f"SELECT * FROM operations WHERE {where} "
        "ORDER BY started_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        # 获取对应 revision
        rev = conn.execute(
            "SELECT * FROM revisions WHERE operation_id = ? ORDER BY id DESC LIMIT 1",
            (entry["id"],),
        ).fetchone()
        if rev:
            entry["revision"] = dict(rev)
        entries.append(entry)

    return {
        "entries": entries,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }


def _diff_file(
    state: Any,
    subject: str,
    project_root: str,
    path: str,
    *,
    revision_id: str | None = None,
    git_base: str | None = None,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
) -> dict[str, Any]:
    """对比单文件的历史版本或 Git 基准差异。

    支持两种对比方式：
    1. revision_id：对比指定 revision 与当前文件
    2. git_base：使用 Git diff 对比文件历史版本

    Args:
        state: StateManager 实例。
        subject: 主体标识。
        project_root: 项目根目录。
        path: 目标文件路径。
        revision_id: revision ID（从 list_changes 获取）。
        git_base: Git 基准（如 HEAD~1，传 None 使用 Git diff）。
        context_lines: 差异上下文行数。

    Returns:
        包含差异内容的字典。

    Raises:
        CatError: 参数无效或版本不存在时。
    """
    from cat_mcp.fs.paths import SecurePathResolver
    from cat_mcp.fs.sensitive import is_sensitive as _check_sensitive

    if revision_id and git_base:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "revision_id and git_base are mutually exclusive",
        )
    if not revision_id and not git_base:
        # 默认使用 git_base="HEAD"（当前文件 vs HEAD）
        git_base = "HEAD"

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

    current_path = resolved.resolved_path
    if not os.path.isfile(current_path):
        raise CatError(ErrorCode.FILE_NOT_FOUND, f"File {path!r} not found")

    # 读取当前文件内容
    try:
        with open(current_path, "r", encoding="utf-8") as f:
            current_lines = f.readlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            f"Cannot read current file: {exc}",
        )

    # --- revision_id 模式：对比历史版本 ---
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

        # 获取历史内容（从 before_blob_ref 读取历史备份）
        # 备份文件路径：project_root/.cat_backup/<revision_id>
        before_ref = rev.get("before_blob_ref", "")
        if not before_ref:
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                "No backup available for this revision",
            )

        backup_path = os.path.join(project_root, ".cat_backup", before_ref)
        if not os.path.isfile(backup_path):
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                f"Backup file not found: {before_ref}",
            )

        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                old_lines = f.readlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Cannot read backup file: {exc}",
            )

        # 使用 difflib 生成差异
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                current_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
                n=context_lines,
            )
        )

        return {
            "path": path,
            "diff_type": "revision",
            "revision_id": revision_id,
            "operation_id": operation["id"],
            "operation_status": operation.get("status", ""),
            "operation_time": operation.get("started_at", ""),
            "old_mode": "historical",
            "new_mode": "current",
            "lines_added": sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++")),
            "lines_deleted": sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---")),
            "diff": _truncate_diff(diff_lines),
        }

    # --- git_base 模式：使用 Git diff ---
    if git_base is not None:
        try:
            from cat_mcp.fs.git_tools import _run_git, _check_is_git_repo
        except ImportError:
            raise CatError(
                ErrorCode.NOT_IMPLEMENTED,
                "Git diff unavailable — git_tools module not loaded",
            )

        resolved_cwd = os.path.realpath(project_root)
        _check_is_git_repo(resolved_cwd)

        # 构造 git diff 命令
        args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "-U", str(context_lines),
            git_base,
            "--", path,
        ]

        result = _run_git(args, cwd=resolved_cwd)
        diff_text = result.stdout

        # 未显示 diff 可能因为无差异或数据在暂存区
        if not diff_text:
            return {
                "path": path,
                "diff_type": "git",
                "git_base": git_base,
                "lines_added": 0,
                "lines_deleted": 0,
                "diff": [],
                "note": "No differences found (file may be identical or untracked)",
            }

        diff_lines = diff_text.split("\n")

        return {
            "path": path,
            "diff_type": "git",
            "git_base": git_base,
            "lines_added": sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++")),
            "lines_deleted": sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---")),
            "diff": _truncate_diff(diff_lines),
        }

    return {"path": path, "diff": []}


def _truncate_diff(diff_lines: list[str]) -> list[str]:
    """截断超过最大行数的差异输出。

    Args:
        diff_lines: 差异行列表。

    Returns:
        截断后的差异行列表。
    """
    if len(diff_lines) <= _MAX_DIFF_LINES:
        return diff_lines

    truncated = diff_lines[:_MAX_DIFF_LINES]
    truncated.append(
        f"... (truncated, {len(diff_lines) - _MAX_DIFF_LINES} more lines)"
    )
    return truncated


# ---------------------------------------------------------------------------
# MCP 工具注册
# ---------------------------------------------------------------------------


def _register_cat_history_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册历史查询工具（``cat_list_changes`` 和 ``cat_diff_file``）。

    Args:
        server: MCPServer 实例。
        workspace_mgr: WorkspaceManager 实例。
        state: StateManager 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    # ------------------------------------------------------------------
    # cat_list_changes
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_list_changes",
        description=(
            "查询指定路径或项目的修改历史记录。"
            "返回操作记录、版本号和时间信息。"
            "支持分页和路径过滤。只读，不修改任何状态。"
        ),
        read_only=True,
    )
    async def list_changes_handler(
        workspace_id: str,
        path: str | None = None,
        limit: int = _DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        """查询修改历史。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        return _list_changes(
            state=state,
            subject=subject,
            path=path,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------------
    # cat_diff_file
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_diff_file",
        description=(
            "对比单文件的历史版本或 Git 基准差异。"
            "支持 revision_id（对比历史备份）和 git_base（git diff）两种模式。"
            "两者互斥，不指定时默认 git_base=\"HEAD\"。"
            "输出 unified diff 格式。只读，不修改任何状态。"
        ),
        read_only=True,
    )
    async def diff_file_handler(
        workspace_id: str,
        path: str,
        revision_id: str | None = None,
        git_base: str | None = None,
        context_lines: int = _DEFAULT_CONTEXT_LINES,
    ) -> dict[str, Any]:
        """对比文件差异。"""
        if context_lines < 0 or context_lines > 100:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "context_lines must be between 0 and 100",
            )

        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        return _diff_file(
            state=state,
            subject=subject,
            project_root=project_root,
            path=path,
            revision_id=revision_id,
            git_base=git_base,
            context_lines=context_lines,
        )
