"""Git 只读状态和差异查询工具。

提供 ``cat_git_status`` 和 ``cat_git_diff`` 两个 MCP 只读工具，
安全地执行 Git 状态和差异查询，不修改 Git 仓库状态。

安全约束：
- 固定 argv 数组（无 shell 注入）
- diff target 白名单限制（None / "cached" / "HEAD"，拒绝任意 refspec）
- 禁用外部 diff/pager 及危险 Git 配置（-c 覆盖）
- subprocess 超时控制
- 路径内容经 ACL/SensitiveFileChecker 过滤
- 非 Git 目录明确报告
- 禁止 commit/push 等写操作
"""
# 依据: CAT-30

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Git 安全前缀命令
# ---------------------------------------------------------------------------

# 固定前缀 argv，禁用外部 diff、pager、交互式提示和危险别名
# -c core.pager=cat              → 强制分页器为 cat（纯文本输出）
# -c core.autocrlf=false         → 禁止自动 CRLF 转换，保持原始内容
# -c core.safecrlf=false         → 不阻止 CRLF 转换警告（只读不影响）
# -c core.quotepath=false        → 输出原始 UTF-8 路径（不转义非 ASCII）
# -c diff.external=              → 禁用外部 diff 工具
# -c diff.textconv=false         → 禁用 textconv 转换
# -c protocol.file.allow=never   → 禁止 file:// 协议（安全加固）
_GIT_SAFE_PREFIX: list[str] = [
    "git",
    "-c", "core.pager=cat",
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "core.quotepath=false",
    "-c", "diff.external=",
    "-c", "diff.textconv=false",
    "-c", "protocol.file.allow=never",
    "--no-replace-objects",
]

# Subprocess 默认超时（秒）
_GIT_DEFAULT_TIMEOUT = 30

# Git 环境：仅传播 PATH，防止 GIT_DIR/GIT_SSH 等环境变量泄露
_GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin")}


def _run_git(
    args: list[str],
    cwd: str,
    timeout: int = _GIT_DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """安全执行 Git 命令并返回结果。

    安全措施：
    - 用 ``os.path.realpath`` 在函数入口重新解析 cwd，降低 TOCTOU 窗口
    - 固定 argv 数组 + ``env=_GIT_ENV``（仅传播 PATH）
    - ``stdin=subprocess.DEVNULL`` 防止 Git 读取 stdin

    Args:
        args: Git 子命令参数（不含安全前缀）。
        cwd: 工作目录（Git 仓库根）。
        timeout: 命令超时秒数。

    Returns:
        subprocess.CompletedProcess 对象。

    Raises:
        CatError: 当命令超时、执行失败或目录不是 Git 仓库时。
    """
    # 重新解析 cwd 以降低 TOCTOU 窗口
    resolved_cwd = os.path.realpath(cwd)

    cmd = _GIT_SAFE_PREFIX + args

    try:
        result = subprocess.run(
            cmd,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_GIT_ENV,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise CatError(
            ErrorCode.OPERATION_TIMEOUT,
            f"Git command timed out after {timeout}s",
        )
    except FileNotFoundError:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            "Git 不可用或未安装",
        )
    except OSError as exc:
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            f"Git 执行失败: {exc}",
        )

    if result.returncode != 0:
        # 非零退出码：记录详细 stderr 到日志，返回脱敏消息给客户端
        _stderr = result.stderr.strip()
        logger.warning(
            "Git command %r in %r failed (exit %d): %s",
            args,
            resolved_cwd,
            result.returncode,
            _stderr,
        )
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            "Git 命令执行失败",
        )

    return result


def _check_is_git_repo(cwd: str) -> str:
    """验证 cwd 是 Git 工作树根目录，返回 resolved 路径。

    合并安全检查：git-dir 验证 + .git 内部拒绝 + cwd 重新解析，
    降低 TOCTOU 窗口。

    Args:
        cwd: 待检查的目录。

    Returns:
        解析后的绝对路径。

    Raises:
        CatError: 不是 Git 仓库或在 .git 目录内。
    """
    resolved_cwd = os.path.realpath(cwd)

    # 1. 检查是否是 Git 仓库
    try:
        r = subprocess.run(
            _GIT_SAFE_PREFIX + ["rev-parse", "--git-dir"],
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=10,
            env=_GIT_ENV,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            "Git 不可用或未安装",
        )

    if r.returncode != 0 or not r.stdout.strip():
        raise CatError(
            ErrorCode.OPERATION_FAILED,
            "非 Git 仓库或不在 Git 工作树内",
        )

    # 2. 检查是否在 .git 目录内部
    try:
        r2 = subprocess.run(
            _GIT_SAFE_PREFIX + ["rev-parse", "--is-inside-git-dir"],
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=10,
            env=_GIT_ENV,
            stdin=subprocess.DEVNULL,
        )
        if r2.returncode == 0 and r2.stdout.strip() == "true":
            raise CatError(
                ErrorCode.PATH_OUT_OF_BOUNDS,
                "禁止访问 .git 内部目录",
            )
    except subprocess.TimeoutExpired:
        pass

    return resolved_cwd


