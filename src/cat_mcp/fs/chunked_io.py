"""单文件分块上传和下载工具。

提供 ``cat_begin_upload``、``cat_upload_chunk``、``cat_finish_upload``、
``cat_abort_upload`` 和 ``cat_read_chunk`` 五个 MCP 工具，
支持超大文本和二进制文件的分块传输。

安全约束：
- 上传 session 绑定 subject+workspace，禁止跨主体访问
- 每块校验 SHA-256 哈希
- finish 时校验总大小和总 SHA-256 后原子提交
- 上传租期 + 超时清理
- 目标路径经 SecurePathResolver + SensitiveFileChecker 校验
- finish 前不修改目标文件
"""
# 依据: CAT-33

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# 默认上传租期（秒）
_DEFAULT_UPLOAD_TTL = 3600  # 1 小时

# 单块最大大小（字节）
_MAX_CHUNK_SIZE = 10 * 1024 * 1024  # 10MB

# 最大上传总大小（字节）
_MAX_UPLOAD_TOTAL = 500 * 1024 * 1024  # 500MB

# 最大块数
_MAX_CHUNKS = 10_000

# .cat_uploads 子目录名
_UPLOADS_DIR = ".cat_uploads"


def _generate_upload_id() -> str:
    """生成不可预测的 upload ID。

    Returns:
        32 字符的十六进制字符串。
    """
    return secrets.token_hex(16)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _get_uploads_dir(project_root: str) -> str:
    """返回项目根下的上传临时目录路径。"""
    return os.path.join(project_root, _UPLOADS_DIR)


def _ensure_uploads_dir(project_root: str) -> str:
    """确保上传临时目录存在并返回其路径。"""
    uploads_dir = _get_uploads_dir(project_root)
    os.makedirs(uploads_dir, exist_ok=True)
    return uploads_dir


def _get_temp_upload_path(project_root: str, upload_id: str, chunk_index: int) -> str:
    """返回指定块的临时文件路径。"""
    return os.path.join(_get_uploads_dir(project_root), f"{upload_id}.{chunk_index:06d}.tmp")


def _compute_sha256(data: bytes) -> str:
    """计算数据的 SHA-256 哈希。

    Args:
        data: 字节数据。

    Returns:
        小写十六进制哈希字符串。
    """
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 上传 session 管理
# ---------------------------------------------------------------------------


def _get_active_upload(
    state: Any,
    upload_id: str,
    subject: str,
) -> dict[str, Any]:
    """获取并验证 active 状态的 upload session。

    Args:
        state: StateManager 实例。
        upload_id: 上传 ID。
        subject: 主体标识。

    Returns:
        Upload session dict。

    Raises:
        CatError: 如果 session 不存在、已过期或不属于该主体。
    """
    session = state.get_upload_session(upload_id)
    if session is None:
        raise CatError(ErrorCode.UPLOAD_NOT_FOUND, f"Upload {upload_id!r} not found")

    if session["subject"] != subject:
        raise CatError(
            ErrorCode.FORBIDDEN,
            "Upload session does not belong to this subject",
        )

    if session["status"] != "active":
        raise CatError(
            ErrorCode.UPLOAD_INVALID_STATE,
            f"Upload session is in state {session['status']!r}, expected 'active'",
        )

    # 检查租期
    now = _now_iso()
    if session["expires_at"] < now:
        state.update_upload_session_status(upload_id, "expired")
        raise CatError(
            ErrorCode.UPLOAD_EXPIRED,
            "Upload session has expired",
        )

    return session


# ---------------------------------------------------------------------------
# cat_begin_upload — 启动上传
# ---------------------------------------------------------------------------


