"""原子文件提交与崩溃恢复模块。

实现"写入不会产生半个文件"的语义：
1. 写入同目录独占临时文件（.cat_partial）
2. fsync 临时文件 + fsync 父目录
3. os.rename（原子替换目标文件）
4. fsync 父目录（确保 rename 的目录项持久化）

Crash recovery:
- 启动时扫描项目根目录遗留 .cat_partial 文件
- 匹配 operations 表判断状态
- PREPARED → 检查临时文件哈希，匹配则 ABORTED（清理临时文件）
- COMMITTED → 清理临时文件
- IN_DOUBT → 冻结（不改动，记录日志由管理员处置）

备份机制：
- 覆盖前将旧文件内容备份至 .cat_backup 目录
- 备份限额控制（最大文件数／总字节数）
"""
# 依据: Task-R-20261126012656450-P0

from __future__ import annotations

import enum
import errno
import hashlib
import logging
import os
import secrets
import shutil
import stat as stat_module
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.fs.paths import SecurePathResolver, ResolvedPath
from cat_mcp.fs.lock import WriteLockManager
from cat_mcp.state import StateManager

logger = logging.getLogger(__name__)

# 读取块大小（与 cat_mcp.fs.reader._READ_CHUNK_SIZE 一致）
_READ_CHUNK_SIZE = 65536

# 备份子目录名
_BACKUP_DIR_NAME = ".cat_backup"

# 备份默认限额
_DEFAULT_BACKUP_MAX_FILES = 1000
_DEFAULT_BACKUP_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB

# ---------------------------------------------------------------------------
# CommitStatus
# ---------------------------------------------------------------------------


class CommitStatus(enum.Enum):
    """原子提交操作的生命周期状态。"""

    PREPARED = "prepared"
    COMMITTED = "committed"
    ABORTED = "aborted"
    IN_DOUBT = "in_doubt"


# ---------------------------------------------------------------------------
# CommitContext
# ---------------------------------------------------------------------------


@dataclass
class CommitContext:
    """原子提交上下文，记录一次写入操作的事务状态。

    Attributes:
        operation_id: 数据库 operations 表的主键 ID。
        target_path: 目标文件工程相对路径。
        temp_path: 临时文件绝对路径。
        backup_path: 旧文件备份路径（首次覆盖时设置），None 表示新建文件。
        status: 当前提交状态。
        file_hash: 写入内容的 SHA-256 哈希。
    """

    operation_id: str
    target_path: str
    temp_path: str
    backup_path: str | None
    status: CommitStatus
    file_hash: str | None
    _lock_ctx: Any | None = field(repr=False, default=None)


# ---------------------------------------------------------------------------
# AtomicWriter
# ---------------------------------------------------------------------------


