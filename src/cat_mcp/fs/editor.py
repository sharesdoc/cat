"""单文件精确局部编辑 MCP 工具。

提供 ``cat_edit_file`` MCP 工具，支持在单文件内进行基于
原文本锚点的精确替换、行范围替换、行前/后插入等编辑操作。

核心约束：
- 所有 edits 基于同一原版文件内容
- 按逆序（文件尾→文件头）应用，避免偏移累积
- 拒绝歧义匹配（匹配数 != expected）
- 拒绝重叠 edits（同一区间不得被两条 edit 覆盖）
- dry_run 预览 diff 不实际写入
- 写入路径：通过 AtomicWriter 原子提交
"""
# 依据: CAT-21

from __future__ import annotations

import difflib
import logging
import os
from dataclasses import dataclass
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 编辑操作模型
# ---------------------------------------------------------------------------


@dataclass
class EditOp:
    """单条编辑操作。

    Attributes:
        old_text: 要替换的旧文本（replace_exact 模式必填）。
        new_text: 新文本（替换/插入时使用）。
        start_line: 行范围起始（1-indexed）。
        end_line: 行范围结束（含，1-indexed）。
        mode: 编辑模式：replace_exact / replace_lines / insert_before / insert_after。
    """
    old_text: str = ""
    new_text: str = ""
    start_line: int = 0
    end_line: int = 0
    mode: str = "replace_exact"


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_edit_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_edit_file`` 编辑工具。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例。
        state: ``StateManager`` 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import check_concurrency, get_quota_manager

    @cat_tool(
        server,
        name="cat_edit_file",
        description=(
            "对单文件进行精确局部编辑，支持按精确文本替换、"
            "按行范围替换、在指定行前/后插入。"
            "同一次调用可含多条编辑，全部成功或全部回滚。"
            "dry_run=True 仅预览 diff，不写入。"
            "返回 diff 内容和修改行数。"
        ),
        destructive=True,
    )
    async def edit_file_handler(
        workspace_id: str,
        path: str,
        edits: list[dict[str, Any]],
        dry_run: bool = False,
        expected_version: str | None = None,
        request_id: str | None = None,
        path_base: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_edit_file`` MCP 工具调用。

        Args:
            workspace_id: 工作空间标识。
            path: 目标文件路径。
            edits: 编辑操作列表。每项含：
                - mode: "replace_exact"|"replace_lines"|"insert_before"|"insert_after"
                - old_text: 替换模式的原文本
                - new_text: 新文本
                - start_line: 行范围起始（1-indexed）
                - end_line: 行范围结束（含）
            dry_run: 仅预览不写入。
            expected_version: 预期文件版本哈希。
            request_id: 幂等请求 ID。
            path_base: "cwd" 或 "root"。
            cwd_version: 可选 CWD 版本校验。

        Returns:
            {"diff": str, "changed_lines": int} 或 dry_run 预览。
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

        if path_base is None:
            path_base = "cwd"
        base = _resolve_base(path_base, workspace)

        # 初始化变量（防止 except 块引用未绑定变量）
        op = None

        # 并发配额 inside try-finally
        try:
            check_concurrency(subject)

            if cwd_version is not None:
                from cat_mcp.fs.lock import get_lock_manager

                get_lock_manager().check_cwd_version(workspace, cwd_version)

            # 安全解析路径
            from cat_mcp.fs.paths import SecurePathResolver

            resolver = SecurePathResolver(project_root)

            with resolver.resolve(path, base=base) as rp:
                resolved_path = rp.resolved_path

            # 敏感文件检查
            from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker

            if get_sensitive_checker().is_sensitive(resolved_path):
                raise CatError(ErrorCode.FORBIDDEN, "Path is sensitive")

            # ACL
            from cat_mcp.acl import get_access_checker

            access_checker = get_access_checker()
            if not access_checker.check(subject, project_id, resolved_path, "write"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized")

            # 幂等检查由 AtomicWriter.prepare_write 的 create_operation_idempotent
            # 原子处理，不自建 op 记录避免双重幂等 + input_digest 冲突

            # edits 大小检查
            _MAX_EDITS = 100
            _MAX_EDIT_CONTENT_BYTES = 1024 * 1024  # 1 MiB
            if len(edits) > _MAX_EDITS:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Too many edits ({len(edits)} > {_MAX_EDITS})",
                )
            total_bytes = sum(
                len(e.get("old_text", "").encode("utf-8"))
                + len(e.get("new_text", "").encode("utf-8"))
                for e in edits
            )
            if total_bytes > _MAX_EDIT_CONTENT_BYTES:
                raise CatError(
                    ErrorCode.RESPONSE_TOO_LARGE,
                    "Edit content exceeds size limit",
                )

            # 读取文件原内容（捕获二进制文件解码错误）
            try:
                file_content = _read_file(resolver, resolved_path)
            except UnicodeDecodeError:
                raise CatError(
                    ErrorCode.UNSUPPORTED_ENCODING,
                    "File is not valid UTF-8 text",
                )

            # 解析 edits
            edit_ops = _parse_edits(edits)

            # 计算 diff 和应用结果
            diff_text, new_content, changed_lines = _apply_edits(
                file_content, edit_ops,
            )

            if dry_run:
                return {
                    "diff": diff_text,
                    "changed_lines": changed_lines,
                    "dry_run": True,
                }

            # 实际写入（通过 AtomicWriter + 版本校验 + 幂等）
            from cat_mcp.fs.atomic import CommitStatus, get_atomic_writer

            atomic_writer = get_atomic_writer(project_root)
            content_bytes = new_content.encode("utf-8")

            ctx = atomic_writer.prepare_write(
                file_path=resolved_path,
                content=content_bytes,
                workspace=workspace,
                request_id=request_id,
                subject=subject,
                expected_version=expected_version,
            )

            if ctx.status == CommitStatus.PREPARED:
                try:
                    atomic_writer.commit_write(ctx)
                except Exception:
                    atomic_writer.abort_write(ctx)
                    raise

            # 操作状态由 AtomicWriter 的 create_operation_idempotent 管理

            # 审计日志
            from cat_mcp.fs.audit import get_audit_logger

            try:
                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.edit",
                    outcome="success",
                    path=resolved_path,
                    workspace_id=workspace_id,
                    bytes=len(content_bytes),
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for edit")

            logger.info(
                "Subject %s edited %s (%d lines changed)",
                subject, resolved_path, changed_lines,
            )

            return {
                "diff": diff_text,
                "changed_lines": changed_lines,
                "version": ctx.file_hash,
            }
        except Exception:
            # 捕获所有异常（CatError 和非 CatError）以记录审计和操作状态
            if request_id:
                try:
                    state.update_operation_status(
                        _get_op_id(request_id, op), "failed",
                    )
                except Exception:
                    logger.warning("Failed to update operation status")
            try:
                from cat_mcp.fs.audit import get_audit_logger

                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.edit",
                    outcome="failure",
                    path=path,
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit failure for edit")
            raise


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _read_file(resolver: Any, file_path: str) -> str:
    """读取文件全部内容为文本。

    Raises:
        CatError: 文件不存在或无法读取。
        UnicodeDecodeError: 文件不是有效 UTF-8。
    """
    buf = bytearray()
    chunk_size = 65536

    with resolver.resolve(file_path) as rp:
        st = rp.stat(follow_symlinks=False)
        size = st.st_size

        fd = rp.open(os.O_RDONLY)
        try:
            while len(buf) < size:
                chunk = os.read(fd, min(chunk_size, size - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
        finally:
            os.close(fd)

    return buf.decode("utf-8")


def _parse_edits(raw_edits: list[dict[str, Any]]) -> list[EditOp]:
    """解析并校验编辑操作列表。

    Raises:
        CatError (INVALID_ARGUMENT): 编辑格式无效时。
    """
    valid_modes = {
        "replace_exact", "replace_lines",
        "insert_before", "insert_after",
    }
    ops: list[EditOp] = []

    if not raw_edits:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "Edits list must not be empty",
        )

    for i, ed in enumerate(raw_edits):
        mode = ed.get("mode", "replace_exact")
        if mode not in valid_modes:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"Edit #{i}: invalid mode {mode!r}",
            )

        op = EditOp(mode=mode)
        op.old_text = ed.get("old_text", "")
        op.new_text = ed.get("new_text", "")
        op.start_line = ed.get("start_line", 0)
        op.end_line = ed.get("end_line", 0)

        if mode == "replace_exact" and not op.old_text:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"Edit #{i}: old_text is required for replace_exact",
            )
        if mode in ("insert_before", "insert_after") and op.start_line <= 0:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"Edit #{i}: start_line is required for {mode}",
            )
        if mode == "replace_lines" and op.start_line <= 0:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"Edit #{i}: start_line and end_line are required for replace_lines",
            )
        if mode == "replace_lines" and op.start_line > op.end_line:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"Edit #{i}: start_line ({op.start_line}) > end_line ({op.end_line})",
            )

        ops.append(op)

    return ops


