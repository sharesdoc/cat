"""安全文件系统操作模块。

提供 fd-relative 安全路径解析、目录遍历、文件读取、配额管理、敏感文件保护等文件操作能力。
"""

from cat_mcp.fs.paths import SecurePathResolver, ResolvedPath
from cat_mcp.fs.quota import (
    QuotaManager,
    check_response_size,
    check_concurrency,
    release_concurrency,
    set_quota_manager,
    get_quota_manager,
)
from cat_mcp.fs.sensitive import SensitiveFileChecker, is_sensitive, set_checker, get_checker
from cat_mcp.fs.directory import DirectoryLister

__all__ = [
    "SecurePathResolver",
    "ResolvedPath",
    "QuotaManager",
    "check_response_size",
    "check_concurrency",
    "release_concurrency",
    "set_quota_manager",
    "get_quota_manager",
    "SensitiveFileChecker",
    "is_sensitive",
    "set_checker",
    "get_checker",
    "DirectoryLister",
]