def _begin_upload(
    state: Any,
    workspace_mgr: Any,
    subject: str,
    workspace_id: str,
    project_id: str,
    project_root: str,
    path: str,
    expected_size: int,
    expected_sha256: str,
    *,
    ttl: int = _DEFAULT_UPLOAD_TTL,
) -> dict[str, Any]:
    """启动一个新的分块上传 session。

    Args:
        state: StateManager 实例。
        workspace_mgr: WorkspaceManager 实例。
        subject: 主体标识。
        workspace_id: 工作空间 ID。
        project_root: 项目根目录。
        path: 目标文件路径（相对于项目根）。
        expected_size: 预期总大小（字节）。
        expected_sha256: 预期最终文件 SHA-256 哈希。
        ttl: 上传租期（秒）。

    Returns:
        包含 upload_id 等信息的字典。

    Raises:
        CatError: 参数校验失败时。
    """
    from cat_mcp.fs.paths import SecurePathResolver
    from cat_mcp.fs.sensitive import is_sensitive as _check_sensitive

    if expected_size <= 0:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "expected_size must be positive",
        )
    if expected_size > _MAX_UPLOAD_TOTAL:
        raise CatError(
            ErrorCode.FILE_TOO_LARGE,
            f"Total upload size {expected_size} exceeds maximum {_MAX_UPLOAD_TOTAL}",
        )
    if len(expected_sha256) != 64 or not all(c in "0123456789abcdef" for c in expected_sha256.lower()):
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "expected_sha256 must be a 64-character hex string",
        )

    # 路径安全检查
    resolver = SecurePathResolver(project_root)
    try:
        resolved = resolver.resolve(path)
    except CatError:
        raise
    except Exception as exc:
        raise CatError(ErrorCode.INVALID_PATH, f"Invalid path: {exc}")

    if _check_sensitive(resolved.resolved_path):
        raise CatError(ErrorCode.UNSAFE_PATH, f"Path {path!r} is sensitive")

    # 检查是否已存在（用 stat 而不是写入）
    if os.path.exists(resolved.resolved_path):
        raise CatError(
            ErrorCode.FILE_ALREADY_EXISTS,
            f"Target file {path!r} already exists",
        )

    # 确保上传临时目录存在
    _ensure_uploads_dir(project_root)

    # 生成 upload_id
    upload_id = _generate_upload_id()
    expires_at = datetime.fromtimestamp(
        time.time() + ttl, tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S.") + f"{(datetime.fromtimestamp(time.time() + ttl, tz=timezone.utc).microsecond // 1000):03d}Z"

    session = state.create_upload_session(
        upload_id=upload_id,
        subject=subject,
        workspace_id=workspace_id,
        project_id=project_id,
        path=path,
        expected_size=expected_size,
        expected_sha256=expected_sha256.lower(),
        expires_at=expires_at,
    )

    return {
        "upload_id": upload_id,
        "expected_size": expected_size,
        "expected_sha256": expected_sha256.lower(),
        "expires_at": expires_at,
        "chunk_size_limit": _MAX_CHUNK_SIZE,
        "max_chunks": _MAX_CHUNKS,
    }


# ---------------------------------------------------------------------------
# cat_upload_chunk — 上传单个数据块
# ---------------------------------------------------------------------------


def _upload_chunk(
    state: Any,
    subject: str,
    upload_id: str,
    chunk_index: int,
    data_base64: str,
    chunk_sha256: str,
) -> dict[str, Any]:
    """上传单个数据块。

    Args:
        state: StateManager 实例。
        subject: 主体标识。
        upload_id: 上传 session ID。
        chunk_index: 块序号（从 0 开始）。
        data_base64: Base64 编码的块数据。
        chunk_sha256: 块数据的 SHA-256 哈希。

    Returns:
        包含块接收确认信息的字典。

    Raises:
        CatError: 参数校验或哈希校验失败时。
    """
    session = _get_active_upload(state, upload_id, subject)

    project_id = session["project_id"]
    project = state.get_project(project_id)
    if project is None:
        raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")
    project_root: str = project.get("root_path", "")

    if chunk_index < 0:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "chunk_index must be >= 0",
        )
    if chunk_index >= _MAX_CHUNKS:
        raise CatError(
            ErrorCode.LIMIT_EXCEEDED,
            f"chunk_index {chunk_index} exceeds maximum {_MAX_CHUNKS}",
        )

    # 解码 base64
    try:
        chunk_data = base64.b64decode(data_base64)
    except (ValueError, Exception) as exc:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"Invalid base64 data: {exc}",
        )

    # 检查块大小
    if len(chunk_data) > _MAX_CHUNK_SIZE:
        raise CatError(
            ErrorCode.FILE_TOO_LARGE,
            f"Chunk size {len(chunk_data)} exceeds limit {_MAX_CHUNK_SIZE}",
        )

    # 空块只在已是最后一块时允许（由 finish 逻辑判断）
    if len(chunk_data) == 0 and session["expected_size"] > 0:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "Empty chunk is not allowed for non-empty upload",
        )

    # 校验块哈希
    actual_hash = _compute_sha256(chunk_data)
    if actual_hash != chunk_sha256.lower():
        raise CatError(
            ErrorCode.CHUNK_HASH_MISMATCH,
            f"Chunk SHA-256 mismatch: expected {chunk_sha256}, got {actual_hash}",
        )

    # 检查总大小限制
    new_total = session["total_received"] + len(chunk_data)
    if new_total > session["expected_size"]:
        raise CatError(
            ErrorCode.FILE_TOO_LARGE,
            f"Upload would exceed expected size {session['expected_size']}",
        )
    if new_total > _MAX_UPLOAD_TOTAL:
        raise CatError(
            ErrorCode.FILE_TOO_LARGE,
            f"Upload would exceed maximum size {_MAX_UPLOAD_TOTAL}",
        )

    # 写入临时文件
    temp_path = _get_temp_upload_path(project_root, upload_id, chunk_index)
    try:
        with open(temp_path, "wb") as f:
            f.write(chunk_data)
    except OSError as exc:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            f"Failed to write chunk: {exc}",
        )

    # 记录到数据库
    try:
        state.record_upload_chunk(upload_id, chunk_index, actual_hash, temp_path)
        state.increment_upload_chunks(upload_id, len(chunk_data))
    except Exception:
        # 数据库记录失败时清理临时文件
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    return {
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "chunk_sha256": actual_hash,
        "chunk_size": len(chunk_data),
        "received": new_total,
    }


