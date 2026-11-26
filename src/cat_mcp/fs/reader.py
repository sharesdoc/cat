"""单文件属性获取和分段文本读取。

提供：
- :class:`FileReader` — 文件 stat 和分段读取核心实现
- :func:`_register_cat_reader_tools` — ``cat_stat``、``cat_read_file`` 工具注册

使用 ``SecurePathResolver`` 做安全路径解析，``SensitiveFileChecker`` 做敏感文件检查。

用法::

    from cat_mcp.fs.reader import FileReader

    reader = FileReader("/srv/cat/projects/demo-app")
    info = reader.stat("src/main.py")
    result = reader.read_file("src/main.py", start_line=1, end_line=50)
"""
# 依据: Task-R-20261126012656446-P0

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import stat as stat_module
from datetime import datetime, timezone
from typing import Any

from cat_mcp.errors import CatError, ErrorCode
from cat_mcp.fs.paths import SecurePathResolver, _raise_cat_error
from cat_mcp.fs.sensitive import is_sensitive as _is_sensitive_fn
from cat_mcp.fs.sensitive import SensitiveFileChecker
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# BOM 序列（按最长匹配优先排列）
_BOM_TABLE: list[tuple[bytes, str]] = [
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]

_READ_CHUNK_SIZE = 65536  # 64KB，阅读和哈希的流块大小

# ---------------------------------------------------------------------------
# 编码/换行/MIME 检测工具函数
# ---------------------------------------------------------------------------


def _detect_bom(data: bytes) -> tuple[str | None, bytes]:
    """检测 *data* 开头是否包含 BOM。

    Returns:
        ``(bom_encoding, data_without_bom)``。
        若未检测到 BOM，bom_encoding 为 None。
    """
    # 先尝试最长的 BOM（4 字节）
    for bom, encoding in _BOM_TABLE:
        if data.startswith(bom):
            return encoding, data[len(bom) :]
    return None, data


def _detect_newline(data: bytes) -> str | None:
    """从原始字节 *data* 检测换行风格。

    Returns:
        ``"LF"``、``"CRLF"``、``"mixed"``，或未检测到换行时返回 ``None``。
    """
    crlf_count = data.count(b"\r\n")
    if crlf_count == 0:
        return "LF" if b"\n" in data else None
    # 有 CRLF，检查是否纯 CRLF 或混合
    remaining = data.replace(b"\r\n", b"")
    return "mixed" if b"\n" in remaining else "CRLF"


def _guess_mime(path: str) -> str | None:
    """根据文件扩展名猜测 MIME 类型。

    Returns:
        MIME 类型字符串，无法猜测时返回 ``None``。
    """
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type


def _detect_encoding(raw: bytes) -> str | None:
    """使用 chardet 检测原始字节 *raw* 的编码。

    优先检测 BOM，BOM 不可用时用 chardet。

    Returns:
        编码名称（小写），无法可靠检测时返回 ``None``。
    """
    if not raw:
        # 空文件默认 UTF-8
        return "utf-8"

    # 先检测 BOM
    bom_enc, _ = _detect_bom(raw)
    if bom_enc:
        return bom_enc

    # 用 chardet
    try:
        import chardet

        result = chardet.detect(raw)
        if result and result.get("encoding") and result.get("confidence", 0) >= 0.5:
            return result["encoding"].lower()
    except ImportError:
        pass
    return None


# ---------------------------------------------------------------------------
# 文件统计与分析方法
# ---------------------------------------------------------------------------


def _build_stat_entry_from_raw(
    resolved_path: str,
    st: os.stat_result | None,
    raw: bytes | None = None,
) -> dict[str, Any]:
    """从 stat 结果和原始字节构建文件元数据。

    返回字段：
        path, type, size, mtime (UTC ISO 8601), version, encoding, newline, MIME

    Args:
        resolved_path: 工程相对路径。
        st: lstat 结果（可为 None，表示不存在）。
        raw: 文件的原始字节内容（仅对普通文件需要，用于计算哈希/编码）。

    Returns:
        元数据字典。
    """
    result: dict[str, Any] = {
        "path": resolved_path,
        "type": "unknown",
        "size": st.st_size if st else 0,
        "mtime": _format_mtime(st.st_mtime) if st else None,
        "version": None,
        "encoding": None,
        "newline": None,
        "MIME": None,
    }

    if st is None:
        return result

    mode = st.st_mode

    # 符号链接
    if stat_module.S_ISLNK(mode):
        result["type"] = "symlink"
        return result

    # 目录
    if stat_module.S_ISDIR(mode):
        result["type"] = "directory"
        return result

    # 非普通文件（设备、FIFO 等）
    if not stat_module.S_ISREG(mode):
        result["type"] = "special"
        return result

    # 普通文件
    result["type"] = "file"

    if raw is not None:
        # 版本 = SHA-256 原始字节哈希
        result["version"] = hashlib.sha256(raw).hexdigest()
        # 编码检测
        encoding = _detect_encoding(raw)
        result["encoding"] = encoding
        # 换行风格
        result["newline"] = _detect_newline(raw)
    else:
        result["version"] = None
        result["encoding"] = None
        result["newline"] = None

    # MIME 猜测
    result["MIME"] = _guess_mime(resolved_path)

    return result