def _is_sensitive_path(
    path: str,
    project_root: str,
) -> bool:
    """检查路径是否属于敏感文件，保守拒绝模式。

    安全检查失败时默认返回 True（拒绝），而非放行。

    Args:
        path: 相对路径。
        project_root: 项目根目录。

    Returns:
        True 如果路径属于敏感类别或安全检查不可用。
    """
    # macOS APFS 默认不区分大小写，统一转小写检查
    lower_path = path.lower()

    # .git 目录内任何内容都拒绝
    if lower_path.startswith(".git") or lower_path.startswith("/.git"):
        return True

    # 子模块 .git 文件（指向 ../.git/modules/...）
    if "/.git" in lower_path:
        return True

    # 使用 SensitiveFileChecker 进行安全检查
    try:
        from cat_mcp.fs.sensitive import is_sensitive

        full_path = os.path.join(project_root, path)
        return is_sensitive(full_path)
    except ImportError:
        # 缺少敏感模块时保守拒绝
        return True
    except Exception:
        logger.exception("Sensitive file check failed for %s, denying access", path)
        return True


def _validate_diff_target(target: str | None) -> None:
    """验证 diff target 参数是否在安全白名单内。

    仅允许 None（工作树 vs 暂存区）、"cached"（暂存区 vs HEAD）、
    "HEAD"（工作树 vs HEAD）。拒绝任意 refspec 防止参数注入。

    Args:
        target: 待验证的 target 参数。

    Raises:
        CatError: 如果 target 不在白名单内。
    """
    allowed = (None, "cached", "HEAD")
    if target not in allowed:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            f"diff target 必须为 None/'cached'/'HEAD' 之一，收到: {target!r}",
        )


# ---------------------------------------------------------------------------
# cat_git_status — 获取 Git 仓库状态
# ---------------------------------------------------------------------------


def _get_git_status(
    cwd: str,
    project_root: str,
    *,
    include_untracked: bool = True,
) -> dict[str, Any]:
    """获取 Git 仓库的状态信息。

    使用 ``git status --porcelain=v2 -z`` 获取机器可读的状态输出，
    解析为结构化数据返回。

    Args:
        cwd: 工作目录（Git 仓库工作树根）。
        project_root: 项目根目录（用于路径安全检查）。
        include_untracked: 是否包含未跟踪文件。

    Returns:
        包含状态信息的字典。
    """
    resolved_cwd = _check_is_git_repo(cwd)

    args = ["status", "--porcelain=v2", "-z"]
    if not include_untracked:
        args.append("--untracked-files=no")

    result = _run_git(args, cwd=resolved_cwd)
    raw_output = result.stdout

    # 解析 -z 分隔的 NUL 输出
    lines = raw_output.split("\0") if raw_output else []

    # 获取当前分支和 HEAD 信息
    branch_info = _get_branch_info(resolved_cwd)

    entries: list[dict[str, Any]] = []
    untracked_count = 0
    has_conflicts = False
    staged_count = 0
    unstaged_count = 0

    for line in lines:
        if not line:
            continue

        entry = _parse_porcelain_v2_line(line, project_root)
        if entry is None:
            continue

        # 未跟踪文件单独计数
        if entry.get("status") == "untracked":
            untracked_count += 1
            if include_untracked:
                entries.append(entry)
            continue

        entries.append(entry)

        # 统计暂存/未暂存
        if entry.get("staged"):
            staged_count += 1
        if entry.get("unstaged"):
            unstaged_count += 1

        # 检测冲突
        if entry.get("status") in (
            "conflict",
            "added_by_us", "added_by_them",
            "deleted_by_us", "deleted_by_them",
            "both_modified_conflict", "both_added_conflict", "both_deleted_conflict",
        ):
            has_conflicts = True

    return {
        "branch": branch_info["branch"],
        "head_commit": branch_info["head_commit"],
        "head_commit_short": branch_info["head_commit_short"],
        "ahead": branch_info.get("ahead", 0),
        "behind": branch_info.get("behind", 0),
        "clean": len(entries) == 0,
        "has_conflicts": has_conflicts,
        "staged_count": staged_count,
        "unstaged_count": unstaged_count,
        "untracked_count": untracked_count,
        "entries": entries,
    }