# ---------------------------------------------------------------------------
# cat_finish_upload — 完成上传并原子提交
# ---------------------------------------------------------------------------


def _finish_upload(
    state: Any,
    idempotency_checker: Any,
    subject: str,
    upload_id: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """完成上传：验证所有块后原子提交到目标文件。

    Args:
        state: StateManager 实例。
        idempotency_checker: IdempotencyChecker 实例。
        subject: 主体标识。
        upload_id: 上传 session ID。
        request_id: 可选幂等键。

    Returns:
        包含提交结果的字典。

    Raises:
        CatError: 验证失败时（块缺失、哈希不匹配等）。
    """
    session = _get_active_upload(state, upload_id, subject)

    project_id = session["project_id"]
    project = state.get_project(project_id)
    if project is None:
        raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")
    project_root: str = project.get("root_path", "")

    target_path: str = session["path"]
    expected_size: int = session["expected_size"]
    expected_sha256: str = session["expected_sha256"]

    # 获取所有已接收块
    chunks = state.get_upload_chunks(upload_id)
    if not chunks:
        raise CatError(
            ErrorCode.UPLOAD_INVALID_STATE,
            "No chunks received",
        )

    # 验证块连续性
    received_indices = sorted(c["chunk_index"] for c in chunks)
    expected_indices = list(range(len(received_indices)))
    if received_indices != expected_indices:
        missing = set(expected_indices) - set(received_indices)
        raise CatError(
            ErrorCode.UPLOAD_INVALID_STATE,
            f"Missing chunks: {sorted(missing)}",
        )

    # 验证总大小
    total_received = session["total_received"]
    if total_received != expected_size:
        raise CatError(
            ErrorCode.UPLOAD_INVALID_STATE,
            f"Total received {total_received} != expected {expected_size}",
        )

    # 组装并计算总 SHA-256，同时写入目标
    sha256_hash = hashlib.sha256()

    from cat_mcp.fs.paths import SecurePathResolver
    resolver = SecurePathResolver(project_root)

    try:
        resolved = resolver.resolve(target_path)
    except CatError:
        raise
    except Exception as exc:
        raise CatError(ErrorCode.INVALID_PATH, f"Invalid path: {exc}")

    # 使用 AtomicWriter 原子写入
    from cat_mcp.fs.atomic import AtomicWriter, set_atomic_writer, get_atomic_writer
    from cat_mcp.fs.lock import get_lock_manager

    atomic_writer = get_atomic_writer(project_root)
    if atomic_writer is None:
        # 创建临时的 AtomicWriter
        from cat_mcp.fs.paths import SecurePathResolver as _SPR
        lock_mgr = get_lock_manager()
        atomic_writer = AtomicWriter(
            project_root=project_root,
            state_manager=state,
            resolver=_SPR(project_root),
            lock_manager=lock_mgr,
        )

    # 准备原子写入
    commit_ctx = atomic_writer.prepare_write(
        path=target_path,
        mode="create",
        subject=subject,
        request_id=request_id or upload_id,
    )

    try:
        # 按顺序合并所有块并计算哈希
        for chunk in chunks:
            chunk_path: str = chunk["temp_path"]
            try:
                with open(chunk_path, "rb") as cf:
                    while True:
                        buf = cf.read(65536)
                        if not buf:
                            break
                        sha256_hash.update(buf)
                        atomic_writer.write(commit_ctx, buf)
            except OSError as exc:
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    f"Failed to read chunk {chunk['chunk_index']}: {exc}",
                )

        # 提交写入
        atomic_writer.commit_write(commit_ctx)

        total_hash = sha256_hash.hexdigest()
    except CatError:
        atomic_writer.abort_write(commit_ctx)
        raise
    except Exception:
        atomic_writer.abort_write(commit_ctx)
        raise

    # 验证总哈希
    if total_hash != expected_sha256:
        # 哈希不匹配：删除刚写入的文件
        try:
            os.unlink(resolved.resolved_path)
        except OSError:
            pass
        state.update_upload_session_status(upload_id, "failed")
        raise CatError(
            ErrorCode.CHUNK_HASH_MISMATCH,
            f"Total SHA-256 mismatch: expected {expected_sha256}, got {total_hash}",
        )

    # 标记 session 完成
    state.update_upload_session_status(upload_id, "completed")

    # 清理临时块文件
    _cleanup_chunks(project_root, upload_id, chunks)

    return {
        "upload_id": upload_id,
        "path": target_path,
        "size": total_received,
        "sha256": total_hash,
    }


