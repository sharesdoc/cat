"""安全目录创建与操作 MCP 工具。

提供 ``cat_create_directory`` MCP 工具，支持创建单个目录或
递归创建缺失父目录。

安全约束：
- SecurePathResolver 拒绝 symlink/special files
- SensitiveFileChecker 拒绝敏感路径
- AccessChecker 授权校验
- QuotaManager 并发配额
- AuditLogger 审计记录
- AtomicWriter 幂等支持（通过 operations 表 request_id 去重）
"""
# 依据: CAT-23

from __future__ import annotations

import errno
import logging
import os
import stat as stat_module
from pathlib import PurePosixPath
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_directory_ops_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册目录操作 MCP 工具（``cat_create_directory`` 等）。

    Args:
        server: ``MCPServer`` 实例。
        workspace_mgr: ``WorkspaceManager`` 实例。
        state: ``StateManager`` 实例。
    """
    # 延迟导入避免循环依赖
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject
    from cat_mcp.fs.quota import get_quota_manager, check_concurrency

    # ------------------------------------------------------------------
    # cat_create_directory
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_create_directory",
        description=(
            "在工程内创建目录。"
            "parents=False（默认）时只创建最末一级，父目录必须已存在。"
            "parents=True 时递归创建缺失的父目录。"
            "返回实际创建的目录路径列表。"
        ),
        destructive=True,
    )
    async def create_directory_handler(
        workspace_id: str,
        path: str,
        parents: bool = False,
        request_id: str | None = None,
        path_base: str | None = None,
        cwd_version: int | None = None,
    ) -> dict[str, Any]:
        """处理 ``cat_create_directory`` MCP 工具调用。

        处理流程：
          1. 获取 subject, 校验 workspace
          2. 获取 project_id, project_root
          3. 确定 base 路径
          4. 并发配额检查
          5. 安全解析 path 的完整路径串
          6. 敏感文件检查 + ACL 授权
          7. 逐级创建目录（parents=False 仅最后一级）
          8. 审计日志
          9. 返回已创建目录列表

        Args:
            workspace_id: 工作空间标识。
            path: 要创建的目录路径。
            parents: 是否递归创建缺失父目录（默认 False）。
            request_id: 幂等请求 ID。
            path_base: "cwd" 或 "root"。
            cwd_version: 可选 CWD 版本校验。

        Returns:
            {"created": [str, ...]} — 实际创建的目录相对路径列表。
        """
        # 1. 认证主体
        subject = get_current_subject()

        # 2. 工作空间
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        # 3. 工程根
        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(
                ErrorCode.INTERNAL_ERROR,
                "Project root path not configured",
            )

        # 4. 确定 path_base
        if path_base is None:
            path_base = "cwd"
        base = _resolve_base(path_base, workspace)

        # 5. 并发配额
        check_concurrency(subject)

        # 5a. 可选的 CWD 版本校验（尽早执行，先于安全检查）
        if cwd_version is not None:
            from cat_mcp.fs.lock import get_lock_manager

            lock_mgr = get_lock_manager()
            lock_mgr.check_cwd_version(workspace, cwd_version)

        try:
            # 创建 SecurePathResolver
            from cat_mcp.fs.paths import SecurePathResolver

            resolver = SecurePathResolver(project_root)

            # 6. 安全解析 path（拒绝越界/symlink/特殊文件）
            with resolver.resolve(path, base=base) as rp:
                resolved_path = rp.resolved_path

            # 7. 敏感文件检查（检查最终目标路径）
            from cat_mcp.fs.sensitive import get_checker as get_sensitive_checker

            checker = get_sensitive_checker()
            if checker.is_sensitive(resolved_path):
                raise CatError(
                    ErrorCode.FORBIDDEN,
                    "Access to sensitive path is denied",
                )

            # 8. ACL 授权校验
            from cat_mcp.acl import get_access_checker

            access_checker = get_access_checker()
            if not access_checker.check(subject, project_id, resolved_path, "write"):
                raise CatError(
                    ErrorCode.FORBIDDEN,
                    "Not authorized to write to this path",
                )

            # 9. 幂等检查（通过 create_operation_idempotent 原子操作）
            if request_id:
                import hashlib

                op, is_new = state.create_operation_idempotent(
                    subject=subject,
                    workspace_id=workspace_id,
                    request_id=request_id,
                    path=path,
                    input_digest=hashlib.sha256(
                        resolved_path.encode("utf-8"),
                    ).hexdigest(),
                )
                if not is_new:
                    return {
                        "created": [],
                        "idempotent": True,
                    }

            # 10. 创建目录
            created = _create_directory(
                resolver=resolver,
                target=path,
                base=base,
                parents=parents,
                checker=checker,
                subject=subject,
                project_id=project_id,
                access_checker=access_checker,
            )

            # 11. 审计日志
            from cat_mcp.fs.audit import get_audit_logger

            try:
                audit = get_audit_logger()
                audit.log(
                    subject=subject,
                    project_id=project_id,
                    action="write.create_directory",
                    outcome="success",
                    path=path,
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit event for create_directory")

            logger.info(
                "Subject %s created %d directories: %s",
                subject, len(created), created,
            )

            return {"created": created}
        except CatError:
            # 失败路径的审计日志
            try:
                from cat_mcp.fs.audit import get_audit_logger

                get_audit_logger().log(
                    subject=subject,
                    project_id=project_id,
                    action="write.create_directory",
                    outcome="failure",
                    path=path,
                    workspace_id=workspace_id,
                    correlation_id=request_id,
                )
            except Exception:
                logger.warning("Failed to log audit failure event for create_directory")
            raise
        finally:
            get_quota_manager().release_concurrency(subject)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _create_directory(
    resolver: Any,
    target: str,
    base: str,
    parents: bool,
    checker: Any = None,
    subject: str = "",
    project_id: str = "",
    access_checker: Any = None,
) -> list[str]:
    """安全创建目录。

    使用 SecurePathResolver 逐级解析并 ``os.mkdir(dir_fd=...)``，
    确保每一级都不是 symlink 或特殊文件。

    Args:
        resolver: SecurePathResolver 实例。
        target: 相对工程根的目标路径。
        base: 基准路径（CWD 或 "."）。
        parents: 是否递归创建缺失父目录。

    Returns:
        实际创建的目录路径列表（相对工程根）。

    Raises:
        CatError: 创建失败时的业务异常。
    """
    # 规范化目标路径（获取需要创建的目录层级）
    from cat_mcp.fs.paths import _normalize

    normalized = _normalize(target, base)
    if normalized.startswith(".."):
        raise CatError(
            ErrorCode.PATH_OUT_OF_BOUNDS,
            f"Path escapes project root: {target}",
        )

    if not normalized or normalized == ".":
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "Cannot create project root directory",
        )

    components = PurePosixPath(normalized).parts
    created: list[str] = []

    if parents:
        # 递归模式：逐级创建
        accumulated = ""
        for i, component in enumerate(components):
            if accumulated:
                accumulated = f"{accumulated}/{component}"
            else:
                accumulated = component

            try:
                # 尝试 stat 该级路径看是否已存在
                with resolver.resolve(accumulated) as rp:
                    st = rp.stat(follow_symlinks=False)
                # stat 返回非 None（恒真），检查类型
                if stat_module.S_ISDIR(st.st_mode):
                    continue  # 已存在，跳过
                raise CatError(
                    ErrorCode.FILE_ALREADY_EXISTS,
                    f"Path exists and is not a directory",
                )
            except CatError as exc:
                if exc.error_code == ErrorCode.FILE_NOT_FOUND:
                    # 不存在 → 检查中间目录是否经过安全授权
                    _check_parent_sensitive(
                        accumulated, resolver, checker,
                        subject, project_id, access_checker,
                    )
                    _mkdir_level(resolver, accumulated, base)
                    created.append(accumulated)
                else:
                    raise

        return created

    # 非递归模式：仅创建最后一级，父目录必须存在
    # 先确认父目录存在
    parent = PurePosixPath(normalized).parent
    parent_str = parent.as_posix() if parent.as_posix() != "." else "."

    try:
        with resolver.resolve(parent_str if parent_str != normalized else ".") as rp:
            st = rp.stat(follow_symlinks=False)
            if not stat_module.S_ISDIR(st.st_mode):
                raise CatError(
                    ErrorCode.INVALID_PATH,
                    f"Parent path is not a directory: {parent_str}",
                )
    except CatError as exc:
        if exc.error_code == ErrorCode.FILE_NOT_FOUND:
            raise CatError(
                ErrorCode.FILE_NOT_FOUND,
                f"Parent directory does not exist: {parent_str}",
            )
        raise

    # 检查目标是否已存在
    try:
        with resolver.resolve(normalized) as rp:
            st = rp.stat(follow_symlinks=False)
        if stat_module.S_ISDIR(st.st_mode):
            return []  # 已存在，幂等返回空
        raise CatError(
            ErrorCode.FILE_ALREADY_EXISTS,
            f"A file already exists at path",
        )
    except CatError as exc:
        if exc.error_code != ErrorCode.FILE_NOT_FOUND:
            raise

    # 创建目录
    _mkdir_level(resolver, normalized, base)
    created.append(normalized)
    return created


def _check_parent_sensitive(
    path: str,
    resolver: Any,
    checker: Any,
    subject: str,
    project_id: str,
    access_checker: Any,
) -> None:
    """检查中间目录是否经过敏感文件和 ACL 授权。

    parents=True 模式下逐级创建中间目录前调用此函数，
    确保每个中间目录都经过安全校验。

    Args:
        path: 要检查的中间目录相对路径。
        resolver: SecurePathResolver 实例。
        checker: SensitiveFileChecker 实例。
        subject: 认证主体。
        project_id: 工程 ID。
        access_checker: AccessChecker 实例。

    Raises:
        CatError: 路径敏感或未授权时。
    """
    # 敏感文件检查
    if checker and checker.is_sensitive(path):
        raise CatError(
            ErrorCode.FORBIDDEN,
            "Access to sensitive path is denied",
        )

    # ACL 授权校验
    if access_checker and not access_checker.check(
        subject, project_id, path, "write",
    ):
        raise CatError(
            ErrorCode.FORBIDDEN,
            "Not authorized to write to this path",
        )


def _mkdir_level(
    resolver: Any,
    target: str,
    base: str,
) -> None:
    """创建一个目录层级（仅一级，不递归）。

    通过 SecurePathResolver 解析父目录 fd，然后
    使用 ``os.mkdir(dir_fd=...)`` 安全创建。

    Args:
        resolver: SecurePathResolver 实例。
        target: 要创建的目录相对路径（单级）。
        base: 基准路径。

    Raises:
        CatError: 创建失败时。
    """
    # 解析父目录
    try:
        parent_path = PurePosixPath(target).parent
        parent_str = parent_path.as_posix() if parent_path.as_posix() != "." else "."
        dir_name = PurePosixPath(target).name

        with resolver.resolve(parent_str) as rp:
            try:
                os.mkdir(dir_name, mode=0o755, dir_fd=rp.dir_fd)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    # 并发创建场景，不视为错误
                    logger.debug("Directory already exists (concurrent create): %s", target)
                    return
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Failed to create directory {target}",
                )
    except CatError:
        raise


# ---------------------------------------------------------------------------
# 辅助函数（共享）
# ---------------------------------------------------------------------------


def _resolve_base(path_base: str | None, workspace: dict[str, Any]) -> str:
    """解析 path_base 参数为工程相对路径。

    Args:
        path_base: "cwd"（默认）、"root" 或 None。
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