# ---------------------------------------------------------------------------
# 文件读取与分析
# ---------------------------------------------------------------------------


class FileReader:
    """单文件属性获取和分段文本读取。

    使用 ``SecurePathResolver`` 做安全路径解析，自动检查敏感文件。

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

    def stat(self, path: str, base: str = ".") -> dict[str, Any]:
        """获取文件/目录的完整元数据。

        Args:
            path: 工程相对路径。
            base: 基准路径（CWD 或 "."）。

        Returns:
            元数据字典，含 ``path``、``type``、``size``、``mtime``、
            ``version``、``encoding``、``newline``、``MIME``。
            目录和符号链接的版本、编码等字段为 ``None``。

        Raises:
            CatError (FORBIDDEN): 目标为敏感文件。
            CatError (FILE_NOT_FOUND): 文件不存在。
            CatError (UNSAFE_PATH): 路径含不安全组件。
        """
        return self._resolve_and_stat(path, base)

    def read_file(  # noqa: C901
        self,
        path: str,
        base: str = ".",
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int = 32768,
        expected_version: str | None = None,
        byte_offset: int | None = None,
    ) -> dict[str, Any]:
        """读取单个文件的全部或指定行/字节范围。

        支持行范围（1-based 闭区间）和字节偏移续读（长行）。
        始终返回完整文件的 SHA-256 版本哈希，支持版本一致性校验。

        Args:
            path: 工程相对路径。
            base: 基准路径。
            start_line: 起始行号（1-based，闭区间）。
            end_line: 结束行号（闭区间）。
            max_bytes: 最大返回内容字节数（默认 32768）。
            expected_version: 预期版本哈希。续读时必须传首次返回的版本，
                不匹配返回 ``VERSION_CONFLICT``。
            byte_offset: 原始字节偏移（0-based），用于长行续读。
                与行列范围互斥，必须位于 UTF-8 字符边界。

        Returns:
            ``path``、``version``、``encoding``、``newline``、
            ``start_line``、``end_line``、``total_lines``、
            ``content``、``has_more``、``next_start_line``、
            ``next_byte_offset``、``truncated_reason``。

        Raises:
            CatError (FORBIDDEN): 敏感文件。
            CatError (VERSION_CONFLICT): 文件版本与 expected_version 不匹配。
            CatError (UNSUPPORTED_ENCODING): 无法检测编码（二进制文件）。
            CatError (FILE_NOT_FOUND): 文件不存在。
        """
        with self._resolver.resolve(path, base=base) as rp:
            # 敏感文件检查
            self._check_sensitive(rp.resolved_path)

            # stat 确认是普通文件
            st = rp.stat(follow_symlinks=False)
            if stat_module.S_ISDIR(st.st_mode):
                raise CatError(ErrorCode.IS_DIRECTORY, "Cannot read a directory")
            if not stat_module.S_ISREG(st.st_mode):
                raise CatError(
                    ErrorCode.OPERATION_FAILED,
                    "Cannot read non-regular file",
                )

            # 打开文件读取原始字节
            fd = rp.open(os.O_RDONLY)
            try:
                raw = _read_all(fd)
            finally:
                os.close(fd)

        # 版本 = SHA-256 原始字节哈希
        version = hashlib.sha256(raw).hexdigest()

        # 版本一致性校验
        if expected_version is not None and expected_version != version:
            raise CatError(
                ErrorCode.VERSION_CONFLICT,
                "File has changed since last read",
            )

        # 编码检测
        bom_enc, raw_no_bom = _detect_bom(raw)
        encoding = bom_enc or _detect_encoding(raw)
        if encoding is None:
            raise CatError(
                ErrorCode.UNSUPPORTED_ENCODING,
                "Cannot detect file encoding; binary or unknown encoding",
            )

        # 换行风格
        newline = _detect_newline(raw)

        # MIME 猜测
        mime = _guess_mime(rp.resolved_path)

        # 解码为文本
        try:
            text = raw_no_bom.decode(encoding, errors="strict")
        except (UnicodeDecodeError, LookupError):
            raise CatError(
                ErrorCode.UNSUPPORTED_ENCODING,
                f"Failed to decode file as {encoding}",
            )

        # 计算总行数
        total_lines = _count_lines(text)

        # 字节偏移模式（长行续读）
        if byte_offset is not None:
            return self._read_by_byte_offset(
                raw=raw,
                encoding=encoding,
                byte_offset=byte_offset,
                max_bytes=max_bytes,
                path=rp.resolved_path,
                version=version,
                newline=newline,
                mime=mime,
                total_lines=total_lines,
            )

        # 行范围模式
        return self._read_by_lines(
            text=text,
            start_line=start_line,
            end_line=end_line,
            max_bytes=max_bytes,
            path=rp.resolved_path,
            version=version,
            encoding=encoding,
            newline=newline,
            mime=mime,
            total_lines=total_lines,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _resolve_and_stat(
        self,
        path: str,
        base: str = ".",
    ) -> dict[str, Any]:
        """安全解析路径并获取元数据。

        始终解析到父目录然后 lstat 最后组件，以便正确报告 symlink。

        Args:
            path: 工程相对路径。
            base: 基准路径。

        Returns:
            元数据字典。
        """
        from cat_mcp.fs.paths import _validate_path_format, _normalize

        _validate_path_format(path)
        _validate_path_format(base)
        target = _normalize(path, base)

        if not target or target == ".":
            # 工程根
            with self._resolver.resolve(path, base=base) as rp:
                self._check_sensitive(rp.resolved_path)
                st = rp.stat(follow_symlinks=False)
                return _build_stat_entry_from_raw(
                    rp.resolved_path, st, raw=None,
                )

        pure = PurePosixPath(target)
        parent_path = str(pure.parent) if len(pure.parts) > 1 else "."
        last_component = pure.name
        full_path = pure.as_posix()

        # 先检查敏感
        self._check_sensitive(full_path)

        # 解析父目录
        with self._resolver.resolve(parent_path, base=base) as rp:
            try:
                parent_fd = os.open(
                    rp.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=rp.dir_fd,
                )
            except OSError as exc:
                _raise_cat_error(exc, full_path)

            try:
                st = os.stat(last_component, dir_fd=parent_fd, follow_symlinks=False)
                raw: bytes | None = None
                if stat_module.S_ISREG(st.st_mode):
                    file_fd = os.open(
                        last_component, os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    try:
                        raw = _read_all(file_fd)
                    finally:
                        os.close(file_fd)
            except OSError as exc:
                _raise_cat_error(exc, full_path)
            finally:
                os.close(parent_fd)

            return _build_stat_entry_from_raw(full_path, st, raw)

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

    @staticmethod
    def _read_by_byte_offset(
        *,
        raw: bytes,
        encoding: str,
        byte_offset: int,
        max_bytes: int,
        path: str,
        version: str,
        newline: str | None,
        mime: str | None,
        total_lines: int | None,
    ) -> dict[str, Any]:
        """按字节偏移读取（长行续读）。"""
        if byte_offset < 0 or byte_offset >= len(raw):
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                f"byte_offset {byte_offset} is out of range (file size {len(raw)})",
            )

        # 确保不切断 UTF-8 字符（回退到字符开头）
        safe_offset = _align_to_utf8_start(raw, byte_offset, encoding)

        # 读取 max_bytes 字节（原始字节）
        end_pos = min(safe_offset + max_bytes, len(raw))
        chunk = raw[safe_offset:end_pos]

        # 确保结尾不切断 UTF-8 字符
        chunk = _trim_to_utf8_chars(chunk, encoding)

        # 解码
        content = chunk.decode(encoding, errors="replace")

        real_end = safe_offset + len(chunk)
        has_more = real_end < len(raw)

        return {
            "path": path,
            "version": version,
            "encoding": encoding,
            "newline": newline,
            "start_line": None,
            "end_line": None,
            "total_lines": total_lines,
            "content": content,
            "has_more": has_more,
            "next_start_line": None,
            "next_byte_offset": real_end if has_more else None,
            "truncated_reason": "byte_offset" if has_more else None,
            "MIME": mime,
        }

    @staticmethod
    def _read_by_lines(
        *,
        text: str,
        start_line: int | None,
        end_line: int | None,
        max_bytes: int,
        path: str,
        version: str,
        encoding: str,
        newline: str | None,
        mime: str | None,
        total_lines: int | None,
    ) -> dict[str, Any]:
        """按行范围读取。"""
        lines = text.splitlines(keepends=True)
        actual_total = len(lines)

        # 确定行范围
        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = actual_total if actual_total > 0 else 1

        # 校验行范围（空文件特殊处理）
        if actual_total == 0:
            return {
                "path": path,
                "version": version,
                "encoding": encoding,
                "newline": newline,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": 0,
                "content": "",
                "has_more": False,
                "next_start_line": None,
                "next_byte_offset": None,
                "truncated_reason": None,
                "MIME": mime,
            }

        # 校验行范围
        _validate_line_range(start_line, end_line, actual_total)

        # 提取目标行（1-based → 0-based）
        selected = lines[start_line - 1 : end_line]
        content = "".join(selected)

        # 检查是否需要截断（max_bytes 作用于解码后 UTF-8 字节数）
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > max_bytes:
            # 截断至 max_bytes 内，不切断 UTF-8 字符
            trunc_bytes = content_bytes[:max_bytes]
            # 解码截断后的字节，丢弃最后可能不完整的字符
            content = trunc_bytes.decode("utf-8", errors="ignore")
            # 去除可能的不完整最后一行
            has_more = True
            # 估算下一行号：截断后的文本行数
            truncated_lines = content.splitlines()
            next_line = start_line + len(truncated_lines)
            return {
                "path": path,
                "version": version,
                "encoding": encoding,
                "newline": newline,
                "start_line": start_line,
                "end_line": start_line + len(truncated_lines) - 1,
                "total_lines": actual_total if total_lines is None else total_lines,
                "content": content,
                "has_more": True,
                "next_start_line": next_line,
                "next_byte_offset": None,
                "truncated_reason": "max_bytes",
                "MIME": mime,
            }

        has_more = end_line < actual_total

        return {
            "path": path,
            "version": version,
            "encoding": encoding,
            "newline": newline,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": actual_total if total_lines is None else total_lines,
            "content": content,
            "has_more": has_more,
            "next_start_line": end_line + 1 if has_more else None,
            "next_byte_offset": None,
            "truncated_reason": "line_range" if has_more else None,
            "MIME": mime,
        }


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


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


def _count_lines(text: str) -> int:
    """统计字符串 *text* 的行数。

    空字符串返回 0，末尾换行的空行计为一行。

    Args:
        text: 解码后的文本。

    Returns:
        行数。
    """
    if not text:
        return 0
    count = text.count("\n")
    # 如果文本非空且不以换行结尾，总行数 = 换行数 + 1
    if not text.endswith("\n"):
        count += 1
    return count


def _validate_line_range(start: int, end: int, total: int) -> None:
    """校验行范围参数。

    Raises:
        CatError (INVALID_ARGUMENT): 范围不合法。
    """
    if start < 1:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"start_line must be >= 1, got {start}",
        )
    if end is not None and end < start:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"end_line ({end}) must be >= start_line ({start})",
        )
    if start > total and total > 0:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"start_line ({start}) exceeds file line count ({total})",
        )


def _format_mtime(timestamp: float) -> str:
    """将 UNIX 时间戳格式化为 UTC ISO 8601（毫秒精度）。

    Args:
        timestamp: ``os.stat`` 返回的时间戳。

    Returns:
        ISO 8601 格式字符串，如 ``2026-11-27T06:30:00.123Z``。
    """
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _align_to_utf8_start(data: bytes, offset: int, encoding: str) -> int:
    """将 *offset* 回退到最近的 UTF-8 字符边界。

    Args:
        data: 原始字节。
        offset: 当前偏移。
        encoding: 编码名称。

    Returns:
        调整后位于字符边界的偏移。
    """
    if encoding not in ("utf-8", "utf-8-sig", "ascii"):
        # 非 UTF-8 变长编码，直接返回
        return offset

    while offset < len(data):
        byte = data[offset]
        # UTF-8 连续字节以 10xxxxxx 开头，不是字符开头
        if (byte & 0xC0) != 0x80:
            break
        offset -= 1
    return max(offset, 0)


def _trim_to_utf8_chars(data: bytes, encoding: str) -> bytes:
    """截断 *data* 末尾的不完整 UTF-8 字符。

    Args:
        data: 原始字节块。
        encoding: 编码名称。

    Returns:
        末尾不包含不完整字符的字节块。
    """
    if encoding not in ("utf-8", "utf-8-sig", "ascii"):
        return data

    i = len(data)
    while i > 0:
        byte = data[i - 1]
        # 找到字符开头字节
        if (byte & 0xC0) != 0x80:
            break
        i -= 1

    if i > 0:
        byte = data[i - 1]
        # 计算字符字节数
        if byte & 0x80 == 0:
            expected_len = 1
        elif byte & 0xE0 == 0xC0:
            expected_len = 2
        elif byte & 0xF0 == 0xE0:
            expected_len = 3
        elif byte & 0xF8 == 0xF0:
            expected_len = 4
        else:
            expected_len = 1

        if i - 1 + expected_len > len(data):
            # 不完整字符，截断
            return data[: i - 1]

    return data


# ---------------------------------------------------------------------------
# MCP 工具注册函数
# ---------------------------------------------------------------------------


def _register_cat_reader_tools(
    server: Any,
    state: Any,
    access_checker: Any,
) -> None:
    """注册 ``cat_stat`` 和 ``cat_read_file`` 工具。

    Args:
        server: ``MCPServer`` 实例。
        state: ``StateManager`` 实例。
        access_checker: ``AccessChecker`` 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    # ------------------------------------------------------------------
    # cat_stat
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_stat",
        description="获取单个文件或目录的详细元数据，含类型、大小、修改时间、版本（SHA-256）、编码、换行风格、MIME。只读。",
        read_only=True,
    )
    async def cat_stat_handler(
        workspace_id: str,
        path: str,
        path_base: str | None = None,
    ) -> dict[str, Any]:
        """Handle ``cat_stat`` MCP tool call."""
        # 依据: Task-R-20261126012656446-P0
        subject = get_current_subject()

        # 验证 workspace 访问
        if not access_checker.check_workspace(subject, workspace_id, "read"):
            raise CatError(ErrorCode.FORBIDDEN, "Not authorized")

        # 获取 workspace 和 project 信息
        workspace = state.get_workspace(workspace_id)
        if workspace is None:
            raise CatError(ErrorCode.WORKSPACE_NOT_FOUND, "Workspace not found")

        project_id = workspace.get("project_id", "")
        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        root_path = project.get("root_path", "")
        if not root_path:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root path not configured")

        # 确定基准路径
        base = _resolve_base(path_base, workspace)

        # 创建 FileReader 并执行 stat
        reader = FileReader(root_path)
        result = reader.stat(path, base=base)

        # 移除 path 字段（输出不包含 path — 客户端已知路径）
        # 但 cat_stat 契约输出包含 path，保留
        return result

    # ------------------------------------------------------------------
    # cat_read_file
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_read_file",
        description=(
            "读取单个文件的全部或指定行/字节范围。支持分段续读和版本校验。 "
            "行号 1-based 闭区间。长行使用 byte_offset 续读。"
        ),
        read_only=True,
    )
    async def cat_read_file_handler(
        workspace_id: str,
        path: str,
        path_base: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int | None = None,
        expected_version: str | None = None,
        byte_offset: int | None = None,
    ) -> dict[str, Any]:
        """Handle ``cat_read_file`` MCP tool call."""
        # 依据: Task-R-20261126012656446-P0
        subject = get_current_subject()

        # 验证 workspace 访问
        if not access_checker.check_workspace(subject, workspace_id, "read"):
            raise CatError(ErrorCode.FORBIDDEN, "Not authorized")

        # 获取 workspace 和 project 信息
        workspace = state.get_workspace(workspace_id)
        if workspace is None:
            raise CatError(ErrorCode.WORKSPACE_NOT_FOUND, "Workspace not found")

        project_id = workspace.get("project_id", "")
        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        root_path = project.get("root_path", "")
        if not root_path:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root path not configured")

        # 确定基准路径
        base = _resolve_base(path_base, workspace)

        # 默认参数
        if max_bytes is None:
            max_bytes = 32768

        # 校验参数互斥
        if byte_offset is not None and (start_line is not None or end_line is not None):
            raise CatError(
                ErrorCode.INVALID_ARGUMENT,
                "byte_offset is mutually exclusive with start_line/end_line",
            )

        # 创建 FileReader 并执行读取
        reader = FileReader(root_path)
        result = reader.read_file(
            path,
            base=base,
            start_line=start_line,
            end_line=end_line,
            max_bytes=max_bytes,
            expected_version=expected_version,
            byte_offset=byte_offset,
        )

        # 清理响应（符合契约格式）
        # 移除内部使用的 MIME（该字段不在 cat_read_file 输出契约中）
        result.pop("MIME", None)

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