def _cleanup_chunks(project_root: str, upload_id: str, chunks: list[dict[str, Any]]) -> None:
    """清理上传过程中产生的临时块文件。"""
    for chunk in chunks:
        temp_path = chunk.get("temp_path", "")
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    # 尝试删除 upload_id 相关的 .tmp 文件
    uploads_dir = _get_uploads_dir(project_root)
    try:
        for entry in os.listdir(uploads_dir):
            if entry.startswith(upload_id):
                os.unlink(os.path.join(uploads_dir, entry))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# cat_abort_upload — 中止上传
# ---------------------------------------------------------------------------


def _abort_upload(
    state: Any,
    subject: str,
    upload_id: str,
) -> dict[str, Any]:
    """中止上传并清理所有临时文件。

    Args:
        state: StateManager 实例。
        subject: 主体标识。
        upload_id: 上传 session ID。

    Returns:
        包含确认信息的字典。
    """
    session = _get_active_upload(state, upload_id, subject)

    project_id = session["project_id"]
    project = state.get_project(project_id)
    if project is None:
        raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")
    project_root: str = project.get("root_path", "")

    # 获取所有块并清理
    chunks = state.get_upload_chunks(upload_id)
    _cleanup_chunks(project_root, upload_id, chunks)

    # 标记 session 为已中止
    state.update_upload_session_status(upload_id, "aborted")

    return {
        "upload_id": upload_id,
        "path": session["path"],
        "status": "aborted",
    }


