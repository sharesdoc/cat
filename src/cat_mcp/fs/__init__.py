"""安全文件系统操作模块。

提供 fd-relative 安全路径解析、目录遍历、文件读取、配额管理、敏感文件保护等文件操作能力。
"""
# 依据: Task-R-20261126012656446-P0

from cat_mcp.fs.paths import SecurePathResolver, ResolvedPath
from cat_mcp.fs.quota import (
    QuotaManager,
    check_response_size,
    check_concurrency,
    release_concurrency,
    set_quota_manager,
    get_quota_manager,
)
from cat_mcp.fs.reader import FileReader
from cat_mcp.fs.sensitive import SensitiveFileChecker, is_sensitive, set_checker, get_checker
from cat_mcp.fs.directory import DirectoryLister
from cat_mcp.fs.lock import WriteLockManager, LockContext
from cat_mcp.fs.atomic import AtomicWriter, CommitStatus, CommitContext
from cat_mcp.fs.idempotency import IdempotencyChecker
from cat_mcp.fs.audit import AuditLogger, set_audit_logger, get_audit_logger
from cat_mcp.fs.writer import _register_cat_write_tools
from cat_mcp.fs.directory_ops import _register_cat_directory_ops_tools

__all__ = [
    "SecurePathResolver",
    "ResolvedPath",
    "QuotaManager",
    "check_response_size",
    "check_concurrency",
    "release_concurrency",
    "set_quota_manager",
    "get_quota_manager",
    "FileReader",
    "SensitiveFileChecker",
    "is_sensitive",
    "set_checker",
    "get_checker",
    "DirectoryLister",
    "WriteLockManager",
    "LockContext",
    "AtomicWriter",
    "CommitStatus",
    "CommitContext",
    "IdempotencyChecker",
    "AuditLogger",
    "set_audit_logger",
    "get_audit_logger",
    "_register_cat_write_tools",
    "_register_cat_directory_ops_tools",
]