class AtomicWriter:
    """原子文件写入与崩溃恢复管理器。

    提供两阶段提交语义：prepare（写入临时文件 + 记录日志）→
    commit（原子 rename + 状态持久化）或 abort（清理）。

    Crash recovery 流程：
    1. 扫描项目根下所有 ``.cat_partial`` 遗留临时文件
    2. 对每个文件，提取目标路径 → 查询 operations 表
    3. 按状态执行恢复策略（清理 / 冻结）

    Args:
        project_root: 工程根目录绝对路径。
        state_manager: 已初始化的 :class:`StateManager` 实例。
        resolver: 已初始化的 :class:`SecurePathResolver` 实例。
        lock_manager: 已初始化的 :class:`WriteLockManager` 实例。
        backup_max_files: 备份目录最大文件数（默认 1000）。
        backup_max_bytes: 备份目录总字节数上限（默认 100 MiB）。
    """

    def __init__(
        self,
        project_root: str,
        state_manager: StateManager,
        resolver: SecurePathResolver,
        lock_manager: WriteLockManager,
        backup_max_files: int = _DEFAULT_BACKUP_MAX_FILES,
        backup_max_bytes: int = _DEFAULT_BACKUP_MAX_BYTES,
    ) -> None:
        self._project_root = os.path.realpath(project_root)
        self._state = state_manager
        self._resolver = resolver
        self._lock_mgr = lock_manager
        self._backup_max_files = backup_max_files
        self._backup_max_bytes = backup_max_bytes

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def prepare_write(
        self,
        file_path: str,
        content: bytes,
        *,
        workspace: dict[str, Any] | None = None,
        request_id: str | None = None,
        subject: str | None = None,
    ) -> CommitContext:
        """准备一次原子写入：创建操作记录 + 写入临时文件。

        流程：
        1. 通过 WriteLockManager 获取文件写入锁
        2. 使用 ``resolver`` 解析目标路径的安全父目录 fd
        3. 在父目录 fd 上用 ``O_CREAT|O_EXCL`` 创建唯一临时文件
        4. 写入 *content* 并 ``fsync``
        5. 计算内容 SHA-256 哈希
        6. 在 ``operations`` 表中创建 PREPARED 状态记录
        7. 如果目标文件已存在，备份旧文件至 ``.cat_backup``

        Args:
            file_path: 目标文件工程相对路径（如 ``"src/main.py"``）。
            content: 要写入的字节内容。
            workspace: 工作空间字典（含 ``id``, ``subject`` 等）。
            request_id: 请求标识（用于关联操作记录）。
            subject: 主体标识（未通过 workspace 提供时使用）。

        Returns:
            :class:`CommitContext`，包含临时文件路径和操作 ID。

        Raises:
            CatError: 路径不安全、权限不足、磁盘空间不足等。
        """
        # 0. 获取文件写入锁
        subj: str = subject or ""
        ws_id: str = ""
        req_id: str = request_id or ""
        if workspace:
            ws_id = workspace.get("id", "")
            if not subj:
                subj = workspace.get("subject", "")
            if not req_id:
                req_id = workspace.get("request_id", "")

        lock_ctx = self._lock_mgr.acquire_write_lock(subj, [file_path], timeout=30.0)

        # 计算内容哈希（内容已知，可提前计算）
        file_hash = hashlib.sha256(content).hexdigest()

        # 获取文件基名（用于 request_id，与 recover 的 _extract_target_path 一致）
        with self._resolver.resolve(file_path) as rp_name:
            file_basename = rp_name.name

        # 1. 解析目标路径
        with self._resolver.resolve(file_path) as rp:
            dir_fd = rp.dir_fd
            target_name = rp.name
            resolved_path = rp.resolved_path

            # 2. 检查目标是否已存在（用于备份）
            target_exists = False
            existing_hash: str | None = None
            try:
                st = os.stat(target_name, dir_fd=dir_fd, follow_symlinks=False)
                if stat_module.S_ISREG(st.st_mode):
                    target_exists = True
                    old_fd = os.open(
                        target_name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=dir_fd,
                    )
                    try:
                        existing_hash = _hash_fd(old_fd)
                    finally:
                        os.close(old_fd)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED,
                        f"Cannot stat target file: {exc}",
                    )

            # 3. 创建独占临时文件
            temp_name = _make_temp_name(target_name)
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o644,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    temp_name = _make_temp_name(target_name)
                    temp_fd = os.open(
                        temp_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                        0o644,
                        dir_fd=dir_fd,
                    )
                else:
                    raise CatError(
                        ErrorCode.OPERATION_FAILED,
                        f"Cannot create temp file in {resolved_path}: {exc}",
                    )

            # 4. 写入内容并 fsync
            try:
                written = 0
                while written < len(content):
                    n = os.write(temp_fd, content[written:])
                    if n == 0:
                        raise CatError(
                            ErrorCode.OPERATION_FAILED,
                            "Short write to temp file",
                        )
                    written += n
                os.fsync(temp_fd)
            except OSError as exc:
                os.close(temp_fd)
                _cleanup_temp(dir_fd, temp_name)
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Write to temp file failed: {exc}",
                )

            os.close(temp_fd)

            # 5. 暂存 temp 绝对路径
            parent_dir = PurePosixPath(resolved_path).parent
            temp_abs = os.path.normpath(
                os.path.join(self._project_root, parent_dir.as_posix(), temp_name)
            )

            # 6. 备份旧文件（在 dir_fd 有效时执行）
            backup_path: str | None = None
            if target_exists:
                backup_path = self._backup_file(
                    file_path, dir_fd=dir_fd, target_name=target_name,
                )

        # 7. 创建数据库操作记录
        op = self._state.create_operation(
            subject=subj,
            workspace_id=ws_id,
            request_id=req_id or file_basename,
            path=file_path,
            input_digest=file_hash,
        )
        operation_id = op["id"] if op else ""

        return CommitContext(
            operation_id=operation_id,
            target_path=file_path,
            temp_path=temp_abs,
            backup_path=backup_path,
            status=CommitStatus.PREPARED,
            file_hash=file_hash,
            _lock_ctx=lock_ctx,
        )

    def commit_write(self, ctx: CommitContext) -> None:
        """原子提交：rename 临时文件到目标文件 + 持久化状态。

        流程：
        1. ``os.rename``（同文件系统原子操作）替换目标文件
        2. ``fsync`` 父目录确保目录项持久化
        3. 更新 ``operations`` 表状态为 COMMITTED

        Args:
            ctx: 由 :meth:`prepare_write` 返回的提交上下文。

        Raises:
            CatError: 上下文状态不是 PREPARED、Rename 失败等。
        """
        if ctx.status != CommitStatus.PREPARED:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Cannot commit operation in status {ctx.status.value}",
            )

        # 1. 解析目标路径获取 dir_fd
        with self._resolver.resolve(ctx.target_path) as rp:
            # 2. 构建临时文件和目标文件的完整路径（在 dir_fd 所在目录下）
            temp_basename = os.path.basename(ctx.temp_path)
            target_basename = rp.name

            # 3. os.rename 原子替换
            #    在 macOS/Linux 上，rename(2) 是原子操作（同文件系统）
            try:
                os.rename(temp_basename, target_basename, src_dir_fd=rp.dir_fd, dst_dir_fd=rp.dir_fd)
            except OSError as exc:
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Atomic rename failed: {exc}",
                )

            # 4. fsync 父目录确保目录项持久化
            try:
                os.fsync(rp.dir_fd)
            except OSError as exc:
                # fsync 失败不阻断流程，但记录告警
                logger.warning(
                    "Parent directory fsync failed for %s: %s",
                    ctx.target_path,
                    exc,
                )

        # 5. 更新操作状态
        ctx.status = CommitStatus.COMMITTED
        if ctx.operation_id:
            self._state.update_operation_status(ctx.operation_id, "committed")

        # 6. 释放写入锁
        if ctx._lock_ctx is not None:
            self._lock_mgr.release_write_lock(ctx._lock_ctx.lock_id)
            ctx._lock_ctx = None

    def abort_write(self, ctx: CommitContext) -> None:
        """中止写入：清理临时文件 + 更新状态为 ABORTED。

        Args:
            ctx: 由 :meth:`prepare_write` 返回的提交上下文。
        """
        if ctx.status == CommitStatus.COMMITTED:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                "Cannot abort a committed operation",
            )

        # 1. 清理临时文件
        temp_basename = os.path.basename(ctx.temp_path)
        try:
            # 安全解析目标路径获取 dir_fd
            with self._resolver.resolve(ctx.target_path) as rp:
                try:
                    os.unlink(temp_basename, dir_fd=rp.dir_fd)
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        logger.warning(
                            "Failed to remove temp file %s: %s",
                            ctx.temp_path,
                            exc,
                        )
        except CatError:
            # 目录结构可能已被删除，通过绝对路径尝试
            _cleanup_temp_path(ctx.temp_path)

        # 2. 更新操作状态
        ctx.status = CommitStatus.ABORTED
        if ctx.operation_id:
            self._state.update_operation_status(ctx.operation_id, "failed")

        # 3. 释放写入锁
        if ctx._lock_ctx is not None:
            self._lock_mgr.release_write_lock(ctx._lock_ctx.lock_id)
            ctx._lock_ctx = None

    def recover(self, startup: bool = True) -> list[dict[str, Any]]:
        """崩溃恢复：扫描遗留临时文件，按哈希和操作记录恢复。

        对每个符合条件的 ``.cat_partial`` 文件：

        - **无对应操作记录** → 视为孤儿临时文件，直接清理（ABORTED）
        - **操作状态 PREPARED** → 哈希匹配则 ABORTED（清理），否则 IN_DOUBT（冻结）
        - **操作状态 COMMITTED** → 清理临时文件
        - **操作状态 failed** → 清理临时文件
        - **无匹配 + 哈希无法验证** → IN_DOUBT（冻结）

        冻结的路径记录在返回列表中，需管理员手动处置。

        Args:
            startup: 是否在启动时调用。为 ``True`` 时扫描整个工程根；
                    为 ``False`` 时仅扫描已知路径（保留供增量使用）。

        Returns:
            冻结的路径列表（``IN_DOUBT`` 状态的条目）。
            每项为 ``{"path": str, "reason": str}``。
        """
        frozen: list[dict[str, Any]] = []

        if not startup:
            return frozen

        # 1. 扫描工程根下所有 .cat_partial 文件
        partial_files = _find_partial_files(self._project_root)
        if not partial_files:
            return frozen

        logger.info(
            "Crash recovery: found %d partial temp file(s)",
            len(partial_files),
        )

        # 2. 逐一处理
        for abs_path, rel_dir, basename in partial_files:
            target_rel = _extract_target_path(basename)
            if not target_rel:
                # 无法提取目标路径，强制冻结
                frozen.append({
                    "path": abs_path,
                    "reason": "Cannot extract target path from temp filename",
                })
                continue

            # 查询是否有匹配的操作记录
            ops = self._state.get_operation(target_rel)
            if ops is None:
                # 无操作记录 → 孤儿临时文件，直接清理
                logger.info("Recovery: cleaning orphan temp file %s", abs_path)
                _cleanup_temp_path(abs_path)
                continue

            op_status = ops.get("status", "")

            if op_status == "committed":
                # 已提交：清理临时文件
                logger.info("Recovery: cleanup temp for committed op %s", abs_path)
                _cleanup_temp_path(abs_path)

            elif op_status == "prepared":
                # 尝试计算临时文件哈希与操作记录比对
                temp_hash = _hash_file_at_path(abs_path)
                op_hash = ops.get("input_digest")
                if temp_hash is not None and op_hash is not None and temp_hash == op_hash:
                    # 哈希匹配 → 写入已完整，但未提交 → 安全清理（ABORTED）
                    logger.info(
                        "Recovery: hash match for prepared op %s, cleaning temp",
                        abs_path,
                    )
                    _cleanup_temp_path(abs_path)
                else:
                    # 哈希不匹配或无法计算 → IN_DOUBT（冻结）
                    frozen.append({
                        "path": abs_path,
                        "reason": (
                            f"Prepared operation {ops.get('id', '?')} "
                            f"hash mismatch or unreadable; target={target_rel}"
                        ),
                    })
                    logger.warning(
                        "Recovery: IN_DOUBT temp file %s for op %s",
                        abs_path,
                        ops.get("id", "?"),
                    )

            elif op_status == "failed":
                # 已标记失败 → 清理临时文件
                logger.info("Recovery: cleanup temp for failed op %s", abs_path)
                _cleanup_temp_path(abs_path)

            else:
                # 未知状态 → 冻结
                frozen.append({
                    "path": abs_path,
                    "reason": f"Unknown operation status '{op_status}'",
                })

        if frozen:
            logger.warning(
                "Crash recovery: %d path(s) frozen in IN_DOUBT state",
                len(frozen),
            )

        return frozen

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _backup_file(
        self,
        file_path: str,
        *,
        dir_fd: int,
        target_name: str,
    ) -> str | None:
        """备份目标文件旧内容至 ``.cat_backup`` 目录。

        在目标文件父目录下创建 ``.cat_backup/`` 子目录，
        将旧文件复制过去，并附带时间戳和随机后缀防止冲突。

        Returns:
            备份文件绝对路径，或备份失败时的 ``None``。
        """
        backup_dir = os.path.join(self._project_root, _BACKUP_DIR_NAME)

        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError:
            logger.warning("Cannot create backup dir %s", backup_dir)
            return None

        # 备份文件名：<原文件名>.<时间戳>.<随机>.bak
        timestamp = time.strftime("%Y%m%d%H%M%S")
        rand = secrets.token_hex(4)
        safe_name = PurePosixPath(file_path).name
        backup_name = f"{safe_name}.{timestamp}.{rand}.bak"
        backup_path = os.path.join(backup_dir, backup_name)

        try:
            # 使用 dir_fd 读取原文件
            src_fd = os.open(
                target_name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=dir_fd,
            )
            try:
                with open(backup_path, "wb") as dst_f:
                    while True:
                        chunk = os.read(src_fd, _READ_CHUNK_SIZE)
                        if not chunk:
                            break
                        dst_f.write(chunk)
                    dst_f.flush()
                    os.fsync(dst_f.fileno())
            finally:
                os.close(src_fd)

            # 备份后执行配额清理
            self._enforce_backup_quota(backup_dir)

            return backup_path
        except OSError as exc:
            logger.warning("Backup failed for %s: %s", file_path, exc)
            return None

    def _enforce_backup_quota(self, backup_dir: str) -> None:
        """清理超额备份文件，按 FIFO 顺序删除最旧的。

        Args:
            backup_dir: 备份目录绝对路径。
        """
        try:
            entries: list[tuple[str, float, int]] = []
            total_bytes = 0
            for name in os.listdir(backup_dir):
                full = os.path.join(backup_dir, name)
                try:
                    st = os.stat(full)
                    if not stat_module.S_ISREG(st.st_mode):
                        continue
                    entries.append((full, st.st_mtime, st.st_size))
                    total_bytes += st.st_size
                except OSError:
                    continue

            # 按 mtime 升序排序（最旧的在前）
            entries.sort(key=lambda e: e[1])

            # 按文件数超限删除
            while len(entries) > self._backup_max_files:
                oldest = entries.pop(0)
                try:
                    os.unlink(oldest[0])
                    total_bytes -= oldest[2]
                except OSError:
                    continue

            # 按总字节数超限删除
            while total_bytes > self._backup_max_bytes and entries:
                oldest = entries.pop(0)
                try:
                    sz = oldest[2]
                    os.unlink(oldest[0])
                    total_bytes -= sz
                except OSError:
                    continue
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 全局 AtomicWriter 注册表（按 project_root 索引）
# ---------------------------------------------------------------------------

