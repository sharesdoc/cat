"""按名称和路径查找文件（glob 搜索）。

使用 SecurePathResolver + SensitiveFileChecker 安全遍历目录树，
支持 **/ 递归 glob、fnmatch 文件名匹配和有界快照分页。

用法::

    from cat_mcp.fs.finder import FileFinder

    finder = FileFinder(resolver, sensitive_checker)
    result = finder.find_files(
        path="src",
        path_base=".",
        glob="**/*.py",
        limit=50,
    )
    # result = {entries: [...], has_more: bool, next_cursor: str, ...}
"""
# 依据: Task-R-20261126012656447-P0

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import logging
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.fs.paths import SecurePathResolver, _raise_cat_error
from cat_mcp.fs.sensitive import SensitiveFileChecker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认忽略的目录名（搜索时跳过这些目录及子树）
_DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    "node_modules",
    "target",
    "dist",
    "build",
    ".git",
})

# 安全打开目录的标志位（macOS 兼容：O_RDONLY + O_NOFOLLOW + O_NONBLOCK + O_CLOEXEC）
_O_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# ---------------------------------------------------------------------------
# 分页游标编码
# ---------------------------------------------------------------------------


def _encode_cursor(offset: int, snapshot_id: str) -> str:
    """将偏移和快照 ID 编码为不透明游标字符串。

    Args:
        offset: 当前页的起始偏移。
        snapshot_id: 快照标识（目录 mtime/inode 的哈希）。

    Returns:
        Base64 编码的不透明游标。
    """
    payload = json.dumps({"o": offset, "s": snapshot_id}, separators=(",", ":"))
    return base64.b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, str]:
    """解码不透明游标。

    Returns:
        ``(offset, snapshot_id)`` 元组。

    Raises:
        CatError (INVALID_ARGUMENT): 游标格式无效。
    """
    try:
        payload = json.loads(base64.b64decode(cursor))
        return int(payload["o"]), str(payload["s"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise CatError(ErrorCode.INVALID_ARGUMENT, "Invalid cursor")


# ---------------------------------------------------------------------------
# 搜索结果条目
# ---------------------------------------------------------------------------


@dataclass
class FindEntry:
    """单个搜索结果条目。

    Attributes:
        path: 工程相对路径（POSIX 风格，如 ``"src/main.py"``）。
        name: 条目基本名称（如 ``"main.py"``）。
        type: 条目类型，``"file"`` 或 ``"dir"``。
        size: 文件大小（字节），目录固定为 0。
        mtime: 最后修改时间的 Unix 时间戳（float）。
    """

    path: str
    name: str
    type: str
    size: int
    mtime: float


def _find_entry_to_dict(entry: FindEntry) -> dict[str, Any]:
    """将 :class:`FindEntry` 转为可序列化的字典。

    Args:
        entry: 搜索结果条目。

    Returns:
        符合契约输出格式的字典。
    """
    return {
        "path": entry.path,
        "name": entry.name,
        "type": entry.type,
        "size": entry.size,
        "mtime": entry.mtime,
    }


# ---------------------------------------------------------------------------
# Glob 模式解析
# ---------------------------------------------------------------------------


def _parse_glob_pattern(pattern: str) -> tuple[str | None, str, bool]:
    """解析 glob 模式，提取递归前缀、文件名模式和是否递归。

    支持以下形式：

    - ``"*.py"`` → 非递归，文件名模式 ``"*.py"``
    - ``"**/*.py"`` → 递归，文件名模式 ``"*.py"``
    - ``"**/test_*.py"`` → 递归，文件名模式 ``"test_*.py"``
    - ``"sub/**/*.txt"`` → 递归，目录前缀 ``"sub"``，文件名模式 ``"*.txt"``

    Args:
        pattern: 用户提供的 glob 模式。

    Returns:
        ``(dir_prefix, filename_pattern, recursive)`` 三元组。
        *dir_prefix* 为 ``None`` 表示不限制搜索目录；
        非 ``None`` 时只在匹配该前缀的目录下搜索。
        *recursive* 表示是否需要递归遍历子目录。
    """
    # 检查是否包含 /
    if "/" not in pattern:
        return None, pattern, False

    # 查找 **/ 的出现位置
    double_star_idx = pattern.find("**/")

    if double_star_idx == 0:
        # 模式以 **/ 开头：如 "**/*.py"
        rest = pattern[3:]  # 去掉 "**/"
        return None, rest, True

    if double_star_idx > 0:
        # 模式在中间包含 **/：如 "sub/**/*.txt"
        dir_prefix = pattern[:double_star_idx].rstrip("/")
        rest = pattern[double_star_idx + 3:]  # 去掉 "**/"
        return dir_prefix, rest, True

    # 包含 / 但没有 **/，如 "dir/*.py" — 非递归，但有目录前缀
    # 这种情况下，我们把前缀提取出来，在指定前缀目录下非递归搜索
    parts = pattern.rsplit("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1], False

    return None, pattern, False


# ---------------------------------------------------------------------------
# 快照标识
# ---------------------------------------------------------------------------


def _compute_snapshot_id(st: os.stat_result, path: str) -> str:
    """计算目录快照标识。

    基于目录的 inode、mtime 和路径生成不可猜测的哈希值，
    用于检测目录内容是否已变化。

    Args:
        st: 目录的 ``os.stat_result``。
        path: 目录的工程相对路径。

    Returns:
        16 字符的十六进制字符串。
    """
    raw = f"{st.st_ino}:{st.st_mtime}:{st.st_size}:{path}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# FileFinder
# ---------------------------------------------------------------------------


class FileFinder:
    """按名称和路径查找文件（glob 搜索）。

    在 :class:`SecurePathResolver` 安全路径解析的基础上执行
    深度不限的 glob 搜索，支持递归（``**/``）和非递归搜索模式、
    默认忽略目录、敏感文件跳过和有界快照分页。

    Args:
        resolver: 已初始化的 :class:`SecurePathResolver`。
        sensitive_checker: 已初始化的 :class:`SensitiveFileChecker`。
    """

    def __init__(
        self,
        resolver: SecurePathResolver,
        sensitive_checker: SensitiveFileChecker,
    ) -> None:
        self._resolver = resolver
        self._sensitive_checker = sensitive_checker

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def find_files(
        self,
        path: str,
        path_base: str = ".",
        glob: str = "*",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """按 glob 模式搜索文件并返回分页结果。

        参数校验 -> 安全路径解析 -> 遍历与 glob 过滤 -> 分页。

        Args:
            path: 搜索起始路径（相对工程根，或相对 *path_base*）。
            path_base: 基准路径（通常是 ``"."`` 或工作空间 CWD）。
            glob: 文件名 glob 模式（必填），如 ``"*.py"`` 或 ``"**/*.txt"``。
                  含 ``**/`` 前缀时递归搜索所有子目录。
            cursor: 不透明分页游标（第一页传 ``None``）。
            limit: 每页结果上限（1–200，默认 50）。

        Returns:
            字典包含:
            - ``entries``: 匹配文件条目列表（每项含 name/path/type/size/mtime）。
            - ``has_more``: 布尔值，表示是否有更多结果。
            - ``next_cursor``: 下一页的不透明游标（无更多时为 ``None``）。
            - ``truncated_reason``: 如果结果被截断，说明原因（否则 ``None``）。

        Raises:
            CatError: 参数无效、路径不安全等。
        """
        # 1. 参数校验
        if not glob or not glob.strip():
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "glob pattern is required",
            )
        if limit < 1 or limit > 200:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"limit must be between 1 and 200, got {limit}",
            )

        # 2. 解析 glob 模式
        dir_prefix, filename_pattern, recursive = _parse_glob_pattern(glob)

        # 3. 安全解析目标路径
        with self._resolver.resolve(path, base=path_base) as resolved:
            st = resolved.stat()

            if not stat_module.S_ISDIR(st.st_mode):
                raise CatError(
                    ErrorCode.NOT_DIRECTORY,
                    f"Path is not a directory: {resolved.resolved_path}",
                )

            # 计算快照标识
            snapshot_id = _compute_snapshot_id(st, resolved.resolved_path)

            # 验证游标快照是否过期
            if cursor is not None:
                _, cursor_snapshot_id = _decode_cursor(cursor)
                if cursor_snapshot_id != snapshot_id:
                    raise CatError(
                        ErrorCode.CURSOR_EXPIRED,
                        "Directory snapshot has expired; entries changed since cursor was created",
                    )

            # 打开目标目录 fd
            try:
                target_fd = os.open(
                    resolved.name,
                    _O_DIR_FLAGS,
                    dir_fd=resolved.dir_fd,
                )
            except OSError as exc:
                _raise_cat_error(exc, resolved.resolved_path)

            try:
                all_entries: list[FindEntry] = []

                base_display = resolved.resolved_path if resolved.resolved_path != "." else ""

                # 4. 执行搜索
                self._search_entries(
                    dir_fd=target_fd,
                    parent_rel_path=base_display,
                    dir_prefix=dir_prefix,
                    filename_pattern=filename_pattern,
                    recursive=recursive,
                    current_depth=0,
                    all_entries=all_entries,
                )

                # 5. 排序：目录优先，同类型按路径字母序
                all_entries.sort(key=_entry_sort_key)

                # 6. 分页
                page_offset = _decode_cursor(cursor)[0] if cursor else 0
                end = page_offset + limit
                page = all_entries[page_offset:end]
                has_more = end < len(all_entries)

                return {
                    "entries": [_find_entry_to_dict(e) for e in page],
                    "has_more": has_more,
                    "next_cursor": _encode_cursor(end, snapshot_id) if has_more else None,
                    "truncated_reason": None,
                }
            finally:
                os.close(target_fd)

    # ------------------------------------------------------------------
    # 内部：搜索遍历
    # ------------------------------------------------------------------

    def _search_entries(
        self,
        dir_fd: int,
        parent_rel_path: str,
        dir_prefix: str | None,
        filename_pattern: str,
        recursive: bool,
        current_depth: int,
        all_entries: list[FindEntry],
    ) -> None:
        """在 *dir_fd* 对应的目录中搜索匹配的条目。

        Args:
            dir_fd: 当前目录的打开文件描述符。
            parent_rel_path: 当前目录的工程相对路径前缀
                （空字符串或 ``"sub/dir"`` 形式，无尾随斜杠）。
            dir_prefix: 目录前缀过滤器。非 ``None`` 时仅在此前缀
                路径因搜索且匹配时才收集其内容。
            filename_pattern: 文件名 glob 模式（如 ``"*.py"``）。
            recursive: 是否递归遍历子目录。
            current_depth: 当前递归深度（0 起始）。
            all_entries: 收集条目的列表（副作用填充）。
        """
        try:
            it = os.scandir(dir_fd)
        except OSError as exc:
            logger.warning("Cannot scandir fd %d: %s", dir_fd, exc)
            return

        with it:
            for entry in it:
                name = entry.name

                # 1. 永远忽略的目录
                if name in _DEFAULT_IGNORE_DIRS:
                    continue

                # 2. 构建相对路径
                rel_path = f"{parent_rel_path}/{name}" if parent_rel_path else name

                # 3. 敏感文件检查
                if self._sensitive_checker.is_sensitive(rel_path):
                    continue

                # 4. stat 信息（不跟随 symlink）
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                # 5. 拒绝 symlink（安全策略）
                if stat_module.S_ISLNK(st.st_mode):
                    continue

                is_dir_entry = stat_module.S_ISDIR(st.st_mode)

                # 6. 确定文件类型
                entry_type = "dir" if is_dir_entry else "file"

                # 7. 检查文件名是否匹配 glob 模式
                if fnmatch.fnmatch(name, filename_pattern):
                    # 如果 dir_prefix 不为 None，检查路径是否匹配
                    if dir_prefix is None or self._path_matches_prefix(rel_path, dir_prefix):
                        de = FindEntry(
                            path=rel_path,
                            name=name,
                            type=entry_type,
                            size=0 if is_dir_entry else st.st_size,
                            mtime=st.st_mtime,
                        )
                        all_entries.append(de)

                # 8. 递归遍历子目录（如果是目录且允许递归）
                if is_dir_entry and recursive:
                    try:
                        sub_fd = os.open(name, _O_DIR_FLAGS, dir_fd=dir_fd)
                    except OSError:
                        continue
                    try:
                        self._search_entries(
                            dir_fd=sub_fd,
                            parent_rel_path=rel_path,
                            dir_prefix=dir_prefix,
                            filename_pattern=filename_pattern,
                            recursive=recursive,
                            current_depth=current_depth + 1,
                            all_entries=all_entries,
                        )
                    finally:
                        os.close(sub_fd)

    @staticmethod
    def _path_matches_prefix(rel_path: str, prefix: str) -> bool:
        """检查 *rel_path* 是否位于 *prefix* 目录下或就是目录本身。

        Args:
            rel_path: 工程相对路径。
            prefix: 目录前缀（如 ``"sub/dir"``）。

        Returns:
            如果 *rel_path* 以 *prefix* 开头或等于 *prefix* 则返回 ``True``。
        """
        return rel_path == prefix or rel_path.startswith(prefix + "/")


def _entry_sort_key(entry: FindEntry) -> tuple:
    """排序键：目录优先于文件，同类型按路径字母序。

    Args:
        entry: 搜索结果条目。

    Returns:
        可排序的元组 ``(type_order, path_lower)``。
    """
    return (0 if entry.type == "dir" else 1, entry.path.lower())


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_finder_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_find_files`` 搜索工具。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例。
        state: ``StateManager`` 实例。
    """
    # 依据: Task-R-20261126012656447-P0
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.sensitive import get_checker

    # ------------------------------------------------------------------
    # cat_find_files
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_find_files",
        description=(
            "按名称和路径查找文件——使用 glob 模式在工程目录树中搜索匹配的文件/目录。"
            "支持 **/ 递归 glob（如 '**/*.py' 递归搜索所有 .py 文件）。"
            "默认跳过 node_modules、.git、target、dist、build 及敏感文件。"
            "返回工程相对路径，含 name/path/type/size/mtime 字段。"
            "只读操作，不修改工程文件。"
        ),
        read_only=True,
    )
    async def find_files_handler(
        workspace_id: str,
        path: str,
        path_base: str | None = None,
        glob: str = "*",
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_find_files`` MCP 工具调用。

        Args:
            workspace_id: 工作空间标识。
            path: 搜索起始路径（相对工程根，或相对 *path_base*）。
            path_base: 基准路径（``"cwd"`` 或 ``"root"``，默认 CWD）。
            glob: 文件名 glob 模式（如 ``"*.py"`` 或 ``"**/*.txt"``）。必填。
            cursor: 分页游标。
            limit: 每页结果上限（1-200，默认 50）。

        Returns:
            分页的匹配文件列表。
        """
        # 1. 获取认证主体
        subject = get_current_subject()

        # 2. 获取工作空间
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

        # 4. 确定 path_base（默认工作空间 CWD）
        if path_base is None:
            path_base = "cwd"

        base = _resolve_base(path_base, workspace)

        # 5. 创建 SecurePathResolver
        resolver = SecurePathResolver(project_root)

        # 6. 获取 SensitiveFileChecker
        sensitive_checker = get_checker()

        # 7. 创建 FileFinder
        finder = FileFinder(resolver, sensitive_checker)

        # 8. 执行搜索
        return finder.find_files(
            path=path,
            path_base=base,
            glob=glob,
            cursor=cursor,
            limit=limit or 50,
        )


def _resolve_base(path_base: str | None, workspace: dict[str, Any]) -> str:
    """解析 ``path_base`` 参数为工程相对路径。

    Args:
        path_base: ``"cwd"`` 或 ``"root"``。
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
