"""严格的单文件统一差异补丁 MCP 工具。

提供 ``cat_apply_patch`` MCP 工具，将 unified diff 应用到单文件。
核心约束：
- 仅接受单文件 unified diff（拒绝多文件、重命名、二进制）
- 按行号和旧上下文严格匹配，拒绝模糊匹配
- 全部 hunks 验证通过后才写入
- 通过 AtomicWriter 原子写入
"""
# 依据: CAT-22

from __future__ import annotations

import difflib
import logging
import os
import re
import stat as stat_module
from dataclasses import dataclass
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# 补丁最大行数
_MAX_PATCH_LINES = 5000
# 最大文件大小
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB


@dataclass
class Hunk:
    """统一 diff 中的单个 hunk。"""
    old_start: int    # 原文件行号（1-indexed）
    old_count: int    # 原文件行数
    new_start: int    # 新文件行号
    new_count: int    # 新文件行数
    lines: list[str]  # hunk 内容行（含 +/-/  前缀）


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_patch_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_apply_patch`` 补丁工具。"""
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import check_concurrency, get_quota_manager

    @cat_tool(
        server,
        name="cat_apply_patch",
        description=(
            "将 unified diff 补丁应用到指定单文件。"
            "支持多 hunk 补丁，全部成功或全部回滚。"
            "拒绝多文件、二进制、重命名和创建/删除补丁。"
            "dry_run=True 仅预览不写入。"
        ),
        destructive=True,
    )
    async def apply_patch_handler(
        workspace_id: str,
        path: str,
        patch: str,
        dry_run: bool = False,
        expected_version: str | None = None,
        request_id: str | None = None,
        path_base: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_apply_patch`` MCP 工具调用。"""
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

            with resolver.resolve(path, base=base) as rp:
                resolved_path = rp.resolved_path

            # 敏感文件检查
            from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker
            if get_sensitive_checker().is_sensitive(resolved_path):
                raise CatError(ErrorCode.FORBIDDEN, "Path is sensitive")

            # ACL
            from cat_mcp.acl import get_access_checker
            if not get_access_checker().check(subject, project_id, resolved_path, "write"):
                raise CatError(ErrorCode.FORBIDDEN, "Not authorized")

            # 解析 unified diff
            hunks, target_file = _parse_patch(patch)

            # 拒绝目标文件不匹配或缺少 --- 头的补丁
            if not target_file:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Patch must specify a target file (--- header)",
                )
            # 规范化路径比较：移除 target_file 的 ./ 前缀，resolved_path 也标准化
            _norm_target = target_file.lstrip("./")
            _norm_resolved = resolved_path.lstrip("./")
            if _norm_target != _norm_resolved:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Patch targets a different file",
                )

            # 读取当前文件内容
            file_content = _read_file(resolver, resolved_path)
            lines = file_content.splitlines(keepends=True)

            # 验证并计算新内容
            # 用 "\n" 分割后取文本行（保留尾部空格，仅去掉行尾换行符）
            old_lines = file_content.split("\n")
            new_content, changed_lines = _apply_hunks(
                old_lines, hunks, lines,
            )

            if new_content == file_content:
                return {"diff": "", "changed_lines": 0, "unchanged": True}

            # 生成 diff 预览
            diff_lines = list(
                difflib.unified_diff(
                    lines, new_content.splitlines(keepends=True),
                    fromfile="original", tofile="modified",
                    lineterm="",
                )
            )
            diff_text = "\n".join(diff_lines)

            if dry_run:
                return {"diff": diff_text, "changed_lines": changed_lines, "dry_run": True}

            # 通过 AtomicWriter 写入
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

            # 审计日志
            from cat_mcp.fs.audit import get_audit_logger
            try:
                get_audit_logger().log(
                    subject=subject, project_id=project_id,
                    action="write.patch", outcome="success",
                    path=resolved_path, workspace_id=workspace_id,
                    bytes=len(content_bytes), correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for patch")

            logger.info(
                "Subject %s patched %s (%d lines changed)",
                subject, resolved_path, changed_lines,
            )

            return {"diff": diff_text, "changed_lines": changed_lines, "version": ctx.file_hash}

        except Exception:
            if request_id:
                try:
                    state.update_operation_status(request_id, "failed")
                except Exception:
                    logger.warning("Failed to update operation status")
            try:
                from cat_mcp.fs.audit import get_audit_logger
                get_audit_logger().log(
                    subject=subject, project_id=project_id,
                    action="write.patch", outcome="failure",
                    path=path, workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit failure for patch")
            raise
        finally:
            get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# Unified diff 解析器
# ---------------------------------------------------------------------------

_PATCH_HEADER_RE = re.compile(r"^--- (?:[ab]/)?(.+?)(?:\t.*)?$")
_PATCH_HEADER2_RE = re.compile(r"^\+\+\+ (?:[ab]/)?(.+?)(?:\t.*)?$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@")

# 拒绝的补丁类型（注意：不含 "index "，git diff 输出包含 "index" 行但不应拒绝）
_REJECT_PREFIXES = {
    "rename from ", "rename to ",
    "new file mode", "deleted file mode",
    "similarity index ",
    "copy from ", "copy to ",
}


def _parse_patch(patch_text: str) -> tuple[list[Hunk], str]:
    """解析 unified diff 文本。

    Returns:
        (hunks: list[Hunk], target_file: str)。
        target_file 可能为空（无 --- 头时）。
    """
    lines = patch_text.splitlines()
    if len(lines) > _MAX_PATCH_LINES:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"Patch exceeds maximum line count ({_MAX_PATCH_LINES})",
        )

    hunks: list[Hunk] = []
    target_file = ""
    current_hunk_lines: list[str] = []

    in_hunk = False
    reject_patch = False
    reject_reason = ""

    for line in lines:
        # 拒绝二进制补丁（包括 GIT binary patch 格式）
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "Binary patches are not supported",
            )

        # 拒绝 "\ No newline" 标记（hunk 重建时不处理）
        if line.startswith("\\ "):
            continue

        # 拒绝多文件/重命名/创建/删除指令
        for prefix in _REJECT_PREFIXES:
            if line.startswith(prefix):
                reject_patch = True
                reject_reason = f"Patch type not supported: {prefix.strip()}"

        # --- 文件头
        m = _PATCH_HEADER_RE.match(line)
        if m:
            if target_file and m.group(1) != target_file:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Multi-file patches are not supported",
                )
            target_file = m.group(1)
            continue

        # +++ 文件头
        if _PATCH_HEADER2_RE.match(line):
            continue

        # hunk 头
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current_hunk_lines and in_hunk:
                hunks.append(_finalize_hunk(current_hunk_lines))
                current_hunk_lines = []

            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) else 1

            current_hunk_lines = [line]
            in_hunk = True
            continue

        if in_hunk:
            current_hunk_lines.append(line)

    # 最后一个 hunk
    if current_hunk_lines and in_hunk:
        hunks.append(_finalize_hunk(current_hunk_lines))

    if reject_patch:
        raise CatError(ErrorCode.INVALID_ARGUMENT, reject_reason)

    if not hunks:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "No valid hunks found in patch",
        )

    return hunks, target_file