def _apply_edits(
    content: str,
    edits: list[EditOp],
) -> tuple[str, str, int]:
    """应用编辑操作列表到文件内容。

    1. 匹配原版文本/行范围
    2. 检测歧义（匹配数 != 预期）
    3. 检测重叠 edits
    4. 按位置逆序应用
    5. 生成 unified diff

    Args:
        content: 文件原始内容。
        edits: 已校验的编辑操作列表。

    Returns:
        (diff_text, new_content, changed_lines).
    """
    lines = content.splitlines(keepends=True)

    # 匹配所有 edit 的位置
    spans: list[tuple[int, int, int, str, str]] = []
    # (start_byte, end_byte, edit_index, old_text, new_text)

    for i, edit in enumerate(edits):
        if edit.mode == "replace_exact":
            start = content.find(edit.old_text)
            if start == -1:
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Edit #{i}: old_text not found in file",
                )
            # 检查歧义
            second = content.find(edit.old_text, start + 1)
            if second != -1:
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Edit #{i}: ambiguous match (found 2+ occurrences)",
                )
            end = start + len(edit.old_text)
            spans.append((start, end, i, edit.old_text, edit.new_text))

        elif edit.mode == "replace_lines":
            if edit.start_line < 1 or edit.end_line > len(lines):
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Edit #{i}: line range out of bounds "
                    f"({edit.start_line}-{edit.end_line}, file has {len(lines)} lines)",
                )
            start_byte = sum(len(l) for l in lines[:edit.start_line - 1])
            end_byte = sum(len(l) for l in lines[:edit.end_line])
            old_text_seg = content[start_byte:end_byte]
            spans.append((start_byte, end_byte, i, old_text_seg, edit.new_text))

        elif edit.mode == "insert_before":
            if edit.start_line < 1 or edit.start_line > len(lines) + 1:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Edit #{i}: start_line out of bounds",
                )
            start_byte = sum(len(l) for l in lines[:edit.start_line - 1])
            spans.append((start_byte, start_byte, i, "", edit.new_text))

        elif edit.mode == "insert_after":
            if edit.start_line < 1 or edit.start_line > len(lines):
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Edit #{i}: start_line out of bounds",
                )
            end_byte = sum(len(l) for l in lines[:edit.start_line])
            spans.append((end_byte, end_byte, i, "", edit.new_text))

    # 检测重叠（按 byte range）
    spans_sorted = sorted(spans, key=lambda x: x[0])  # 按开始位置排序
    for j in range(len(spans_sorted) - 1):
        curr = spans_sorted[j]
        nxt = spans_sorted[j + 1]
        if curr[1] > nxt[0]:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Overlapping edits detected between edit "
                f"#{curr[2]} and edit #{nxt[2]}",
            )

    # 按位置逆序应用（从文件尾向文件头，避免偏移累积）
    new_content = content
    for start, end, i, old_t, new_t in reversed(spans_sorted):
        new_content = new_content[:start] + new_t + new_content[end:]

    # 生成 diff
    old_lines = content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines, new_lines,
            fromfile="original", tofile="modified",
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    # 计算修改行数（diff 中不以 ---/+++/@@ 开头的删除/添加行）
    changed_lines = sum(
        1 for ln in diff_lines
        if ln.startswith("+") or ln.startswith("-")
    )

    return diff_text, new_content, changed_lines


def _get_op_id(request_id: str, op: Any) -> str:
    """安全获取操作记录 ID。"""
    if op:
        return op.get("id", request_id)
    return request_id


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
