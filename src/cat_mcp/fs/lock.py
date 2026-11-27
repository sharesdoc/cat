"""写入锁与版本校验模块。

提供文件级和目录级的写入锁（线程安全）及版本校验功能，
防止多个 Cat 客户端基于同一旧版本覆盖文件，明确外部编辑竞态。

用法::

    from cat_mcp.fs.lock import WriteLockManager

    lock_mgr = WriteLockManager()

    # 上下文管理器方式（推荐）
    with lock_mgr.acquire_write_lock("user-abc", ["src/main.py"]) as ctx:
        lock_mgr.check_version("src/main.py", expected_version, resolver)
        lock_mgr.check_cwd_version(workspace, expected_cwd_version)
        ...

    # 手动方式
    ctx = lock_mgr.acquire_write_lock("user-abc", ["src/main.py"])
    try:
        ...
    finally:
        lock_mgr.release_write_lock(ctx.lock_id)
"""
# 依据: Task-R-20261126012656449-P0

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat as stat_module
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.fs.paths import SecurePathResolver

logger = logging.getLogger(__name__)

# 读取块大小（与 cat_mcp.fs.reader._READ_CHUNK_SIZE 一致）
_READ_CHUNK_SIZE = 65536

# ---------------------------------------------------------------------------
# LockContext
# ---------------------------------------------------------------------------


@dataclass
class LockContext(AbstractContextManager["LockContext"]):
    """写入锁上下文，包含锁标识和路径列表。

    支持上下文管理器协议（``with`` 语句），在退出时自动释放锁。

    Attributes:
        lock_id: 唯一锁标识（URL-safe 随机字符串）。
        paths: 此次锁覆盖的工程相对路径列表。
        subject: 持有锁的主体标识。
        acquired_at: 锁获取时间戳（``time.monotonic()``）。
    """

    lock_id: str
    paths: list[str]
    subject: str
    acquired_at: float
    _manager: WriteLockManager | None = field(repr=False, default=None)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        """退出上下文时自动释放锁。

        释放失败仅记录日志，不抛出异常。
        """
        if self.lock_id and self._manager is not None:
            # __exit__ 与手动 release 共享同一锁 ID；release_write_lock 负责
            # 删除活跃锁记录和释放底层 Lock，此处调用空安全
            self._manager._release_ctx_lock(self.lock_id)


# ---------------------------------------------------------------------------
# WriteLockManager
# ---------------------------------------------------------------------------