def _finalize_hunk(lines: list[str]) -> Hunk:
    """从 hunk 头行和内容行构建 Hunk 对象。"""
    header = lines[0]
    m = _HUNK_HEADER_RE.match(header)
    if not m:
        raise CatError(ErrorCode.INVALID_ARGUMENT, f"Invalid hunk header: {header}")

    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) else 1

    return Hunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        lines=list(lines),
    )


# ---------------------------------------------------------------------------
# Hunk 应用
# ---------------------------------------------------------------------------


def _apply_hunks(
    old_lines: list[str],
    hunks: list[Hunk],
    raw_lines: list[str],
) -> tuple[str, int]:
    """验证并应用 hunks 到文件内容。

    Args:
        old_lines: 无换行符的文件行。
        hunks: 已解析的 hunk 列表。
        raw_lines: 带换行符的原始行。

    Returns:
        (new_content, changed_lines)。
    """
    # 从文件尾到文件头验证并计算新内容
    # 用文件行号验证 hunk 匹配，用 byte 范围做替换

    # 先验证所有 hunk 匹配
    for hunk in hunks:
        start_idx = hunk.old_start - 1
        hunk_old = [l for l in hunk.lines if l.startswith(" ") or l.startswith("-")]

        # 去除 +/-/ 前缀后对比
        expected = []
        for l in hunk.lines[1:]:  # 跳过 hunk 头
            if l.startswith(" ") or l.startswith("-"):
                expected.append(l[1:])

        actual = old_lines[start_idx:start_idx + len(expected)]
        if len(actual) != len(expected):
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Hunk at line {hunk.old_start}: "
                f"expected {len(expected)} lines, got {len(actual)}",
            )

        # 逐行对比上下文
        for i, (exp, act) in enumerate(zip(expected, actual)):
            if exp.rstrip("\n\r") != act.rstrip("\n\r"):
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Hunk at line {hunk.old_start}: "
                    f"context mismatch at offset {i}",
                )

    # 按旧行号逆序应用
    new_content = "".join(raw_lines)

    # 检测行尾风格（优先使用文件实际行尾）
    _line_end = "\r\n" if raw_lines and raw_lines[0].endswith("\r\n") else "\n"

    for hunk in reversed(hunks):
        old_start_byte = _line_to_byte(raw_lines, hunk.old_start - 1)
        old_end = hunk.old_start - 1 + hunk.old_count
        old_end_byte = _line_to_byte(raw_lines, old_end)

        # 构建替换文本（使用原始文件的行尾风格）
        new_hunk_text = ""
        for l in hunk.lines[1:]:
            if l.startswith("-"):
                continue
            elif l.startswith(" ") or l.startswith("+"):
                new_hunk_text += l[1:] + _line_end

        new_content = new_content[:old_start_byte] + new_hunk_text + new_content[old_end_byte:]

    changed_lines = sum(
        1 for h in hunks
        for l in h.lines[1:]
        if l.startswith("+") or l.startswith("-")
    )

    return new_content, changed_lines


def _line_to_byte(lines: list[str], line_idx: int) -> int:
    """计算指定行索引在原始串中的字节偏移。"""
    if line_idx <= 0:
        return 0
    return sum(len(l) for l in lines[:line_idx])


def _read_file(resolver: Any, path: str) -> str:
    """读取文件内容。"""
    buf = bytearray()
    chunk_size = 65536
    with resolver.resolve(path) as rp:
        st = rp.stat(follow_symlinks=False)
        if st.st_size > _MAX_FILE_BYTES:
            raise CatError(
                ErrorCode.FILE_TOO_LARGE,
                f"File too large for patching ({st.st_size} > {_MAX_FILE_BYTES})",
            )
        fd = rp.open(os.O_RDONLY)
        try:
            while len(buf) < st.st_size:
                chunk = os.read(fd, min(chunk_size, st.st_size - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
        finally:
            os.close(fd)
    return buf.decode("utf-8")


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