_atomic_writers: dict[str, AtomicWriter] = {}


def set_atomic_writer(project_root: str, writer: AtomicWriter) -> None:
    """注册一个工程根对应的 ``AtomicWriter`` 实例。

    Args:
        project_root: 工程根目录规范绝对路径。
        writer: 已初始化的原子写入管理器。
    """
    _atomic_writers[project_root] = writer


def get_atomic_writer(project_root: str) -> AtomicWriter:
    """获取指定工程根的 ``AtomicWriter`` 实例。

    Args:
        project_root: 工程根目录规范绝对路径。

    Returns:
        已初始化的原子写入管理器。

    Raises:
        RuntimeError: 未注册时。
    """
    writer = _atomic_writers.get(project_root)
    if writer is None:
        raise RuntimeError(
            f"AtomicWriter not initialised for project root {project_root!r}"
        )
    return writer


def get_all_atomic_writers() -> list[AtomicWriter]:
    """返回所有已注册的 ``AtomicWriter`` 实例列表。"""
    return list(_atomic_writers.values())


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _make_temp_name(target_basename: str) -> str:
    """生成唯一的临时文件名。

    格式: ``<目标文件名>.<PID>.<随机16字节>.cat_partial``

    Args:
        target_basename: 目标文件基本名。

    Returns:
        临时文件基本名（不含目录）。
    """
    pid = os.getpid()
    rand = secrets.token_hex(8)  # 16 字节 hex → 安全随机
    return f"{target_basename}.{pid}.{rand}.cat_partial"