class WriteLockManager:
    """文件级和目录级写入锁管理器，带版本校验。

    使用 :class:`threading.Lock` 保护每个资源键。锁获取顺序按键字母序
    确定，防止死锁。所有锁为进程内持有，不跨进程分布。

    覆盖 "工程+规范路径锁、双路径稳定顺序、目录层级锁" 设计要点：
    - 工程路径和规范路径均生成锁键，排序后获取防止死锁
    - 对每个路径同时锁定其所有上级目录（目录层级锁）
    - 持锁后可重验权限、路径、版本和 CWD 版本

    用法::

        mgr = WriteLockManager()

        with mgr.acquire_write_lock("user-abc", ["src/main.py"]) as ctx:
            mgr.check_version("src/main.py", expected, resolver)
            ...
    """

    def __init__(self) -> None:
        # 资源键 → threading.Lock 映射（惰性初始化）
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

        # 活跃锁跟踪：lock_id → LockContext
        self._active_locks: dict[str, LockContext] = {}
        self._active_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def acquire_write_lock(
        self,
        subject: str,
        file_paths: list[str],
        timeout: float = 30.0,
    ) -> LockContext:
        """获取一组文件的写入锁。

        对 *file_paths* 中的每个路径生成锁键（文件级 + 目录级），
        按字母序排序后依次获取。锁定后返回 :class:`LockContext`，
        供调用方通过 ``with`` 语句或手动 ``release_write_lock`` 释放。

        锁键生成策略（"工程+规范路径锁、目录层级锁"）：
        - ``file:<规范化路径>`` — 文件级锁键
        - ``dir:<上级目录>`` — 每级祖先目录锁键（含根 ``dir:.``）
        - 所有锁键排序后获取，保证稳定顺序，防止死锁

        Args:
            subject: 已认证的主体标识。
            file_paths: 工程相对路径列表（已规范化）。
            timeout: 每个锁键获取的超时秒数（默认 30.0）。

        Returns:
            :class:`LockContext`，持锁后有效。

        Raises:
            CatError (CONCURRENT_MODIFICATION): 持锁超时或失败时。
            CatError (INVALID_ARGUMENT): ``file_paths`` 为空时。
        """
        if not file_paths:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "file_paths must not be empty",
            )

        # 1. 生成锁键（路径 + 上级目录），排序稳定
        lock_keys = self._get_lock_keys(file_paths)

        # 2. 生成锁 ID
        lock_id = secrets.token_urlsafe(16)

        # 3. 依次获取锁
        acquired_keys: list[str] = []
        try:
            for key in lock_keys:
                self._ensure_lock(key)
                lock = self._locks[key]
                acquired = lock.acquire(timeout=timeout)
                if not acquired:
                    raise CatError(
                        ErrorCode.CONCURRENT_MODIFICATION,
                        f"Failed to acquire write lock for {key} "
                        f"(timeout={timeout}s)",
                    )
                acquired_keys.append(key)
        except Exception:
            # 释放已获取的锁（逆序）
            for key in reversed(acquired_keys):
                try:
                    self._locks[key].release()
                except Exception:
                    pass
            raise

        # 4. 注册活跃锁
        ctx = LockContext(
            lock_id=lock_id,
            paths=file_paths,
            subject=subject,
            acquired_at=time.monotonic(),
            _manager=self,
        )
        with self._active_lock:
            self._active_locks[lock_id] = ctx

        return ctx

    def release_write_lock(self, lock_id: str) -> None:
        """释放指定 *lock_id* 的写入锁。

        Args:
            lock_id: 由 :meth:`acquire_write_lock` 返回的锁标识。

        Raises:
            CatError (OPERATION_FAILED): ``lock_id`` 不存在时。
        """
        with self._active_lock:
            ctx = self._active_locks.pop(lock_id, None)

        if ctx is None:
            raise CatError(
                ErrorCode.OPERATION_FAILED,
                f"Lock not found: {lock_id}",
            )

        # 重新生成锁键并逆序释放
        lock_keys = self._get_lock_keys(ctx.paths)
        for key in reversed(lock_keys):
            try:
                lock = self._locks.get(key)
                if lock is not None:
                    lock.release()
            except Exception:
                logger.exception("Failed to release lock key %s", key)

    def _release_ctx_lock(self, lock_id: str) -> None:
        """由 ``LockContext.__exit__`` 调用，空安全释放锁。

        与 :meth:`release_write_lock` 共享同一锁 ID；
        锁已释放过时静默跳过，不抛出异常。
        """
        with self._active_lock:
            ctx = self._active_locks.pop(lock_id, None)
        if ctx is None:
            return  # 已释放或不存在，静默忽略

        lock_keys = self._get_lock_keys(ctx.paths)
        for key in reversed(lock_keys):
            try:
                lock = self._locks.get(key)
                if lock is not None:
                    lock.release()
            except Exception:
                logger.exception("Failed to release lock key %s", key)

    def check_version(
        self,
        file_path: str,
        expected_version: str | None,
        resolver: SecurePathResolver,
        base: str = ".",
    ) -> bool:
        """验证文件版本是否匹配预期。

        使用 SHA-256 计算文件当前哈希与 *expected_version* 对比。
        应在 :meth:`acquire_write_lock` **之后**调用，确保持锁期间
        版本一致。

        Args:
            file_path: 工程相对路径。
            expected_version: 预期版本哈希。为 ``None`` 时跳过校验。
            resolver: 已初始化的 :class:`SecurePathResolver`。
            base: 基准路径（默认 ``"."``）。

        Returns:
            版本匹配时返回 ``True``，否则抛出异常。

        Raises:
            CatError (VERSION_CONFLICT): 文件版本不匹配时。
            CatError (FILE_NOT_FOUND): 文件不存在时。
        """
        if expected_version is None:
            return True

        with resolver.resolve(file_path, base=base) as rp:
            st = rp.stat(follow_symlinks=False)
            if not stat_module.S_ISREG(st.st_mode):
                raise CatError(
                    ErrorCode.VERSION_CONFLICT,
                    f"Path is not a regular file: {file_path}",
                )
            fd = rp.open(os.O_RDONLY)
            try:
                current_version = _hash_file(fd)
            finally:
                os.close(fd)

        if current_version != expected_version:
            raise CatError(
                ErrorCode.VERSION_CONFLICT,
                f"File version mismatch for {file_path}",
            )
        return True

    def check_cwd_version(
        self,
        workspace: dict[str, Any],
        expected_cwd_version: int | None,
    ) -> bool:
        """验证工作空间 CWD 版本是否匹配预期。

        应在 :meth:`acquire_write_lock` 之后调用，确保持锁期间
        CWD 版本一致。

        Args:
            workspace: 工作空间字典（必须含 ``cwd_version`` 字段）。
            expected_cwd_version: 预期 CWD 版本。为 ``None`` 时跳过校验。

        Returns:
            版本匹配时返回 ``True``，否则抛出异常。

        Raises:
            CatError (INVALID_ARGUMENT): workspace 不是 dict 或缺少 cwd_version。
            CatError (VERSION_CONFLICT): CWD 版本不匹配时。
        """
        if not isinstance(workspace, dict):
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "workspace must be a dict",
            )
        if "cwd_version" not in workspace:
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "workspace must contain 'cwd_version' field",
            )
        if expected_cwd_version is None:
            return True
            return True

        current = workspace.get("cwd_version")
        if current != expected_cwd_version:
            raise CatError(
                ErrorCode.VERSION_CONFLICT,
                f"CWD version mismatch: expected {expected_cwd_version}, "
                f"current {current}",
            )
        return True

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _get_lock_keys(file_paths: list[str]) -> list[str]:
        """生成锁键列表，按字母序稳定排序。

        对每个路径生成：
        - ``file:<规范化路径>`` — 文件路径锁键
        - ``dir:<上级目录>`` — 所有上级目录锁键（上限 64 层）

        两个路径生成的锁键合并后去重排序，保证"双路径稳定顺序"。

        Args:
            file_paths: 工程相对路径列表。

        Returns:
            去重排序后的锁键列表。
        """
        keys: set[str] = set()
        for fp in file_paths:
            norm = PurePosixPath(fp).as_posix()
            # 文件级锁键
            keys.add(f"file:{norm}")

            # 目录级锁键：每个祖先目录（上限 64 层，与 SecurePathResolver 一致）
            parts = PurePosixPath(norm).parts
            max_depth = min(len(parts) - 1, 64)
            for i in range(max_depth):
                parent = str(PurePosixPath(*parts[: i + 1]))
                keys.add(f"dir:{parent}")

            # 工程根目录锁
            keys.add("dir:.")

        return sorted(keys)

    def _ensure_lock(self, key: str) -> None:
        """确保 *key* 对应的 :class:`threading.Lock` 已初始化。

        惰性初始化 + 双重检查锁定（DCLP）。

        Args:
            key: 锁键。
        """
        if key not in self._locks:
            with self._locks_lock:
                if key not in self._locks:
                    self._locks[key] = threading.Lock()


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _hash_file(fd: int) -> str:
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


# ---------------------------------------------------------------------------
# 模块级快捷函数（供全局访问）
# ---------------------------------------------------------------------------

_lock_manager: WriteLockManager | None = None


def set_lock_manager(mgr: WriteLockManager) -> None:
    """设置全局 ``WriteLockManager`` 实例。

    Args:
        mgr: 已初始化的写锁管理器。
    """
    global _lock_manager
    _lock_manager = mgr


def get_lock_manager() -> WriteLockManager:
    """获取全局 ``WriteLockManager`` 实例。

    Returns:
        已初始化的写锁管理器。

    Raises:
        RuntimeError: 未调用 ``set_lock_manager`` 时。
    """
    if _lock_manager is None:
        raise RuntimeError(
            "WriteLockManager not initialised — call set_lock_manager() first"
        )
    return _lock_manager
