# Cat — 远程软件工程工作空间 MCP 服务器

**Cat** 是一个远程软件工程工作空间 MCP 服务器。它使经授权的 AI 编程助手能够通过模型上下文协议（MCP）直接、确定性地读写远程服务器上的工程目录。

Cat 不运行 AI 模型、不替代 IDE 或版本控制工具、不执行任意 Shell 命令。它是一个专注于文件系统服务的工具，为真实的 AI 辅助软件工程工作流而设计。

## 规划特性

- 工程注册与发现
- 目录浏览（分页、过滤）
- 单文件读取（行范围、字节范围）
- 精确文件编辑（文本匹配、行范围、统一 diff）
- 并发工作空间隔离与版本化 CWD
- 搜索（可选 ripgrep + 标准库回退）
- 审计与变更历史
- 基于 OAuth2 的认证与授权

## 快速开始

### 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理器

### 安装

```bash
# 克隆仓库
git clone https://github.com/username/cat.git
cd cat

# 创建虚拟环境并安装依赖
uv sync

# 运行测试
uv run pytest

# 启动服务器（开发中）
uv run cat-mcp --config config.example.toml
```

## 许可

本项目使用 MIT 许可证发布，详见 [LICENSE](LICENSE) 文件。