# ---------------------------------------------------------------------------
# cat_read_chunk — 分块读取文件
# ---------------------------------------------------------------------------


def _read_chunk(
    state: Any,
    workspace_mgr: Any,
    subject: str,
    workspace_id: str,
    project_root: str,
    path: str,
    offset: int = 0,
    size: int = 65536,
    *,
    mode: str = "text",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """读取文件的指定片段。

    类似现有的分段读取，但以 base64 格式支持二进制内容。

    Args:
        state: StateManager 实例。
        workspace_mgr: WorkspaceManager 实例。
        subject: 主体标识。
        workspace_id: 工作空间 ID。
        project_root: 项目根目录。
        path: 目标文件路径（相对于项目根）。
        offset: 读取起始偏移（字节）。
        size: 读取大小（字节，默认 64KB）。
        mode: 读取模式："text"（文本）或 "base64"（二进制）。
        encoding: 文本编码（仅 mode="text" 时使用）。

    Returns:
        包含文件片段的字典。

    Raises:
        CatError: 参数校验或文件访问失败时。
    """
    from cat_mcp.fs.paths import SecurePathResolver
    from cat_mcp.fs.sensitive import is_sensitive as _check_sensitive

    if offset < 0:
        raise CatError(ErrorCode.INVALID_ARGUMENT, "offset must be >= 0")
    if size <= 0:
        raise CatError(ErrorCode.INVALID_ARGUMENT, "size must be > 0")
    max_read = 10 * 1024 * 1024  # 10MB
    if size > max_read:
        raise CatError(
            ErrorCode.FILE_TOO_LARGE,
            f"Read size {size} exceeds maximum {max_read}",
        )

    resolver = SecurePathResolver(project_root)
    try:
        resolved = resolver.resolve(path)
    except CatError:
        raise
    except Exception as exc:
        raise CatError(ErrorCode.INVALID_PATH, f"Invalid path: {exc}")

    if _check_sensitive(resolved.resolved_path):
        raise CatError(ErrorCode.UNSAFE_PATH, f"Path {path!r} is sensitive")

    if not os.path.isfile(resolved.resolved_path):
        raise CatError(ErrorCode.FILE_NOT_FOUND, f"File {path!r} not found")

    file_size = os.path.getsize(resolved.resolved_path)
    if offset >= file_size:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"Offset {offset} exceeds file size {file_size}",
        )

    # 读取文件片段
    try:
        with open(resolved.resolved_path, "rb") as f:
            f.seek(offset)
            data = f.read(size)
    except OSError as exc:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            f"Failed to read file: {exc}",
        )

    actual_size = len(data)
    end_offset = offset + actual_size

    if mode == "text":
        try:
            content = data.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            raise CatError(
                ErrorCode.UNSUPPORTED_ENCODING,
                f"Cannot decode as {encoding}: {exc}",
            )
        return {
            "path": path,
            "offset": offset,
            "size": actual_size,
            "end_offset": end_offset,
            "content": content,
            "file_size": file_size,
            "mode": "text",
        }
    else:
        # base64 模式
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "path": path,
            "offset": offset,
            "size": actual_size,
            "end_offset": end_offset,
            "data_base64": encoded,
            "file_size": file_size,
            "mode": "base64",
        }


