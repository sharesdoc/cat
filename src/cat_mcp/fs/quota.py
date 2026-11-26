"""统一配额和有界工具执行管理器。

防止请求、并发、大文件和搜索耗尽服务器或上下文。

提供:

- :class:`QuotaManager` — 统一入口，封装并发、搜索、大小、超时配额
- :func:`check_response_size` — 检查响应是否超限
- :func:`bounded_execute` — 有界线程执行器（支持超时和取消）

用法::

    from cat_mcp.fs.quota import QuotaManager, check_response_size

    mgr = QuotaManager(config.limits)
    mgr.acquire_concurrency("user-abc")
    try:
        result = await mgr.run_with_timeout(10.0, some_async_fn)
    finally:
        mgr.release_concurrency("user-abc")

    check_response_size(data, max_bytes=65536)
"""
# 依据: Task-R-20261126012656460-P0

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from cat_mcp.config import LimitsConfig
from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# QuotaManager
# ---------------------------------------------------------------------------


class QuotaManager:
    """统一配额管理器。

    管理以下维度的资源使用：

    - **并发配额**：每主体并发操作数上限
    - **搜索超时**：搜索操作超时时间
    - **搜索结果**：搜索结果上限
    - **响应大小**：单次工具响应最大字节数

    Args:
        limits: 限额配置（来自 :class:`LimitsConfig`）。
    """

    def __init__(self, limits: LimitsConfig) -> None:
        self._limits = limits

        # 并发配额跟踪：subject → 当前活跃操作数
        self._concurrency: dict[str, int] = {}
        self._concurrency_lock = threading.Lock()

        # 有界线程池
        self._thread_pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # 并发配额
    # ------------------------------------------------------------------

    def acquire_concurrency(self, subject: str) -> bool:
        """尝试为主体 *subject* 获取一个并发槽位。

        如果当前活跃数已达上限（
        :attr:`LimitsConfig.max_concurrent_per_subject`），返回
        ``False``，调用方应拒绝请求。

        Args:
            subject: 已认证的主体标识。

        Returns:
            ``True`` 当成功获取槽位，否则 ``False``。
        """
        max_conc = self._limits.max_concurrent_per_subject
        with self._concurrency_lock:
            current = self._concurrency.get(subject, 0)
            if current >= max_conc:
                return False
            self._concurrency[subject] = current + 1
            return True

    def release_concurrency(self, subject: str) -> None:
        """释放主体 *subject* 的一个并发槽位。

        Args:
            subject: 已认证的主体标识。
        """
        with self._concurrency_lock:
            current = self._concurrency.get(subject, 0)
            if current <= 1:
                self._concurrency.pop(subject, None)
            else:
                self._concurrency[subject] = current - 1

    def get_concurrency(self, subject: str) -> int:
        """返回主体 *subject* 的当前活跃并发数。

        Args:
            subject: 已认证的主体标识。

        Returns:
            当前活跃操作数。
        """
        with self._concurrency_lock:
            return self._concurrency.get(subject, 0)

    # ------------------------------------------------------------------
    # 有界线程池
    # ------------------------------------------------------------------

    @property
    def thread_pool(self) -> ThreadPoolExecutor:
        """获取或创建有界线程池。

        线程池大小设为 ``max_concurrent_per_subject * 2``，
        确保不会因单主体并发限制形成死锁。
        """
        if self._thread_pool is None:
            max_workers = self._limits.max_concurrent_per_subject * 2
            self._thread_pool = ThreadPoolExecutor(
                max_workers=max(max_workers, 2),
                thread_name_prefix="cat-worker",
            )
        return self._thread_pool

    def shutdown(self, wait: bool = True) -> None:
        """关闭线程池。

        Args:
            wait: 是否等待正在执行的任务完成。
        """
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=wait)
            self._thread_pool = None

    def run_sync(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """在线程池中同步执行 *fn*，支持超时。

        Args:
            fn: 要执行的函数。
            timeout: 超时秒数（``None`` 表示不限时）。
            *args: 传递给 *fn* 的位置参数。
            **kwargs: 传递给 *fn* 的关键字参数。

        Returns:
            *fn* 的返回值。

        Raises:
            CatError (OPERATION_TIMEOUT): 执行超时。
        """
        future = self.thread_pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            raise CatError(
                ErrorCode.OPERATION_TIMEOUT,
                f"Operation timed out after {timeout}s",
            )

    async def run_async(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """在线程池中异步执行 *fn*，支持超时。

        Args:
            fn: 要执行的函数。
            timeout: 超时秒数（``None`` 表示不限时）。
            *args: 传递给 *fn* 的位置参数。
            **kwargs: 传递给 *fn* 的关键字参数。

        Returns:
            *fn* 的返回值。

        Raises:
            CatError (OPERATION_TIMEOUT): 执行超时。
        """
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(self.thread_pool, lambda: fn(*args, **kwargs))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise CatError(
                ErrorCode.OPERATION_TIMEOUT,
                f"Operation timed out after {timeout}s",
            )

    # ------------------------------------------------------------------
    # 搜索配额
    # ------------------------------------------------------------------

    @property
    def max_search_seconds(self) -> int:
        """搜索超时秒数。"""
        return self._limits.max_search_seconds

    @property
    def max_search_results(self) -> int:
        """搜索结果上限。"""
        return self._limits.max_search_results

    # ------------------------------------------------------------------
    # 响应大小检查
    # ------------------------------------------------------------------

    @property
    def max_response_bytes(self) -> int:
        """单次工具响应最大字节数。"""
        return self._limits.max_tool_response_bytes


# ---------------------------------------------------------------------------
# 模块级快捷函数
# ---------------------------------------------------------------------------

# 全局 QuotaManager 实例（由 server.py 初始化）
_quota_manager: QuotaManager | None = None


def set_quota_manager(mgr: QuotaManager) -> None:
    """设置全局 ``QuotaManager`` 实例。

    Args:
        mgr: 已初始化的配额管理器。
    """
    global _quota_manager
    _quota_manager = mgr


def get_quota_manager() -> QuotaManager:
    """获取全局 ``QuotaManager`` 实例。

    Raises:
        RuntimeError: 未调用 ``set_quota_manager`` 时。
    """
    if _quota_manager is None:
        raise RuntimeError(
            "QuotaManager not initialised — call set_quota_manager() first"
        )
    return _quota_manager


def check_response_size(
    data: str | bytes | bytearray,
    max_bytes: int | None = None,
) -> None:
    """检查响应数据是否超出大小限制。

    Args:
        data: 响应数据（字符串或字节串）。
        max_bytes: 上限字节数（默认取自
            :attr:`QuotaManager.max_response_bytes`）。

    Raises:
        CatError (TOO_LARGE): 数据超过上限。
    """
    if max_bytes is None:
        max_bytes = get_quota_manager().max_response_bytes

    size = len(data) if isinstance(data, (bytes, bytearray)) else len(data.encode())
    if size > max_bytes:
        raise CatError(
            ErrorCode.FILE_TOO_LARGE,
            f"Response exceeds size limit ({size} > {max_bytes} bytes)",
        )


def check_concurrency(subject: str) -> None:
    """检查主体 *subject* 的并发额度。可用则获取槽位，不可用则抛异常。

    Args:
        subject: 已认证的主体标识。

    Raises:
        CatError (RATE_LIMITED): 并发超限。
    """
    mgr = get_quota_manager()
    if not mgr.acquire_concurrency(subject):
        limit = mgr._limits.max_concurrent_per_subject
        raise CatError(
            ErrorCode.LIMIT_EXCEEDED,
            f"Concurrency limit reached ({limit} concurrent operations per subject)",
        )


def release_concurrency(subject: str) -> None:
    """释放主体 *subject* 的并发槽位。

    Args:
        subject: 已认证的主体标识。
    """
    mgr = get_quota_manager()
    mgr.release_concurrency(subject)
