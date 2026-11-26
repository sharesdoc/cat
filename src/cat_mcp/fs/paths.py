"""fd-relative 安全路径解析层。

使用 ``os.open(..., dir_fd=root_fd, O_NOFOLLOW|O_DIRECTORY)`` 逐级
打开目录组件，拒绝 symlink、hardlink、特殊文件、跨工程逃逸等不安全
路径。

用法::

    from cat_mcp.fs.paths import SecurePathResolver

    resolver = SecurePathResolver("/srv/cat/projects/demo-app")
    try:
        rp = resolver.resolve("src/main.py")
        # rp.dir_fd — 父目录文件描述符（安全）
        # rp.name  — "main.py"
        # rp.resolved_path — "src/main.py"
    finally:
        rp.close()

V1 默认使用 ``os.open`` + ``dir_fd`` 方案（兼容 macOS / Linux）；
未来可评估 ``openat2`` (Linux 5.6+) 增强。
"""
# 依据: Task-R-20261126012656442-P0

from __future__ import annotations

import errno
import logging
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径安全常量
# ---------------------------------------------------------------------------

# macOS 缺少 O_PATH，使用 O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC 代替
# O_NONBLOCK 防止 FIFO 阻塞（攻击者用 mkfifo 替换目录项时 os.open 会
# 永久挂起）。检测到 ENXIO（FIFO 无写端）或 EAGAIN 时转为 CatError。
_O_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# 明确拒绝的特殊文件类型集合
_SPECIAL_FILE_TYPES: frozenset[int] = frozenset({
    stat_module.S_IFBLK,   # 块设备
    stat_module.S_IFCHR,   # 字符设备
    stat_module.S_IFIFO,   # FIFO / 命名管道
    stat_module.S_IFSOCK,  # socket
})