def _get_branch_info(cwd: str) -> dict[str, Any]:
    """获取当前分支和 HEAD 信息。

    Args:
        cwd: Git 工作目录。

    Returns:
        包含分支名称、HEAD 提交哈希等信息的字典。
    """
    info: dict[str, Any] = {
        "branch": None,
        "head_commit": None,
        "head_commit_short": None,
    }

    try:
        r = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=cwd)
        info["branch"] = r.stdout.strip()
    except CatError:
        info["branch"] = None

    try:
        r = _run_git(["rev-parse", "HEAD"], cwd=cwd)
        info["head_commit"] = r.stdout.strip()

        r2 = _run_git(["rev-parse", "--short", "HEAD"], cwd=cwd)
        info["head_commit_short"] = r2.stdout.strip()
    except CatError:
        pass

    try:
        r = _run_git(
            ["rev-list", "--count", "--left-right", "HEAD...@{upstream}"],
            cwd=cwd,
        )
        counts = r.stdout.strip().split("\t")
        if len(counts) == 2:
            info["ahead"] = int(counts[0].strip()) if counts[0].strip() else 0
            info["behind"] = int(counts[1].strip()) if counts[1].strip() else 0
    except (CatError, ValueError):
        info["ahead"] = 0
        info["behind"] = 0

    return info


def _parse_porcelain_v2_line(
    line: str,
    project_root: str,
) -> dict[str, Any] | None:
    """解析单行 ``git status --porcelain=v2`` 输出。

    Porcelain v2 格式（NUL 已分割）：
    - 普通条目：``1 XY sub mH mI mW hH hI <path>``
    - 重命名/复制：``2 XY sub mH mI mW hH hI Xscore <path> <origPath>``
    - 未跟踪：``? <path>``

    注意：path 是最后字段，可能有空格。使用字段数计算正确起始位置。

    Args:
        line: 一行 porcelain v2 输出（NUL 已分割）。
        project_root: 项目根目录。

    Returns:
        解析后的条目字典，如果跳过则返回 None。
    """
    if not line or len(line) < 2:
        return None

    prefix = line[0]

    # 未跟踪文件
    if prefix == "?":
        path = line[2:]
        if _is_sensitive_path(path, project_root):
            return None
        return {
            "path": path,
            "status": "untracked",
            "staged": False,
            "unstaged": False,
        }

    # 忽略文件（不返回）
    if prefix == "!":
        return None

    parts = line.split(" ")

    # 普通条目（1 XY sub mH mI mW hH hI path）
    if prefix == "1":
        if len(parts) < 8:
            return None
        xy = parts[1]
        # Porcelain v2 格式中 path 是第 8 个字段（索引 7）开始后的全部内容
        # 固定字段 8 个：1 + XY + sub + mH + mI + mW + hH + hI = 8
        _FIXED_FIELDS = 8
        if len(parts) > _FIXED_FIELDS:
            path = " ".join(parts[_FIXED_FIELDS:])
        else:
            path = parts[7] if len(parts) > 7 else ""

        if _is_sensitive_path(path, project_root):
            return None

        staged = xy[0] != "."
        unstaged = xy[1] != "."

        entry: dict[str, Any] = {
            "path": path,
            "xy_status": xy,
            "status": _xy_to_description(xy),
            "staged": staged,
            "unstaged": unstaged,
            "x": xy[0],
            "y": xy[1],
            "submodule_state": parts[2] if len(parts) > 2 else None,
        }

        # 文件模式（mH, mI, mW）
        if len(parts) > 3:
            entry["head_mode"] = parts[3]
        if len(parts) > 4:
            entry["index_mode"] = parts[4]
        if len(parts) > 5:
            entry["worktree_mode"] = parts[5]

        # 对象哈希
        if len(parts) > 6:
            entry["head_hash"] = parts[6]
        if len(parts) > 7:
            entry["index_hash"] = parts[7]

        return entry

    # 重命名/复制条目（2 XY sub mH mI mW hH hI Xscore path origPath）
    if prefix == "2":
        if len(parts) < 9:
            return None
        xy = parts[1]
        score = parts[8]

        # path 和 origPath 从固定字段后开始
        _FIXED_FIELDS_RENAME = 9
        if len(parts) > _FIXED_FIELDS_RENAME:
            path = " ".join(parts[_FIXED_FIELDS_RENAME:])
            orig_path = None
            # origPath 在 path 之后，用长度判断
            # 简单处理：path 和 origPath 通过已知字段数分割
            _ORIG_FIELDS = 2  # path + origPath
            remaining = parts[_FIXED_FIELDS_RENAME:]
            if len(remaining) >= 2:
                path = " ".join(remaining[:-1])  # 假设最后一个是 origPath
                orig_path = remaining[-1]
            else:
                path = remaining[0] if remaining else ""
                orig_path = None
        else:
            path = parts[7] if len(parts) > 7 else ""
            orig_path = parts[8] if len(parts) > 8 else None

        if _is_sensitive_path(path, project_root):
            return None
        if orig_path and _is_sensitive_path(orig_path, project_root):
            return None

        staged = xy[0] != "."
        unstaged = xy[1] != "."

        entry = {
            "path": path,
            "xy_status": xy,
            "status": _xy_to_description(xy),
            "staged": staged,
            "unstaged": unstaged,
            "x": xy[0],
            "y": xy[1],
            "rename_score": score,
            "submodule_state": parts[2] if len(parts) > 2 else None,
        }
        if orig_path:
            entry["original_path"] = orig_path
        return entry

    return None


