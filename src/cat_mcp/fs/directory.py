"""层级目录遍历与稳定分页。

提供 :class:`DirectoryLister` 用于安全的深度受限目录遍历、
glob 过滤、敏感文件跳过和有界快照分页。

用法::

    from cat_mcp.fs.directory import DirectoryLister

    lister = DirectoryLister(resolver, sensitive_checker)
    result = lister.list_directory(
        path="src",
        depth=2,
        limit=50,
    )
    # result = {entries: [...], has_more: bool, next_cursor: str, ...}
"""
# 依据: Task-R-20261126012656445-P0

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

import pathspec

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.fs.paths import SecurePathResolver
from cat_mcp.fs.sensitive import SensitiveFileChecker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认忽略的目录名（无论 .gitignore 内容如何，永远忽略）
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
# 目录条目
# ---------------------------------------------------------------------------


@dataclass
class DirEntry:
    """单个目录条目，包含基本的文件系统元信息。

    Attributes:
        path: 工程相对路径（POSIX 风格，如 ``"src/main.py"``）。
        name: 条目基本名称（如 ``"main.py"``）。
        type: 条目类型，``"file"`` 或 ``"dir"``。
        depth: 相对遍历起始目录的深度（1 起始）。
        size: 文件大小（字节），目录固定为 0。
        mtime: 最后修改时间的 Unix 时间戳（float）。
        child_count: 对于目录，过滤后的可见直接子条目数；
                     非目录固定为 0。
    """

    path: str
    name: str
    type: str
    depth: int
    size: int
    mtime: float
    child_count: int


def _dir_entry_to_dict(entry: DirEntry) -> dict[str, Any]:
    """将 :class:`DirEntry` 转为可序列化的字典。

    Args:
        entry: 目录条目。

    Returns:
        符合契约输出格式的字典。
    """
    return {
        "path": entry.path,
        "name": entry.name,
        "type": entry.type,
        "depth": entry.depth,
        "size": entry.size,
        "mtime": entry.mtime,
        "child_count": entry.child_count,
    }


# ---------------------------------------------------------------------------
# .gitignore 加载
# ---------------------------------------------------------------------------


def _load_gitignore(root_path: str) -> pathspec.PathSpec:
    """从 *root_path* 加载 ``.gitignore`` 规则。

    Args:
        root_path: 工程根目录绝对路径。

    Returns:
        ``pathspec.PathSpec`` 实例，文件不存在时返回空匹配。
    """
    gitignore_path = os.path.join(root_path, ".gitignore")
    try:
        with open(gitignore_path, "r") as fh:
            lines = fh.readlines()
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)
    except (FileNotFoundError, PermissionError, OSError):
        return pathspec.PathSpec.from_lines("gitwildmatch", [])


# ---------------------------------------------------------------------------
# 辅助函数
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


def _compile_glob(pattern: str) -> Callable[[str], bool]:
    """将 Unix glob 模式编译为匹配函数。

    使用 ``fnmatch`` 实现简单的文件名/路径匹配。

    Args:
        pattern: Unix glob 模式（如 ``"*.py"``）。

    Returns:
        接收名称字符串、返回 ``bool`` 的匹配函数。
    """
    import fnmatch as _fnmatch

    def _match(name: str) -> bool:
        return _fnmatch.fnmatch(name, pattern)

    return _match


def _raise_cat_error(exc: OSError, path: str) -> None:
    """将 ``OSError`` 映射为 ``CatError``。

    Args:
        exc: 原始操作系统异常。
        path: 操作路径（用于错误消息）。

    Raises:
        CatError: 总是抛出。
    """
    if exc.errno == errno.ENOENT:
        raise CatError(ErrorCode.FILE_NOT_FOUND, str(exc)) from exc
    if exc.errno in (errno.EACCES, errno.EPERM):
        raise CatError(ErrorCode.FORBIDDEN, str(exc)) from exc
    if exc.errno == errno.ELOOP:
        raise CatError(ErrorCode.UNSAFE_PATH, str(exc)) from exc
    raise CatError(ErrorCode.OPERATION_FAILED, str(exc)) from exc


def _entry_sort_key(entry: DirEntry) -> tuple:
    """排序键：目录优先于文件，同类型按路径字母序。

    Args:
        entry: 目录条目。

    Returns:
        可排序的元组 ``(type_order, path_lower)``。
    """
    return (0 if entry.type == "dir" else 1, entry.path.lower())


# ---------------------------------------------------------------------------
# DirectoryLister
# ---------------------------------------------------------------------------