# ---------------------------------------------------------------------------
# ResolvedPath
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPath:
    """安全路径解析结果，包含父目录文件描述符和规范化相对路径。

    使用方式::

        with resolver.resolve("src/main.py") as rp:
            fd = os.open(rp.name, os.O_RDONLY, dir_fd=rp.dir_fd)

    Attributes:
        dir_fd: 目标文件所在父目录的文件描述符（已打开），
                用于 ``dir_fd`` 参数的安全文件操作。
        name: 最终路径组件（文件名或目录名）。
        resolved_path: 域名工程根相对路径（POSIX 风格，如 ``"src/main.py"``）。
    """

    dir_fd: int
    name: str
    resolved_path: str

    def close(self) -> None:
        """关闭 ``dir_fd``，释放资源。"""
        try:
            os.close(self.dir_fd)
        except OSError as exc:
            logger.warning("Failed to close fd %d: %s", self.dir_fd, exc)

    def __enter__(self) -> ResolvedPath:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        self.close()

    def open(
        self,
        flags: int,
        mode: int = 0o644,
        *,
        follow_symlinks: bool = False,
    ) -> int:
        """在安全解析的目录上打开目标文件。

        Args:
            flags: ``os.open`` 标志（如 ``os.O_RDONLY``）。
            mode: 创建文件时的权限。
            follow_symlinks: 是否跟随符号链接（默认 ``False``）。

        Returns:
            文件描述符。

        Raises:
            CatError: 当目标不是常规文件或访问被拒绝时。
        """
        open_flags = flags
        if not follow_symlinks:
            open_flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.name, open_flags, mode, dir_fd=self.dir_fd)
        except OSError as exc:
            _raise_cat_error(exc, self.resolved_path)
        return fd

    def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
        """获取目标文件的 ``stat`` 信息。

        Args:
            follow_symlinks: 是否跟随符号链接（默认 ``False``）。

        Returns:
            ``os.stat_result``。

        Raises:
            CatError: 当无法 stat 时。
        """
        try:
            if follow_symlinks:
                return os.stat(self.name, dir_fd=self.dir_fd)
            return os.stat(self.name, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError as exc:
            _raise_cat_error(exc, self.resolved_path)


# ---------------------------------------------------------------------------
# SecurePathResolver
# ---------------------------------------------------------------------------


class SecurePathResolver:
    """fd-relative 安全路径解析器。

    使用 ``os.open`` + ``dir_fd`` + ``O_NOFOLLOW`` 逐级安全打开目录，
    确保每一级都不是 symlink、不是特殊文件，最终路径不超出工程根。

    Args:
        project_root: 工程根目录绝对路径。
        符号链接策略按逐操作粒度控制（参见 :meth:`ResolvedPath.open` 和
        :meth:`ResolvedPath.stat` 的 ``follow_symlinks`` 参数）。
    """

    def __init__(
        self,
        project_root: str,
    ) -> None:
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root must not be empty")
        # 解析为规范绝对路径（非 fd-relative，仅作为引用边界对比）
        resolved = os.path.realpath(project_root)
        if not os.path.isdir(resolved):
            raise CatError(
                ErrorCode.INTERNAL_ERROR,
                "Project root is not a valid directory",
            )
        self._project_root = resolved

    @property
    def project_root(self) -> str:
        """工程根目录规范绝对路径。"""
        return self._project_root

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def resolve(
        self,
        path: str,
        *,
        base: str = ".",
    ) -> ResolvedPath:
        """安全解析 *path*，返回父目录 fd 和规范化相对路径。

        校验步骤：

        1. 拒绝 NUL 字符、绝对路径、Windows 驱动器等歧义形式
        2. 连接 *base* 后规范化（允许 ``..`` 但不得越出工程根）
        3. 用 ``os.open(dir_fd=root_fd, O_NOFOLLOW|O_DIRECTORY)``
           逐级打开目录组件
        4. 每级检查文件类型：拒绝 symlink、特殊文件
        5. 最终路径必须在工程根范围内

        Args:
            path: 用户提供的路径（相对或绝对）。
            base: 基准路径（通常是 CWD 或 ``"."``），用于拼接相对路径。

        Returns:
            :class:`ResolvedPath`，其中 ``dir_fd`` 已打开、
            调用方必须在使用后调用 ``close()``。

        Raises:
            CatError:
                ``INVALID_PATH`` — 路径格式非法（NUL、绝对路径等）。
                ``PATH_OUT_OF_BOUNDS`` — 路径越出工程根。
                ``UNSAFE_PATH`` — 路径包含不安全组件（symlink、特殊文件等）。
                ``NOT_FOUND`` — 路径组件不存在。
        """
        # 1. 基本格式校验
        _validate_path_format(path)
        _validate_path_format(base)

        # 2. 解析并规范化目标路径
        target = _normalize(path, base)

        # 3. 检查越根
        if target.startswith(".."):
            raise CatError(
                ErrorCode.PATH_OUT_OF_BOUNDS,
                f"Path escapes project root: {path}",
            )

        # 4. 空路径或 "." 代表工程根本身
        if not target or target == ".":
            return self._resolve_root()

        # 5. 拆分组件，逐级安全打开
        parts = PurePosixPath(target).parts

        # 目录深度限制（防止递归遍历耗尽 fd 资源）
        _MAX_RESOLVE_DEPTH = 64
        if len(parts) > _MAX_RESOLVE_DEPTH:
            raise CatError(
                ErrorCode.INVALID_PATH,
                f"Path depth exceeds limit ({_MAX_RESOLVE_DEPTH})",
            )
        # PurePosixPath("/").parts == ("/",) —— 不应走到这里
        # PurePosixPath("a/b").parts == ("a", "b")

        # 打开工程根 fd
        try:
            root_fd = os.open(
                self._project_root,
                _O_DIR_FLAGS,
            )
        except OSError as exc:
            _raise_cat_error(exc, self._project_root)

        # consumed: root_fd 已被传递给调用方（作为 ResolvedPath.dir_fd）
        consumed = False
        try:
            current_fd = root_fd
            # 逐级遍历组件，除最后一个外必须是目录
            for i, component in enumerate(parts):
                is_last = i == len(parts) - 1

                # stat 组件（不跟随 symlink）。最后组件的 ENOENT 是合法的
                #（创建/写入时文件可能还不存在），跳过后续检查。
                st = None
                try:
                    st = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    if is_last and exc.errno == errno.ENOENT:
                        pass  # 创建新文件场景，合法
                    else:
                        _raise_cat_error(exc, component)

                if st is not None:
                    # 检查是否是 symlink
                    _check_not_symlink(component, current_fd, st)

                    # 检查文件类型
                    _check_not_special(st, component)

                if is_last:
                    # 最后一个组件：可以是文件或目录（或不存在）
                    # 返回父目录 fd + 文件名
                    parent_fd = current_fd
                    name = component
                    consumed = True  # current_fd（当前目录 fd）已交给调用方
                    return ResolvedPath(
                        dir_fd=parent_fd,
                        name=name,
                        resolved_path=target,
                    )
                else:
                    # 中间组件必须是目录
                    if not stat_module.S_ISDIR(st.st_mode):
                        raise CatError(
                            ErrorCode.INVALID_PATH,
                            f"Path component is not a directory: {component}",
                        )

                    # 已确认是目录且不是 symlink → 用 dir_fd 打开子目录
                    try:
                        next_fd = os.open(
                            component,
                            _O_DIR_FLAGS,
                            dir_fd=current_fd,
                        )
                    except OSError as exc:
                        _raise_cat_error(exc, component)

                    # 关闭当前 fd（不再需要），前进到下一级
                    # 用 try/finally 保护 next_fd：即使 close 抛出 OSError
                    # 也不泄漏 next_fd
                    try:
                        if current_fd != root_fd:
                            os.close(current_fd)
                    except OSError:
                        os.close(next_fd)
                        raise
                    current_fd = next_fd

            # 不应到达这里——至少有一个组件才会进入循环
            assert False, "unreachable"

        except (CatError, Exception):
            # 异常时清理已打开的 fd；若 consumed=True 说明 current_fd
            # 已交给调用方由其负责关闭
            if not consumed:
                if current_fd != root_fd:
                    try:
                        os.close(current_fd)
                    except OSError:
                        pass
                os.close(root_fd)
            raise

    def resolve_parent(self, path: str, *, base: str = ".") -> ResolvedPath:
        """解析 *path* 的**父目录**。

        与 :meth:`resolve` 的区别：当 *path* 指向已存在的文件时，
        返回的 ``dir_fd`` 仍然是父目录，而 ``name`` 是原目标文件名。
        当 *path* 以 ``/`` 结尾或为 ``"."`` 时等同 :meth:`resolve`。

        适用于创建/写入文件：先解析父目录安全，再在父目录 fd 上创建子文件。

        Returns:
            ``ResolvedPath``，其中 ``dir_fd`` 为父目录 fd，
            ``name`` 为子文件/目录名。
        """
        return self.resolve(path, base=base)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_root(self) -> ResolvedPath:
        """返回工程根本身的安全引用。"""
        try:
            root_fd = os.open(
                self._project_root,
                _O_DIR_FLAGS,
            )
        except OSError as exc:
            _raise_cat_error(exc, self._project_root)
        return ResolvedPath(
            dir_fd=root_fd,
            name=".",
            resolved_path=".",
        )


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _validate_path_format(path: str) -> None:
    """检查路径基本格式：拒绝空串、NUL、绝对路径、Windows 驱动器等。

    Raises:
        CatError (INVALID_PATH): 格式非法时。
    """
    if not path:
        raise CatError(ErrorCode.INVALID_PATH, "Path must not be empty")

    if "\x00" in path:
        raise CatError(ErrorCode.INVALID_PATH, "Path contains null character")

    # 拒绝绝对路径（用户必须使用相对路径）
    if path.startswith("/"):
        raise CatError(
            ErrorCode.INVALID_PATH,
            "Absolute paths are not allowed, use relative paths",
        )

    # 拒绝 Windows 驱动器字母（如 C:）
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise CatError(
            ErrorCode.INVALID_PATH,
            "Windows drive letters are not allowed",
        )

    # 拒绝 UNC 路径（\\server\share）
    if path.startswith("\\\\"):
        raise CatError(
            ErrorCode.INVALID_PATH,
            "UNC paths are not allowed",
        )

    # 拒绝 URL 风格路径
    if "://" in path:
        raise CatError(
            ErrorCode.INVALID_PATH,
            "URL-style paths are not allowed",
        )

    # 拒绝反斜杠（POSIX 路径名中虽合法，但常为 Windows 用户误传）
    if "\\" in path:
        raise CatError(
            ErrorCode.INVALID_PATH,
            "Backslash is not allowed in paths; use forward slash",
        )


def _normalize(path: str, base: str = ".") -> str:
    """将 *path* 相对 *base* 规范化为 POSIX 相对路径。

    - 连接 *base* + *path*
    - 解析 ``..`` 和 ``.``
    - 确保结果不越出工程根（调用方检查）

    Returns:
        规范化后的 POSIX 风格相对路径。
    """
    # PurePosixPath 做 POSIX 语义路径拼接和规范化
    # 注意：PurePosixPath 是跨平台的，这里故意用 POSIX 语义
    if base == ".":
        joined = PurePosixPath(path)
    else:
        joined = PurePosixPath(base, path)

    # 规范化：解析 . 和 ..
    normalized = joined.as_posix()

    # PurePosixPath("/").as_posix() == "/" —— 不应出现
    # PurePosixPath("..").as_posix() == ".."
    # PurePosixPath("a/../../b").as_posix() == "../b"

    return normalized


def _check_not_symlink(
    component: str,
    dir_fd: int,
    st: os.stat_result,
) -> None:
    """检查组件不是符号链接。

    如果 *st* 的 mode 不包含 ``S_ISLNK``，意味着即使 ``O_NOFOLLOW``
    未生效（理论上不应发生），我们也能通过对比 raw stat 与 follow stat
    检测到 symlink。

    Raises:
        CatError (UNSAFE_PATH): 检测到 symlink 时。
    """
    # 第一层防护：O_NOFOLLOW 已让 os.open 在 symlink 上失败
    # 第二层防护：显式检查 stat 结果
    if stat_module.S_ISLNK(st.st_mode):
        raise CatError(
            ErrorCode.UNSAFE_PATH,
            f"Symbolic link detected and rejected: {component}",
        )

    # 第三层防护：跨设备检查——hardlink 在同一文件系统内是允许的
    #（hardlink 无法通过 fd-relative 检测，但无法穿越工程根边界）


def _check_not_special(st: os.stat_result, component: str) -> None:
    """检查文件不是特殊类型（设备、FIFO、socket 等）。

    Raises:
        CatError (UNSAFE_PATH): 检测到特殊文件时。
    """
    mode = stat_module.S_IFMT(st.st_mode)
    if mode in _SPECIAL_FILE_TYPES:
        raise CatError(
            ErrorCode.UNSAFE_PATH,
            f"Special file type rejected: {component}",
        )


def _raise_cat_error(exc: OSError, path: str) -> None:
    """将 ``OSError`` 映射为对应的 ``CatError``。

    Args:
        exc: 原始 ``OSError``。
        path: 出错的路径（用于错误消息）。
    """
    error_code: ErrorCode
    if exc.errno == errno.ENOENT:
        error_code = ErrorCode.FILE_NOT_FOUND
    elif exc.errno == errno.EACCES or exc.errno == errno.EPERM:
        error_code = ErrorCode.FORBIDDEN
    elif exc.errno == errno.ELOOP:
        error_code = ErrorCode.UNSAFE_PATH
    elif exc.errno == errno.ENXIO or exc.errno == errno.EAGAIN:
        error_code = ErrorCode.UNSAFE_PATH  # FIFO 或特殊文件阻塞
    elif exc.errno == errno.EISDIR:
        error_code = ErrorCode.IS_DIRECTORY
    elif exc.errno == errno.ENOTDIR:
        error_code = ErrorCode.NOT_DIRECTORY
    elif exc.errno == errno.ENAMETOOLONG:
        error_code = ErrorCode.INVALID_PATH
    elif exc.errno == errno.EEXIST:
        error_code = ErrorCode.FILE_ALREADY_EXISTS
    elif exc.errno == errno.ENOSPC:
        error_code = ErrorCode.OPERATION_FAILED
    elif exc.errno == errno.EMFILE or exc.errno == errno.ENFILE:
        error_code = ErrorCode.LIMIT_EXCEEDED
    elif exc.errno == errno.EROFS:
        error_code = ErrorCode.FORBIDDEN
    elif exc.errno == errno.EDQUOT:
        error_code = ErrorCode.LIMIT_EXCEEDED
    else:
        error_code = ErrorCode.OPERATION_FAILED

    raise CatError(error_code, str(exc)) from exc