def _xy_to_description(xy: str) -> str:
    """将 Git 的 XY 状态码映射为人类可读的描述。

    Args:
        xy: 两位状态码（第一位=暂存区/HEAD，第二位=工作树/暂存区）。

    Returns:
        描述字符串。
    """
    _MAP: dict[str, str] = {
        "  ": "unmodified",
        " M": "modified_unstaged",
        " T": "type_changed_unstaged",
        " D": "deleted_unstaged",
        " R": "renamed_unstaged",
        " C": "copied_unstaged",
        "M ": "modified_staged",
        "T ": "type_changed_staged",
        "D ": "deleted_staged",
        "R ": "renamed_staged",
        "C ": "copied_staged",
        "MM": "modified_staged_and_unstaged",
        "MT": "modified_staged_type_changed",
        "MD": "modified_staged_deleted",
        "AM": "added_modified",
        "AD": "added_deleted",
        "A ": "added_staged",
        "RM": "renamed_modified",
        "RD": "renamed_deleted",
        "CM": "copied_modified",
        "CD": "copied_deleted",
        "UU": "both_modified_conflict",
        "AA": "both_added_conflict",
        "DD": "both_deleted_conflict",
        "AU": "added_by_us",
        "UD": "deleted_by_them",
        "UA": "added_by_them",
        "DU": "deleted_by_us",
        "??": "untracked",
        "!!": "ignored",
    }
    return _MAP.get(xy, f"unknown_{xy}")


# ---------------------------------------------------------------------------
# cat_git_diff — 获取 Git 差异内容
# ---------------------------------------------------------------------------


def _get_git_diff(
    cwd: str,
    project_root: str,
    *,
    target: str | None = None,
    path_filter: str | None = None,
    context_lines: int = 3,
    max_size: int = 10 * 1024 * 1024,  # 10MB
) -> dict[str, Any]:
    """获取 Git 差异内容。

    使用 ``git diff --no-ext-diff --no-textconv`` 安全获取纯文本差异，
    禁用外部 diff 工具和 textconv 转换。

    target 仅接受白名单值（None/"cached"/"HEAD"），拒绝任意 refspec。

    Args:
        cwd: Git 仓库工作树根。
        project_root: 项目根目录（用于路径安全检查）。
        target: 差异目标（白名单限制）。
        path_filter: 可选的路径过滤参数（传给 ``git diff -- <path>``）。
        context_lines: 上下文行数。
        max_size: 差异输出最大字节数。

    Returns:
        包含差异信息的字典。

    Raises:
        CatError: 参数无效或命令失败时。
    """
    _validate_diff_target(target)

    if context_lines < 0 or context_lines > 1000:
        raise CatError(
            ErrorCode.INVALID_ARGUMENT,
            "context_lines 必须在 0-1000 之间",
        )

    resolved_cwd = _check_is_git_repo(cwd)

    args: list[str] = []

    if target is None:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    elif target == "cached":
        args = ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color"]
    elif target == "HEAD":
        args = ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color"]

    if context_lines != 3:
        args.extend(["-U", str(context_lines)])

    if path_filter:
        args.extend(["--", path_filter])

    result = _run_git(args, cwd=resolved_cwd)
    raw_output = result.stdout

    if len(raw_output.encode("utf-8")) > max_size:
        raise CatError(
            ErrorCode.RESPONSE_TOO_LARGE,
            f"差异输出超过最大限制 ({max_size} 字节)",
        )

    files = _parse_diff_output(raw_output, project_root)
    stats = _get_diff_stat(resolved_cwd, target, path_filter)

    return {
        "files": files,
        "total_files": len(files),
        "stats": stats,
    }


