# 第三方许可声明 / Third-Party Notices

Cat MCP 服务器依赖于以下开源项目。此处列出其许可信息。

## Python Runtime Dependencies

| 依赖 | 许可证 | 用途 |
|------|--------|------|
| [mcp (Python SDK)](https://github.com/modelcontextprotocol/python-sdk) | MIT | MCP 协议实现 |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | 数据验证 |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI 服务器 |
| [starlette](https://github.com/encode/starlette) | BSD-3-Clause | ASGI 框架 |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | HTTP 客户端 |
| [pathspec](https://github.com/cpburnz/python-path-specification) | MPL-2.0 | Gitignore 模式匹配 |

## Development Dependencies

| 依赖 | 许可证 | 用途 |
|------|--------|------|
| [pytest](https://github.com/pytest-dev/pytest) | MIT | 测试框架 |
| [ruff](https://github.com/astral-sh/ruff) | MIT | 代码检查 |
| [hatchling](https://github.com/pypa/hatchling) | MIT | 构建系统 |

## Reference Implementations (Inspiration Only)

以下项目为本项目的设计参考，但**未直接移植其代码**（如直接移植，须按原许可证要求标注）：

| 项目 | 许可证 | 参考内容 |
|------|--------|---------|
| [Muvon/octofs](https://github.com/Muvon/octofs) | Apache-2.0 | Git 路径解析理念对照 |
| [j0hanz/filesystem-mcp](https://github.com/j0hanz/filesystem-mcp) | MIT | 文件操作模式参考 |
| [njbrake/filesystem-mcp-server](https://github.com/njbrake/filesystem-mcp-server) | MIT | Docker 构建参考 |
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | MIT | 官方 MCP 服务参考 |
| [desktop-commander/remote-desktop-commander](https://github.com/desktop-commander/remote-desktop-commander) | MIT | 远端桌面参考 |

## Docker Base Images

| 镜像 | 许可证 | 用途 |
|------|--------|------|
| python:3.12-slim | Python Software Foundation License | 构建/运行时 |
| caddy:alpine | Apache-2.0 | 反向代理 |

---

> 如有遗漏，请提交 Issue 或 PR 更新本文件。