def _extract_target_path(temp_basename: str) -> str | None:
    """从临时文件名中提取目标路径。

    格式: ``<目标文件基本名>.<PID>.<随机>.cat_partial``

    Args:
        temp_basename: 临时文件基本名。

    Returns:
        目标路径（基本名部分），无法提取时返回 ``None``。
    """
    # 移除 .cat_partial 后缀
    if not temp_basename.endswith(".cat_partial"):
        return None
    no_suffix = temp_basename[: -len(".cat_partial")]

    # 移除最后两个点分隔的部分（PID 和随机串）
    parts = no_suffix.rsplit(".", 2)
    if len(parts) < 3:
        return None
    # 第一部分是目标文件基本名
    target = parts[0]
    return target if target else None


def _find_partial_files(project_root: str) -> list[tuple[str, str, str]]:
    """递归扫描工程根下所有 ``.cat_partial`` 文件。

    Args:
        project_root: 工程根绝对路径。

    Returns:
        ``(绝对路径, 相对于工程根的目录, 文件名)`` 列表。
    """
    results: list[tuple[str, str, str]] = []
    try:
        for dirpath, _dirnames, filenames in os.walk(project_root):
            for fn in filenames:
                if fn.endswith(".cat_partial"):
                    abs_path = os.path.join(dirpath, fn)
                    rel_dir = os.path.relpath(dirpath, project_root)
                    results.append((abs_path, rel_dir, fn))
    except OSError as exc:
        logger.error("Error scanning for partial files: %s", exc)
    return results


def _hash_fd(fd: int) -> str:
    """读取文件描述符 *fd* 的全部内容并返回 SHA-256 哈希。

    Args:
        fd: 已打开的文件描述符（只读）。

    Returns:
        SHA-256 十六进制字符串（64 字符）。
    """
    hasher = hashlib.sha256()
    while True:
        chunk = os.read(fd, _READ_CHUNK_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def _hash_file_at_path(path: str) -> str | None:
    """计算文件 *path* 的 SHA-256 哈希。

    Args:
        path: 文件绝对路径。

    Returns:
        SHA-256 十六进制字符串，读取失败时返回 ``None``。
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _cleanup_temp(dir_fd: int, temp_name: str) -> None:
    """在指定目录 fd 上安全删除临时文件。

    Args:
        dir_fd: 父目录文件描述符。
        temp_name: 临时文件基本名。
    """
    try:
        os.unlink(temp_name, dir_fd=dir_fd)
    except OSError:
        pass


def _cleanup_temp_path(abs_path: str) -> None:
    """通过绝对路径安全删除临时文件。

    Args:
        abs_path: 临时文件绝对路径。
    """
    try:
        os.unlink(abs_path)
    except OSError:
        pass