class DirectoryLister:
    """层级目录遍历与稳定分页。

    在 :class:`SecurePathResolver` 安全路径解析的基础上执行
    深度受限的目录遍历，支持 glob 包含/排除过滤、敏感文件跳过
    和有界快照分页（过期返回 CURSOR_EXPIRED）。

    Args:
        resolver: 已初始化的 :class:`SecurePathResolver`。
        sensitive_checker: 已初始化的 :class:`SensitiveFileChecker`。
        gitignore: 可选的 ``.gitignore`` 规则。默认从工程根自动加载。
    """

    def __init__(
        self,
        resolver: SecurePathResolver,
        sensitive_checker: SensitiveFileChecker,
        gitignore: pathspec.PathSpec | None = None,
    ) -> None:
        self._resolver = resolver
        self._sensitive_checker = sensitive_checker

        if gitignore is not None:
            self._gitignore = gitignore
        else:
            self._gitignore = _load_gitignore(resolver.project_root)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def list_directory(
        self,
        path: str,
        path_base: str = ".",
        depth: int = 1,
        cursor: str | None = None,
        limit: int = 50,
        include: str | None = None,
        exclude: str | None = None,
    ) -> dict[str, Any]:
        """遍历目录并返回过滤后的分页条目列表。

        参数校验 -> 安全路径解析 -> 遍历与过滤 -> 排序 -> 分页。

        Args:
            path: 目标目录路径（相对工程根，或相对 *path_base*）。
            path_base: 基准路径（通常是 ``"."`` 或工作空间 CWD）。
            depth: 遍历深度（0–8）。depth=0 只返回目标目录自身。
                   depth=1（默认）返回直接子条目。
                   depth=2 返回子条目和孙条目，依此类推。
            cursor: 不透明分页游标（第一页传 ``None``）。
                    过期游标将导致 ``CURSOR_EXPIRED`` 错误。
            limit: 每页最大条目数（1–200，默认 50）。
            include: 包含过滤 glob 模式。仅路径**名称**匹配该模式的
                     条目被包含（如 ``"*.py"``）。
            exclude: 排除过滤 glob 模式。路径**名称**匹配该模式的
                     条目被排除（优先于 include）。
            responsive_large_dir: 对于包含大量文件的目录，是否主动截断
                以避免超时（默认启用；设为 ``False`` 关闭）。

        Returns:
            字典包含:
            - ``entries``: 条目字典列表（每项含 path/name/type/depth/
              size/mtime/child_count）。
            - ``has_more``: 布尔值，表示是否有更多条目。
            - ``next_cursor``: 下一页的不透明游标（无更多时为 ``None``）。
            - ``truncated_reason``: 如果条目被截断，说明原因（否则 ``None``）。

        Raises:
            CatError: 参数无效、路径不安全、快照过期等。
        """
        # 1. 参数校验
        if depth < 0 or depth > 8:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"depth must be between 0 and 8, got {depth}",
            )
        if limit < 1 or limit > 200:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"limit must be between 1 and 200, got {limit}",
            )

        # 2. 安全解析目标路径
        resolved = self._resolver.resolve(path, base=path_base)
        try:
            return self._do_list(
                resolved=resolved,
                depth=depth,
                cursor=cursor,
                limit=limit,
                include=include,
                exclude=exclude,
            )
        finally:
            resolved.close()

    # ------------------------------------------------------------------
    # 内部：执行遍历
    # ------------------------------------------------------------------

    def _do_list(
        self,
        resolved: Any,
        depth: int,
        cursor: str | None,
        limit: int,
        include: str | None,
        exclude: str | None,
    ) -> dict[str, Any]:
        """在已解析路径上执行目录遍历。"""
        # stat 目标路径
        st = resolved.stat()

        # depth=0：只返回目标目录自身
        if depth == 0:
            return self._make_root_entry(resolved, st)

        # 检查目标是否为目录
        if not stat_module.S_ISDIR(st.st_mode):
            raise CatError(
                ErrorCode.NOT_DIRECTORY,
                f"Path is not a directory: {resolved.resolved_path}",
            )

        # 打开目标目录的 fd
        try:
            target_fd = os.open(
                resolved.name,
                _O_DIR_FLAGS,
                dir_fd=resolved.dir_fd,
            )
        except OSError as exc:
            _raise_cat_error(exc, resolved.resolved_path)

        try:
            return self._scan_and_paginate(
                target_fd=target_fd,
                res_path=resolved.resolved_path,
                st=st,
                depth=depth,
                cursor=cursor,
                limit=limit,
                include=include,
                exclude=exclude,
            )
        finally:
            os.close(target_fd)

    def _make_root_entry(
        self,
        resolved: Any,
        st: os.stat_result,
    ) -> dict[str, Any]:
        """depth=0 时返回目标目录自身的条目。"""
        display_path = resolved.resolved_path
        display_name = PurePosixPath(display_path).name if display_path != "." else "."

        entry = DirEntry(
            path=display_path,
            name=display_name,
            type="dir",
            depth=0,
            size=0,
            mtime=st.st_mtime,
            child_count=0,
        )
        return {
            "entries": [_dir_entry_to_dict(entry)],
            "has_more": False,
            "next_cursor": None,
            "truncated_reason": None,
        }

    def _scan_and_paginate(
        self,
        target_fd: int,
        res_path: str,
        st: os.stat_result,
        depth: int,
        cursor: str | None,
        limit: int,
        include: str | None,
        exclude: str | None,
    ) -> dict[str, Any]:
        """在打开的目录 fd 上执行扫描并分页。"""
        # 计算快照标识
        snapshot_id = _compute_snapshot_id(st, res_path)

        # 验证游标快照是否过期
        if cursor is not None:
            _, cursor_snapshot_id = _decode_cursor(cursor)
            if cursor_snapshot_id != snapshot_id:
                raise CatError(
                    ErrorCode.CURSOR_EXPIRED,
                    "Directory snapshot has expired; entries changed since cursor was created",
                )

        # 编译过滤模式
        include_fn = _compile_glob(include) if include else None
        exclude_fn = _compile_glob(exclude) if exclude else None

        base_path = res_path if res_path != "." else ""

        # 执行遍历，收集所有可见条目
        all_entries: list[DirEntry] = []

        # 扫描目标目录直接
        self._scan_entries(
            dir_fd=target_fd,
            parent_rel_path=base_path,
            current_depth=1,
            max_depth=depth,
            all_entries=all_entries,
            gitignore=self._gitignore,
            include_fn=include_fn,
            exclude_fn=exclude_fn,
        )

        # 排序：目录优先，同类型按路径字母序
        all_entries.sort(key=_entry_sort_key)

        # 分页
        page_offset = _decode_cursor(cursor)[0] if cursor else 0
        end = page_offset + limit
        page = all_entries[page_offset:end]
        has_more = end < len(all_entries)

        return {
            "entries": [_dir_entry_to_dict(e) for e in page],
            "has_more": has_more,
            "next_cursor": _encode_cursor(end, snapshot_id) if has_more else None,
            "truncated_reason": None,
        }

    def _scan_entries(
        self,
        dir_fd: int,
        parent_rel_path: str,
        current_depth: int,
        max_depth: int,
        all_entries: list[DirEntry],
        gitignore: pathspec.PathSpec,
        include_fn: Callable[[str], bool] | None,
        exclude_fn: Callable[[str], bool] | None,
    ) -> None:
        """递归扫描 *dir_fd* 对应的目录，填充 *all_entries*。

        Args:
            dir_fd: 当前目录的打开文件描述符。
            parent_rel_path: 当前目录的工程相对路径前缀
                （空字符串或 ``"sub/dir"`` 形式，无尾随斜杠）。
            current_depth: 当前扫描深度（1 起始）。
            max_depth: 最大扫描深度。
            all_entries: 收集条目的列表（副作用填充）。
            gitignore: ``.gitignore`` 规则。
            include_fn: include glob 匹配函数（或 ``None``）。
            exclude_fn: exclude glob 匹配函数（或 ``None``）。
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

                # 2. .gitignore 过滤
                rel_path = f"{parent_rel_path}/{name}" if parent_rel_path else name
                if gitignore.match_file(rel_path):
                    continue

                # 3. 敏感文件检查（denylist 永远优先）
                if self._sensitive_checker.is_sensitive(rel_path):
                    continue

                # 4. stat 信息（不跟随 symlink）
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                # 拒绝 symlink（安全策略）
                if stat_module.S_ISLNK(st.st_mode):
                    continue

                # 5. 确定类型
                is_dir_entry = stat_module.S_ISDIR(st.st_mode)

                # 6. include/exclude 过滤：检查当前条目是否应包含在输出中
                include_this = True
                if include_fn is not None and not include_fn(name):
                    include_this = False
                if exclude_fn is not None and exclude_fn(name):
                    include_this = False

                # 7. 计算子条目数（仅对目录且还有更深层时）
                child_count = 0
                if is_dir_entry:
                    child_count = self._count_children_filtered(
                        dir_fd, name, rel_path,
                    )

                # 8. 如果通过过滤，构建条目并添加到结果
                if include_this:
                    de = DirEntry(
                        path=rel_path,
                        name=name,
                        type="dir" if is_dir_entry else "file",
                        depth=current_depth,
                        size=0 if is_dir_entry else st.st_size,
                        mtime=st.st_mtime,
                        child_count=child_count,
                    )
                    all_entries.append(de)

                # 9. 始终递归遍历子目录（即使目录名本身被过滤），
                #    以便发现深层匹配的文件。但目录自身条目可能已跳过。
                if is_dir_entry and current_depth < max_depth:
                    try:
                        sub_fd = os.open(name, _O_DIR_FLAGS, dir_fd=dir_fd)
                    except OSError:
                        continue
                    try:
                        self._scan_entries(
                            dir_fd=sub_fd,
                            parent_rel_path=rel_path,
                            current_depth=current_depth + 1,
                            max_depth=max_depth,
                            all_entries=all_entries,
                            gitignore=gitignore,
                            include_fn=include_fn,
                            exclude_fn=exclude_fn,
                        )
                    finally:
                        os.close(sub_fd)

    def _count_children_filtered(
        self,
        parent_fd: int,
        dir_name: str,
        dir_rel_path: str,
    ) -> int:
        """计算目录中过滤后的可见直接子条目数。

        Args:
            parent_fd: 父目录的 fd。
            dir_name: 目标子目录的名称。
            dir_rel_path: 目标子目录的工程相对路径。

        Returns:
            过滤后的可见子条目数量。
        """
        count = 0
        try:
            sub_fd = os.open(dir_name, _O_DIR_FLAGS, dir_fd=parent_fd)
        except OSError:
            return 0

        try:
            with os.scandir(sub_fd) as it:
                for e in it:
                    ename = e.name
                    if ename in _DEFAULT_IGNORE_DIRS:
                        continue
                    erel = f"{dir_rel_path}/{ename}" if dir_rel_path else ename
                    if self._gitignore.match_file(erel):
                        continue
                    if self._sensitive_checker.is_sensitive(erel):
                        continue
                    count += 1
        except OSError:
            pass
        finally:
            os.close(sub_fd)

        return count

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def resolver(self) -> SecurePathResolver:
        """关联的路径安全解析器。"""
        return self._resolver

    @property
    def sensitive_checker(self) -> SensitiveFileChecker:
        """关联的敏感文件检查器。"""
        return self._sensitive_checker


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_directory_tools(
    server: Any,
    workspace_mgr: Any,
    state_manager: Any,
) -> None:
    """注册目录相关 MCP 工具（``cat_list_directory``）。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: :class:`~cat_mcp.workspace.WorkspaceManager` 实例。
        state_manager: :class:`~cat_mcp.state.StateManager` 实例。
    """
    # 依据: Task-R-20261126012656445-P0
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.sensitive import get_checker

    @cat_tool(
        server,
        name="cat_list_directory",
        description=(
            "层级目录遍历——按深度逐层查看工程目录内容，不返回文件正文。"
            "支持分页、depth（0-8，默认1）、include/exclude glob 过滤；"
            "默认忽略 node_modules、target、dist、build、.git 及 .gitignore 匹配项。"
            "只读操作，不修改工程文件。"
        ),
        read_only=True,
    )
    async def list_directory_handler(
        workspace_id: str,
        path: str,
        path_base: str | None = None,
        depth: int | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        include: str | None = None,
        exclude: str | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_list_directory`` MCP 工具调用。

        Args:
            workspace_id: 工作空间标识。
            path: 目标目录路径（相对工程根，或相对 *path_base*）。
            path_base: 基准路径（默认使用工作空间当前 CWD）。
            depth: 遍历深度（0-8，默认 1）。
            cursor: 分页游标。
            limit: 每页条目数（1-200，默认 50）。
            include: include glob 模式。
            exclude: exclude glob 模式。

        Returns:
            分页的目录条目列表。
        """
        # 1. 获取认证主体
        subject = get_current_subject()

        # 2. 获取工作空间
        workspace = workspace_mgr._get_active_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        # 3. 获取工程根路径
        project = state_manager.get_project(project_id)
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
            path_base = workspace.get("cwd", ".")

        # 5. 创建 SecurePathResolver
        resolver = SecurePathResolver(project_root)

        # 6. 获取 SensitiveFileChecker
        sensitive_checker = get_checker()

        # 7. 创建 DirectoryLister
        lister = DirectoryLister(resolver, sensitive_checker)

        # 8. 执行目录遍历
        return lister.list_directory(
            path=path,
            path_base=path_base,
            depth=depth or 1,
            cursor=cursor,
            limit=limit or 50,
            include=include,
            exclude=exclude,
        )