def _parse_diff_output(
    output: str,
    project_root: str,
) -> list[dict[str, Any]]:
    """解析 ``git diff`` 输出为结构化的逐文件差异列表。

    解析 unified diff 格式，提取每个文件的变更行和块信息。
    对含空格的路径，使用正则从 ``diff --git a/... b/...`` 中提取。

    Args:
        output: git diff 原始输出。
        project_root: 项目根目录。

    Returns:
        结构化差异条目列表。
    """
    if not output:
        return []

    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None
    current_hunks: list[dict[str, Any]] = []
    current_hunk: dict[str, Any] | None = None
    current_lines: list[str] = []

    for line in output.split("\n"):
        if line.startswith("diff --git "):
            if current_file is not None:
                if current_hunk is not None:
                    current_hunk["lines"] = current_lines
                    current_hunks.append(current_hunk)
                    current_lines = []
                current_file["hunks"] = current_hunks
                file_path = current_file.get("path", "")
                if not _is_sensitive_path(file_path, project_root):
                    files.append(current_file)
                current_lines = []
                current_hunks = []
                current_hunk = None

            # 用正则提取 a/b 路径，支持含空格的路径
            m = _DIFF_HEADER_RE.match(line)
            if m:
                a_path = m.group(1) or ""
                b_path = m.group(2) or ""
            else:
                a_path = ""
                b_path = ""

            path = b_path or a_path

            current_file = {
                "path": path,
                "a_path": a_path,
                "b_path": b_path,
                "hunks": [],
                "lines_added": 0,
                "lines_deleted": 0,
                "binary": False,
            }
            continue

        if line.startswith("new file mode "):
            if current_file is not None:
                current_file["new_file"] = True
            continue

        if line.startswith("deleted file mode "):
            if current_file is not None:
                current_file["deleted_file"] = True
            continue

        if line.startswith("old mode "):
            if current_file is not None:
                current_file["old_mode"] = line[9:]
            continue
        if line.startswith("new mode "):
            if current_file is not None:
                current_file["new_mode"] = line[9:]
            continue

        if line.startswith("rename from "):
            if current_file is not None:
                current_file["rename_from"] = line[12:]
            continue
        if line.startswith("rename to "):
            if current_file is not None:
                current_file["rename_to"] = line[10:]
            continue

        if line.startswith("similarity index "):
            if current_file is not None:
                current_file["similarity"] = line[17:]
            continue

        if line.startswith("index "):
            continue

        if line.startswith("Binary files "):
            if current_file is not None:
                current_file["binary"] = True
            continue

        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        if line.startswith("@@"):
            if current_hunk is not None:
                current_hunk["lines"] = current_lines
                current_hunks.append(current_hunk)
                current_lines = []

            hunk_match = _HUNK_HEADER_RE.match(line)
            if hunk_match:
                old_start = int(hunk_match.group(1))
                old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                new_start = int(hunk_match.group(3))
                new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
                section_text = hunk_match.group(5).strip() if hunk_match.group(5) else ""

                current_hunk = {
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                    "section": section_text,
                    "lines": [],
                }
            else:
                current_hunk = {
                    "old_start": 0,
                    "old_count": 0,
                    "new_start": 0,
                    "new_count": 0,
                    "section": line,
                    "lines": [],
                }
            continue

        if line.startswith("+") or line.startswith("-") or line.startswith(" "):
            if current_hunk is not None:
                current_lines.append(line)
                if line.startswith("+"):
                    if current_file is not None:
                        current_file["lines_added"] += 1
                elif line.startswith("-"):
                    if current_file is not None:
                        current_file["lines_deleted"] += 1
            continue

        if line.startswith("\\ "):
            if current_hunk is not None:
                current_lines.append(line)
            continue

    if current_file is not None:
        if current_hunk is not None:
            current_hunk["lines"] = current_lines
            current_hunks.append(current_hunk)
        current_file["hunks"] = current_hunks
        file_path = current_file.get("path", "")
        if not _is_sensitive_path(file_path, project_root):
            files.append(current_file)

    return files


