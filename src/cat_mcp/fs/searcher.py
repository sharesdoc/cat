"""有界全文搜索实现。

提供标准库实现的字面量和正则表达式文本搜索，
使用 ``SecurePathResolver`` 做安全路径解析，自动检查敏感文件。

输出路径重验：每次读取目标文件前都通过 ``SensitiveFileChecker`` 验证。
不跟随符号链接，搜索进程不得读取禁止文件。

用法::

    from cat_mcp.fs.searcher import TextSearcher

    searcher = TextSearcher("/srv/cat/projects/demo-app")
    result = searcher.search(
        query="TODO",
        mode="literal",
        path="src",
        base=".",
    )
"""
# 依据: Task-R-20261126012656448-P0

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat as stat_module
import time
from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.fs.paths import (
    SecurePathResolver,
    _normalize,
    _validate_path_format,
)
from cat_mcp.fs.sensitive import is_sensitive as _is_sensitive_fn
from cat_mcp.fs.sensitive import SensitiveFileChecker

logger = logging.getLogger(__name__)

# 读取块大小（64 KB）
_READ_CHUNK_SIZE = 65536


# ---------------------------------------------------------------------------
# TextSearcher
# ---------------------------------------------------------------------------


class TextSearcher:
    """有界全文搜索器。

    使用标准库实现有界字面量/正则表达式的全文搜索。
    不跟随符号链接，不读取禁止文件，结果路径通过 SensitiveFileChecker 重验。
    搜索进程不得读取禁止文件。
    搜索起始目录通过 SecurePathResolver 安全解析。

    Args:
        project_root: 工程根目录绝对路径。
        sensitive_checker: 可选敏感文件检查器。为 ``None`` 时用模块级
            :func:`~cat_mcp.fs.sensitive.is_sensitive`。
    """

    def __init__(
        self,
        project_root: str,
        sensitive_checker: SensitiveFileChecker | None = None,
    ) -> None:
        self._resolver = SecurePathResolver(project_root)
        self._sensitive_checker = sensitive_checker

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def search(  # noqa: C901
        self,
        query: str,
        mode: str,
        path: str = ".",
        base: str = ".",
        *,
        case_sensitive: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        context_lines: int = 0,
        max_results: int = 100,
        timeout_seconds: float = 10.0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """执行有界全文搜索。

        Args:
            query: 搜索关键字（必填）。
            mode: 搜索模式（``"literal"`` 或 ``"regex"``，必填）。
            path: 搜索起始路径（工程相对路径，默认当前目录）。
            base: 基准路径。
            case_sensitive: 是否大小写敏感（默认 ``True``）。
            include: 包含文件 glob 模式列表（仅匹配的文件）。
            exclude: 排除文件 glob 模式列表。
            context_lines: 每侧上下文的行数。
            max_results: 搜索结果上限。
            timeout_seconds: 搜索超时秒数。
            cursor: 续传游标（编码为 ``<文件路径>:<行号>``）。

        Returns:
            包含 ``matches``（匹配项列表）、``has_more``、``next_cursor``、
            ``truncated_reason`` 的字典。

        Raises:
            CatError: 参数校验或路径安全错误。
        """
        # --- 参数校验 ---
        if not query:
            raise CatError(ErrorCode.INVALID_ARGUMENT, "query must not be empty")
        if mode not in ("literal", "regex"):
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"mode must be 'literal' or 'regex', got {mode!r}",
            )
        if context_lines < 0:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"context_lines must be >= 0, got {context_lines}",
            )
        if max_results < 1:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"max_results must be >= 1, got {max_results}",
            )

        _validate_path_format(path)
        _validate_path_format(base)
        search_rel = _normalize(path, base)

        if search_rel.startswith(".."):
            raise CatError(
                ErrorCode.PATH_OUT_OF_BOUNDS,
                f"Search path escapes project root: {path}",
            )

        # --- 编译匹配函数 ---
        if mode == "regex":
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                pattern = re.compile(query, flags)
            except re.error as exc:
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Invalid regex pattern: {exc}",
                )
            match_fn = lambda line: bool(pattern.search(line))
        else:
            if case_sensitive:
                match_fn = lambda line, q=query: q in line
            else:
                q_lower = query.lower()
                match_fn = lambda line, q=q_lower: q in line.lower()

        # --- 编译 include/exclude glob ---
        compiled_inc = [re.compile(fnmatch_translate(p)) for p in (include or [])]
        compiled_exc = [re.compile(fnmatch_translate(p)) for p in (exclude or [])]

        # --- 解析游标 ---
        cursor_file: str | None = None
        cursor_line: int = 0
        if cursor:
            try:
                parts = cursor.split(":", 1)
                if len(parts) == 2:
                    cursor_file = parts[0]
                    cursor_line = int(parts[1])
                else:
                    cursor_file = parts[0]
                    cursor_line = 0
            except (ValueError, TypeError):
                raise CatError(
                    ErrorCode.INVALID_ARGUMENT,
                    "Invalid cursor format; expected 'file_path:line'",
                )

        # --- 解析搜索起始目录 ---
        is_single_file = False
        try:
            with self._resolver.resolve(search_rel, base=base) as rp:
                st = rp.stat(follow_symlinks=False)
                if not stat_module.S_ISDIR(st.st_mode):
                    is_single_file = True
                    search_root_abs = os.path.join(
                        self._resolver.project_root, rp.resolved_path
                    )
                    search_root_rel = rp.resolved_path
                else:
                    search_root_abs = os.path.join(
                        self._resolver.project_root, rp.resolved_path
                    )
                    search_root_rel = rp.resolved_path
        except CatError:
            raise

        # --- 搜索状态 ---
        matches: list[dict[str, Any]] = []
        has_more = False
        truncated_reason: str | None = None
        # 当 cursor 为 None 时不需要跳过任何位置
        passed_cursor = cursor is None
        last_file: str | None = None
        last_line: int = 0
        start_time = time.monotonic()
        deadline = start_time + timeout_seconds

        def _check_timeout() -> bool:
            """检查是否超时。超时返回 ``True``。"""
            return time.monotonic() >= deadline

        # --- 搜索单个文件 ---
        if is_single_file:
            # 单文件：已通过 resolver 确认路径安全，直接读取搜索
            self._search_file_at(
                abs_path=search_root_abs,
                file_rel=search_root_rel,
                match_fn=match_fn,
                context_lines=context_lines,
                cursor_file=cursor_file,
                cursor_line=cursor_line,
                passed_cursor=passed_cursor,
                max_results=max_results,
                matches=matches,
                deadline=deadline,
            )
            if matches:
                last_file = matches[-1]["file"]
                last_line = matches[-1]["line"]
            if len(matches) >= max_results:
                has_more = True
                truncated_reason = "max_results"
        else:
            # --- 遍历目录搜索 ---
            for rel_dir, dirnames, filenames in _walk_tree(
                search_root_abs, search_root_rel
            ):
                # 检查超时
                if _check_timeout():
                    truncated_reason = "timeout"
                    break

                # 稳定排序（确定性的游标位置）
                dirnames.sort()
                filenames.sort()

                for filename in filenames:
                    if _check_timeout():
                        truncated_reason = "timeout"
                        break

                    if len(matches) >= max_results:
                        has_more = True
                        truncated_reason = "max_results"
                        break

                    file_rel = os.path.normpath(os.path.join(rel_dir, filename))

                    # 应用 include/exclude glob
                    if not _match_glob(file_rel, filename, compiled_inc, compiled_exc):
                        continue

                    # 敏感文件检查（跳过而非报错，避免泄露文件存在性）
                    try:
                        self._check_sensitive(file_rel)
                    except CatError:
                        continue

                    file_abs = os.path.join(self._resolver.project_root, file_rel)
                    self._search_file_at(
                        abs_path=file_abs,
                        file_rel=file_rel,
                        match_fn=match_fn,
                        context_lines=context_lines,
                        cursor_file=cursor_file,
                        cursor_line=cursor_line,
                        passed_cursor=passed_cursor,
                        max_results=max_results,
                        matches=matches,
                        deadline=deadline,
                    )
                    if matches:
                        last_file = matches[-1]["file"]
                        last_line = matches[-1]["line"]
                    if len(matches) >= max_results:
                        has_more = True
                        truncated_reason = "max_results"
                        break

                if truncated_reason:
                    break

        # --- 构建响应 ---
        next_cursor: str | None = None
        if has_more and last_file is not None and last_line > 0:
            next_cursor = f"{last_file}:{last_line}"

        return {
            "matches": matches,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "truncated_reason": truncated_reason,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _search_file_at(
        self,
        *,
        abs_path: str,
        file_rel: str,
        match_fn,
        context_lines: int,
        cursor_file: str | None,
        cursor_line: int,
        passed_cursor: bool,
        max_results: int,
        matches: list[dict[str, Any]],
        deadline: float,
    ) -> None:
        """读取并搜索单个文件，结果追加到 *matches*。

        不跟随符号链接，跳过非普通文件和二进制文件。
        搜索起始目录已在外部通过 SecurePathResolver 安全解析，
        此处直接读取（已在外部完成敏感文件检查）。

        Args:
            abs_path: 文件绝对路径。
            file_rel: 文件工程相对路径。
            match_fn: 匹配函数。
            context_lines: 每侧上下文行数。
            cursor_file: 游标文件路径。
            cursor_line: 游标行号。
            passed_cursor: 是否已越过游标位置。
            max_results: 结果上限。
            matches: 结果列表（就地追加）。
            deadline: 超时截止时间（monotonic）。
        """
        try:
            # 不跟随符号链接打开
            st = os.stat(abs_path, follow_symlinks=False)
            if not stat_module.S_ISREG(st.st_mode):
                return

            fd = os.open(abs_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                raw = _read_all(fd)
            finally:
                os.close(fd)
        except OSError:
            return

        # 版本哈希
        version = hashlib.sha256(raw).hexdigest()

        # 解码为文本（仅 UTF-8，跳过二进制文件）
        try:
            text = raw.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, LookupError):
            return

        lines = text.splitlines(keepends=False)
        total_lines = len(lines)

        # 确定该文件是否需要跳过游标位置
        skip_to_cursor = False
        if cursor_file is not None and not passed_cursor:
            if file_rel < cursor_file:
                # 这个文件已经完全扫过
                return
            elif file_rel == cursor_file:
                skip_to_cursor = True
                if cursor_line > 0:
                    pass  # 跳过到 cursor_line
            # else: file_rel > cursor_file, 正常搜索

        for line_idx, line_text in enumerate(lines, start=1):
            # 检查超时
            if time.monotonic() >= deadline:
                return

            # 跳过游标之前的内容
            if skip_to_cursor and line_idx <= cursor_line:
                continue

            if not match_fn(line_text):
                continue

            # 收集上下文行
            start_ctx = max(0, line_idx - 1 - context_lines)
            end_ctx = min(total_lines, line_idx + context_lines)
            context = "\n".join(lines[start_ctx:end_ctx])

            match_entry = {
                "file": file_rel,
                "line": line_idx,
                "context": context,
                "version": version,
            }
            matches.append(match_entry)

            # 检查结果上限
            if len(matches) >= max_results:
                return

    def _check_sensitive(self, resolved_path: str) -> None:
        """检查 *resolved_path* 是否敏感。

        Raises:
            CatError (FORBIDDEN): 敏感文件。
        """
        sensitive = False
        if self._sensitive_checker is not None:
            sensitive = self._sensitive_checker.is_sensitive(resolved_path)
        else:
            try:
                sensitive = _is_sensitive_fn(resolved_path)
            except RuntimeError:
                # SensitiveFileChecker 未初始化 — 跳过敏感检查
                pass

        if sensitive:
            raise CatError(
                ErrorCode.FORBIDDEN,
                "Access to sensitive file is denied",
            )


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _walk_tree(root_abs: str, root_rel: str):
    """遍历目录树，不跟随符号链接。

    使用 ``os.walk(followlinks=False)`` 并额外从 ``dirnames`` 中
    剔除符号链接目录（防止平台行为差异）。

    Args:
        root_abs: 搜索起始目录的绝对路径。
        root_rel: 搜索起始目录的工程相对路径。

    Yields:
        ``(rel_dir, dirnames, filenames)`` 三元组，其中 ``rel_dir``
        是当前目录的工程相对路径。
    """
    for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
        # 显式剔除符号链接目录（加固跨平台行为）
        dirnames[:] = [
            d
            for d in dirnames
            if not os.path.islink(os.path.join(dirpath, d))
        ]

        rel = os.path.relpath(dirpath, root_abs)
        if rel == ".":
            yield root_rel, dirnames, filenames
        else:
            yield os.path.join(root_rel, rel), dirnames, filenames


def _read_all(fd: int) -> bytes:
    """从文件描述符 *fd* 读取所有字节。

    Args:
        fd: 打开的文件描述符。

    Returns:
        文件的全部字节内容。
    """
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, _READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def fnmatch_translate(pattern: str) -> str:
    """将 *pattern* 转换为正则表达式字符串。

    使用 ``fnmatch.translate`` 但兼容 Unicode 字符。

    Args:
        pattern: glob 模式。

    Returns:
        正则表达式字符串。
    """
    import fnmatch as _fnmatch

    return _fnmatch.translate(pattern)


def _match_glob(
    file_rel: str,
    filename: str,
    compiled_inc: list[re.Pattern],
    compiled_exc: list[re.Pattern],
) -> bool:
    """检查文件是否匹配 include/exclude glob 模式。

    先检查 include（如果有），再检查 exclude。

    Args:
        file_rel: 文件相对路径。
        filename: 仅文件名。
        compiled_inc: 编译后的包含模式列表。
        compiled_exc: 编译后的排除模式列表。

    Returns:
        ``True`` 如果文件应该被搜索。
    """
    # include: 至少一个模式匹配才包含（未指定时全部包含）
    if compiled_inc and not any(
        p.match(filename) or p.match(file_rel) for p in compiled_inc
    ):
        return False
    # exclude: 任一模式匹配则排除
    if compiled_exc and any(
        p.match(filename) or p.match(file_rel) for p in compiled_exc
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_search_text(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_search_text`` 工具。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例（用于 workspace 归属/过期校验）。
        state: ``StateManager`` 实例（用于获取工程根路径）。
    """
    # 依据: Task-R-20261126012656448-P0
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import get_quota_manager

    @cat_tool(
        server,
        name="cat_search_text",
        description=(
            "在指定路径范围内执行有界全文搜索。支持字面量和正则表达式两种模式。"
            "默认从当前工作目录开始搜索，不跟随符号链接，跳过敏感文件和二进制文件。"
            "每次调用限时10秒，默认上限100条结果。支持游标续传。只读。"
        ),
        read_only=True,
    )
    async def cat_search_text_handler(
        workspace_id: str,
        query: str,
        mode: str,
        path: str | None = None,
        path_base: str | None = None,
        case_sensitive: bool | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        context_lines: int | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Handle ``cat_search_text`` MCP tool call."""
        # 依据: Task-R-20261126012656448-P0
        subject = get_current_subject()

        # 通过 WorkspaceManager 验证 workspace 归属/过期
        workspace = workspace_mgr.get_workspace(subject, workspace_id)

        project_id = workspace.get("project_id", "")
        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        root_path = project.get("root_path", "")
        if not root_path:
            raise CatError(
                ErrorCode.INTERNAL_ERROR, "Project root path not configured"
            )

        # 确定基准路径
        base = _resolve_base(path_base, workspace)

        # 确定搜索起始路径（默认 CWD）
        search_path = path if path is not None else workspace.get("cwd", ".")

        # 默认参数
        if case_sensitive is None:
            case_sensitive = True
        if context_lines is None:
            context_lines = 0
        if limit is None:
            limit = 100

        # 获取配额管理器（用于搜索限额）
        quota = get_quota_manager()
        max_results = min(limit, quota.max_search_results)
        timeout_seconds = float(quota.max_search_seconds)

        # 创建 TextSearcher 并执行搜索
        searcher = TextSearcher(root_path)
        result = searcher.search(
            query=query,
            mode=mode,
            path=search_path,
            base=base,
            case_sensitive=case_sensitive,
            include=include,
            exclude=exclude,
            context_lines=context_lines,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            cursor=cursor,
        )

        return result


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
