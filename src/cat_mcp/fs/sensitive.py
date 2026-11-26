"""敏感文件和内容保护策略。

统一阻止禁止文件通过目录、读写、搜索、备份泄露。

提供:

- :class:`SensitiveFileChecker` — 敏感文件路径匹配器
- :func:`is_sensitive` — 快捷检查函数

用法::

    from cat_mcp.fs.sensitive import SensitiveFileChecker

    checker = SensitiveFileChecker()
    if checker.is_sensitive("config/.env"):
        # 拒绝访问

保守策略优先：不能保证识别全部业务秘密。
"""
# 依据: Task-R-20261126012656443-P0

from __future__ import annotations

import fnmatch
import os
from pathlib import PurePosixPath
from typing import ClassVar

# ---------------------------------------------------------------------------
# 默认 denylist —— 文件名/路径模式
# ---------------------------------------------------------------------------
# 规则来源: B-系统设计文档.md §5.4 敏感文件保护
# 所有模式按 PurePosixPath 的 POSIX 语义匹配。

_DEFAULT_DENY_PATTERNS: list[str] = [
    # 环境变量与配置文件
    ".env",
    ".env.*",           # .env.local, .env.production 等
    "!.env.example",    # 明确允许 .env.example（! 前缀 = 例外）
    # 私钥与证书
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.cer",
    "*.crt",
    "*.der",
    # 凭据与 Token
    "*.cred",
    "*.credential",
    "credentials",
    "credentials.*",
    ".netrc",
    ".pgpass",
    # SSH 配置
    ".ssh/*",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    # Git 内部配置
    ".git/config",
    ".git/credentials",
    ".git-credentials",
    # 生产配置
    "config/secrets.*",
    "secrets.*",
    "secret",
    # Kubernetes
    "*.kubeconfig",
    "kubeconfig",
    ".kube/*",
    # 云服务凭据
    "aws/credentials",
    ".aws/credentials",
    "gcloud.json",
    "*.service-account.json",
    # 数据库连接
    "*.db-password",
    "database.yml",
    # 其他已知敏感文件
    "*.htpasswd",
    ".htpasswd",
    "*.ovpn",
    # Token 文件
    "*.token",
    "*.tkn",
]

# 编译后的模式缓存（.env.example 以例外形式处理）
_DEFAULT_DENY_GLOB: list[tuple[str, bool]] = []
for raw in _DEFAULT_DENY_PATTERNS:
    if raw.startswith("!"):
        _DEFAULT_DENY_GLOB.append((raw[1:], True))  # True = allow (exception)
    else:
        _DEFAULT_DENY_GLOB.append((raw, False))


# ---------------------------------------------------------------------------
# SensitiveFileChecker
# ---------------------------------------------------------------------------


class SensitiveFileChecker:
    """敏感文件路径检查器。

    使用 glob 模式匹配判断路径是否应被拒绝访问。
    默认 denylist 覆盖 .env、私钥、凭证、Git 内部配置等。

    匹配逻辑：

    1. 先检查例外模式（``!`` 前缀）：匹配即允许（白名单覆盖）
    2. 再检查拒绝模式：匹配即拒绝
    3. 都不匹配则允许

    Args:
        extra_deny: 额外的拒绝模式列表（追加到默认列表之后）。
        extra_allow: 额外的允许模式列表（优先级高于拒绝列表）。
    """

    # 默认模式列表（类级别，供外部参考）
    DEFAULT_PATTERNS: ClassVar[list[str]] = [
        p for p in _DEFAULT_DENY_PATTERNS if not p.startswith("!")
    ]

    def __init__(
        self,
        extra_deny: list[str] | None = None,
        extra_allow: list[str] | None = None,
    ) -> None:
        self._rules: list[tuple[str, bool]] = list(_DEFAULT_DENY_GLOB)

        # 追加例外规则（优先级高）
        if extra_allow:
            for pattern in extra_allow:
                self._rules.append((pattern, True))

        # 追加拒绝规则（优先级次于例外）
        if extra_deny:
            for pattern in extra_deny:
                self._rules.append((pattern, False))

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def is_sensitive(self, path: str) -> bool:
        """检查 *path* 是否匹配敏感文件模式。

        Args:
            path: 工程相对路径（POSIX 风格，如 ``"config/.env"``）。

        Returns:
            ``True`` 如果路径应被拒绝访问。
        """
        # 规范化路径：归一化 .. 段（防止 ../ 绕过目录前缀模式）
        # 并统一小写（macOS APFS 大小写不敏感，防止 .ENV 绕过 .env）
        normalized = PurePosixPath(path).as_posix()
        # 用 os.path.normpath 解析纯词法的 .. 段（不访问文件系统）
        normalized = os.path.normpath(normalized)
        # 归一化后仍以 .. 开头 → 路径越过工程根，直接拒绝
        if normalized.startswith(".."):
            return True
        lower = normalized.lower()

        # 检查单个组件是否匹配模式
        def _match(pattern: str, p: str) -> bool:
            pattern_lower = pattern.lower()
            # 先匹配全路径（小写）
            if fnmatch.fnmatch(p, pattern_lower):
                return True
            # 再匹配每个路径组件（含自身，小写）
            parts = PurePosixPath(p).parts
            return any(fnmatch.fnmatch(part, pattern_lower) for part in parts)

        # 第一轮：先检查例外模式。任一例外匹配 → 直接允许
        for pattern, is_allow in self._rules:
            if is_allow and _match(pattern, lower):
                return False

        # 第二轮：再检查拒绝模式。任一拒绝匹配 → 拒绝
        for pattern, is_allow in self._rules:
            if not is_allow and _match(pattern, lower):
                return True

        # 无匹配 → 允许
        return False

    # ------------------------------------------------------------------
    # 类方法
    # ------------------------------------------------------------------

    def sensitive_files(self) -> list[str]:
        """返回当前生效的拒绝模式列表（仅拒绝规则，不含例外）。"""
        return [p for p, is_allow in self._rules if not is_allow]

    def allowed_exceptions(self) -> list[str]:
        """返回当前生效的例外模式列表。"""
        return [p for p, is_allow in self._rules if is_allow]


# ---------------------------------------------------------------------------
# 模块级快捷函数
# ---------------------------------------------------------------------------

_checker: SensitiveFileChecker | None = None


def set_checker(checker: SensitiveFileChecker) -> None:
    """设置全局 ``SensitiveFileChecker`` 实例。

    Args:
        checker: 已初始化的敏感文件检查器。
    """
    global _checker
    _checker = checker


def get_checker() -> SensitiveFileChecker:
    """获取全局 ``SensitiveFileChecker`` 实例。

    Raises:
        RuntimeError: 未调用 ``set_checker`` 时。
    """
    if _checker is None:
        raise RuntimeError(
            "SensitiveFileChecker not initialised — call set_checker() first"
        )
    return _checker


def is_sensitive(path: str) -> bool:
    """快捷方式：检查 *path* 是否敏感。

    Args:
        path: 工程相对路径。

    Returns:
        ``True`` 如果路径应被拒绝访问。
    """
    return get_checker().is_sensitive(path)
