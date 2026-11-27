"""工程概要与构建清单发现工具。

提供 ``cat_project_info`` MCP 只读工具，快速识别项目语言、
构建清单类型和重要文档名称。启发式扫描，不执行编译或脚本。
"""
# 依据: CAT-29

from __future__ import annotations

import logging
import os
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

logger = logging.getLogger(__name__)


# 构建清单特征（文件名 → 语言/框架）
_BUILD_FILE_SIGNATURES: dict[str, tuple[str, str]] = {
    "pyproject.toml": ("Python", "Poetry/PEP 621"),
    "setup.py": ("Python", "setuptools"),
    "setup.cfg": ("Python", "setuptools"),
    "requirements.txt": ("Python", "pip"),
    "Pipfile": ("Python", "pipenv"),
    "Cargo.toml": ("Rust", "Cargo"),
    "Cargo.lock": ("Rust", "Cargo"),
    "package.json": ("JavaScript/TypeScript", "npm/yarn"),
    "yarn.lock": ("JavaScript/TypeScript", "yarn"),
    "pnpm-lock.yaml": ("JavaScript/TypeScript", "pnpm"),
    "go.mod": ("Go", "Go Modules"),
    "go.sum": ("Go", "Go Modules"),
    "pom.xml": ("Java", "Maven"),
    "build.gradle": ("Java/Kotlin", "Gradle"),
    "build.gradle.kts": ("Kotlin", "Gradle Kotlin DSL"),
    "Gemfile": ("Ruby", "Bundler"),
    "composer.json": ("PHP", "Composer"),
    "Makefile": ("Generic", "Make"),
    "CMakeLists.txt": ("C/C++", "CMake"),
    "pubspec.yaml": ("Dart", "pub"),
    "mix.exs": ("Elixir", "Mix"),
    "Project.toml": ("Julia", "Pkg"),
    "stack.yaml": ("Haskell", "Stack"),
    "*.csproj": (".NET", "MSBuild"),
}

# 重要文档名
_IMPORTANT_DOCS = frozenset({
    "README.md", "README", "README.rst", "README.txt",
    "CONTRIBUTING.md", "CHANGELOG.md", "CHANGELOG",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "SECURITY.md", "CODE_OF_CONDUCT.md",
    "AGENTS.md", "CLAUDE.md",
})


def _register_cat_project_info_tools(
    server: Any,
    workspace_mgr: Any,
    state: Any,
) -> None:
    """注册 ``cat_project_info`` 工具。"""
    from cat_mcp.server import cat_tool
    from cat_mcp.projects import get_current_subject

    @cat_tool(
        server,
        name="cat_project_info",
        description=(
            "快速分析工程的技术栈、构建工具和关键文档。"
            "仅做有界扫描，不执行任何脚本或编译命令。"
            "返回语言估计、构建清单路径、关键文档列表和 Git 概况。"
        ),
        read_only=True,
    )
    async def project_info_handler(
        workspace_id: str,
    ) -> dict[str, Any]:
        """分析工程概要和构建清单。"""
        subject = get_current_subject()
        workspace = workspace_mgr.get_workspace(subject, workspace_id)
        project_id = workspace.get("project_id", "")

        project = state.get_project(project_id)
        if project is None:
            raise CatError(ErrorCode.PROJECT_NOT_FOUND, "Project not found")

        project_root: str = project.get("root_path", "")
        if not project_root:
            raise CatError(ErrorCode.INTERNAL_ERROR, "Project root not configured")

        # 扫描项目根目录（仅一层，不递归）
        try:
            entries = os.listdir(project_root)
        except OSError as exc:
            raise CatError(ErrorCode.OPERATION_FAILED, f"Cannot list project root")

        found_build: list[dict[str, str]] = []
        found_docs: list[str] = []
        languages: set[str] = set()

        for entry in entries:
            lower = entry.lower()
            # 构建清单识别
            if entry in _BUILD_FILE_SIGNATURES:
                lang, framework = _BUILD_FILE_SIGNATURES[entry]
                found_build.append({
                    "file": entry,
                    "language": lang,
                    "framework": framework,
                })
                languages.add(lang)
            # 通配匹配（如 *.csproj）
            if entry.endswith(".csproj"):
                found_build.append({
                    "file": entry,
                    "language": ".NET",
                    "framework": "MSBuild",
                })
                languages.add(".NET")

            # 重要文档
            if entry in _IMPORTANT_DOCS:
                found_docs.append(entry)

        # Git 概况
        git_dir = os.path.join(project_root, ".git")
        git_available = os.path.isdir(git_dir)

        # 语言推测
        if not languages and found_build:
            languages.add(found_build[0].get("language", "Unknown"))
        language_estimate = list(languages) if languages else ["Unknown"]

        return {
            "language_estimate": language_estimate,
            "build_files": found_build,
            "key_documents": found_docs,
            "git_available": git_available,
        }