# diff --git a/<path> b/<path>  正则，支持含空格的路径
_DIFF_HEADER_RE = re.compile(
    r"^diff --git a/(.+?) b/(.+?)$"
)

# Hunk 头正则：@@ -start,count +start,count @@ [section]
_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(?:\s+(.*))?$"
)


def _get_diff_stat(
    cwd: str,
    target: str | None,
    path_filter: str | None,
) -> dict[str, Any]:
    """获取差异的统计信息（文件变更数、插入/删除行数）。

    返回全零统计而不传播异常，因为 diff --numstat 失败不影响主差异输出。

    Args:
        cwd: Git 工作目录。
        target: 差异目标。
        path_filter: 可选路径过滤。

    Returns:
        统计信息字典。
    """
    try:
        args: list[str] = ["diff", "--numstat"]

        if target == "cached":
            args = ["diff", "--cached", "--numstat"]
        elif target == "HEAD":
            args = ["diff", "HEAD", "--numstat"]

        if path_filter:
            args.extend(["--", path_filter])

        result = _run_git(args, cwd=cwd)
        raw = result.stdout.strip()

        if not raw:
            return {"files_changed": 0, "insertions": 0, "deletions": 0}

        lines = raw.split("\n")
        total_added = 0
        total_deleted = 0

        for line in lines:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    total_added += int(parts[0]) if parts[0] != "-" else 0
                    total_deleted += int(parts[1]) if parts[1] != "-" else 0
                except ValueError:
                    pass

        return {
            "files_changed": len([l for l in lines if l.strip()]),
            "insertions": total_added,
            "deletions": total_deleted,
        }
    except CatError:
        return {"files_changed": 0, "insertions": 0, "deletions": 0}


# ---------------------------------------------------------------------------
# MCP 工具注册
# ---------------------------------------------------------------------------


def _register_cat_git_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 Git 只读工具（``cat_git_status`` 和 ``cat_git_diff``）。

    Args:
        server: MCPServer 实例。
        workspace_mgr: WorkspaceManager 实例。
        state: StateManager 实例。
    """
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    # ------------------------------------------------------------------
    # cat_git_status
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_git_status",
        description=(
            "获取 Git 仓库完整状态，包括分支、未跟踪文件、暂存变更和冲突。"
            "使用 ``status --porcelain=v2 -z`` 输出并解析为结构化数据。"
            "安全约束：禁外部 diff/pager、固定 argv、敏感路径过滤。"
            "非 Git 仓库或 .git 目录明确拒绝。只读，不修改任何状态。"
        ),
        read_only=True,
    )
    async def git_status_handler(
        workspace_id: str,
        *,
        include_untracked: bool = True,
    ) -> dict[str, Any]:
        """获取 Git 仓库状态。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "项目根目录未配置")

        return _get_git_status(
            cwd=project_root,
            project_root=project_root,
            include_untracked=include_untracked,
        )

    # ------------------------------------------------------------------
    # cat_git_diff
    # ------------------------------------------------------------------

    @cat_tool(
        server,
        name="cat_git_diff",
        description=(
            "获取 Git 差异输出（unified diff 格式）。"
            "支持工作树 vs 暂存区、暂存区 vs HEAD、工作树 vs HEAD 三种模式。"
            "target 限制为白名单 None/'cached'/'HEAD'。"
            "禁外部 diff/textconv、固定 argv、敏感路径过滤。"
            "非 Git 仓库或 .git 目录明确拒绝。只读，不修改任何状态。"
        ),
        read_only=True,
    )
    async def git_diff_handler(
        workspace_id: str,
        *,
        target: str | None = None,
        path_filter: str | None = None,
        context_lines: int = 3,
    ) -> dict[str, Any]:
        """获取 Git 差异内容。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "项目根目录未配置")

        return _get_git_diff(
            cwd=project_root,
            project_root=project_root,
            target=target,
            path_filter=path_filter,
            context_lines=context_lines,
        )