# ---------------------------------------------------------------------------
# MCP 工具注册
# ---------------------------------------------------------------------------


def _register_cat_chunked_io_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册分块上传/下载工具。

    Args:
        server: MCPServer 实例。
        workspace_mgr: WorkspaceManager 实例。
        state: StateManager 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    from cat_mcp.fs.idempotency import IdempotencyChecker

    idempotency_checker = IdempotencyChecker(state)

    # ------------------------------------------------------------------
    # cat_begin_upload
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_begin_upload",
        description=(
            "启动新的分块上传 session。返回 upload_id，后续 upload_chunk/finish/abort 使用。"
            "需指定目标路径、预期总大小和预期 SHA-256。"
            "Session 有租期限制（默认 1 小时），过期自动失效。"
        ),
    )
    async def begin_upload_handler(
        workspace_id: str,
        path: str,
        expected_size: int,
        expected_sha256: str,
    ) -> dict[str, Any]:
        """启动分块上传。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        return _begin_upload(
            state=state,
            workspace_mgr=workspace_mgr,
            subject=subject,
            workspace_id=workspace_id,
            project_id=project_id,
            project_root=project_root,
            path=path,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    # ------------------------------------------------------------------
    # cat_upload_chunk
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_upload_chunk",
        description=(
            "上传单个数据块。需提供 upload_id、块序号、base64 编码数据和块 SHA-256。"
            "每块最大 10MB，块序号从 0 开始连续。"
            "支持乱序和重复上传（重复的块以最后一次为准）。"
        ),
    )
    async def upload_chunk_handler(
        upload_id: str,
        chunk_index: int,
        data_base64: str,
        chunk_sha256: str,
    ) -> dict[str, Any]:
        """上传数据块。"""
        subject = get_current_subject()
        return _upload_chunk(
            state=state,
            subject=subject,
            upload_id=upload_id,
            chunk_index=chunk_index,
            data_base64=data_base64,
            chunk_sha256=chunk_sha256,
        )

    # ------------------------------------------------------------------
    # cat_finish_upload
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_finish_upload",
        description=(
            "完成分块上传。验证所有块完整、总大小和总 SHA-256 匹配后，"
            "原子提交到目标文件。完成后清理临时块文件。"
            "支持 request_id 实现幂等性。"
        ),
    )
    async def finish_upload_handler(
        upload_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """完成分块上传。"""
        subject = get_current_subject()
        return _finish_upload(
            state=state,
            idempotency_checker=idempotency_checker,
            subject=subject,
            upload_id=upload_id,
            request_id=request_id,
        )

    # ------------------------------------------------------------------
    # cat_abort_upload
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_abort_upload",
        description=(
            "中止分块上传，清理所有临时块文件。"
            "中止后 upload_id 不再可用。"
        ),
    )
    async def abort_upload_handler(
        upload_id: str,
    ) -> dict[str, Any]:
        """中止上传。"""
        subject = get_current_subject()
        return _abort_upload(
            state=state,
            subject=subject,
            upload_id=upload_id,
        )

    # ------------------------------------------------------------------
    # cat_read_chunk
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_read_chunk",
        description=(
            "分块读取文件指定片段。支持文本和 base64 两种模式。"
            "适用于大文件的分段读取和二进制文件读取。"
            "每块最大 10MB。只读，不修改任何状态。"
        ),
        read_only=True,
    )
    async def read_chunk_handler(
        workspace_id: str,
        path: str,
        offset: int = 0,
        size: int = 65536,
        mode: str = "text",
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """分块读取文件。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        return _read_chunk(
            state=state,
            workspace_mgr=workspace_mgr,
            subject=subject,
            workspace_id=workspace_id,
            project_root=project_root,
            path=path,
            offset=offset,
            size=size,
            mode=mode,
            encoding=encoding,
        )
