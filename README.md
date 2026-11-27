# Cat — Remote Software Engineering Workspace MCP Server

**Cat** 是一个远程软件工程工作空间 MCP 服务器。它授权 AI 编码助手通过
Model Context Protocol (MCP) 对远程服务器上的工程进行确定性的读写访问。

Cat 不运行 AI 模型，不替代 IDE 或版本控制工具，也不执行任意 shell 命令。
它是一个专注于实际 AI 辅助软件工程工作流的文件系统服务。

> ⚠️ **Alpha 阶段**：核心功能已就绪，公共 API 可能在正式版前调整。

---

## 功能特性

| 类别 | 功能 |
|------|------|
| 📂 **目录浏览** | 分层列表、稳定分页、Glob 过滤、敏感文件跳过 |
| 📄 **文件读取** | 分段文本/二进制读取、行范围、字节范围 |
| ✏️ **文件编辑** | 精确文本匹配替换、行范围替换、统一差异补丁 |
| 📝 **文件写入** | 创建新文件、替换已有文件、原子写入与崩溃恢复 |
| 📋 **目录操作** | 安全创建子目录（含递归模式） |
| 🔄 **移动/复制** | 文件和目录安全移动/重命名、文件复制（不覆盖） |
| 🗑️ **软删除** | 移入 `.cat_trash` 回收区，支持恢复 |
| 🔒 **安全访问** | OAuth2/JWT 认证、4 级 ACL（主体→工程→路径→动作） |
| 🚫 **敏感保护** | 可配置敏感文件/路径拒绝列表 |
| 🔐 **路径安全** | fd-relative 解析、符号链接验证、路径遍历防护 |
| 🔎 **全文搜索** | 集成 ripgrep（可选）、有界搜索、敏感过滤 |
| 🔍 **Git 只读** | 仓库状态查询（porcelain v2）和差异对比 |
| 📜 **变更历史** | 操作记录查询、文件版本差异对比 |
| 📤 **分块传输** | 超大文件分块上传（哈希校验、原子提交）和下载 |
| 📋 **工程概览** | 构建文件发现、语言/技术栈自动识别 |
| 📊 **审计日志** | 完整操作审计、失败/拒绝事件记录 |
| 🔄 **幂等操作** | 请求 ID 幂等、断线重连安全恢复 |
| 🚦 **配额控制** | 并发限制、响应大小限制、超时控制 |

---

## 快速开始

### 前提条件

- Python 3.12+
- Git（可选，用于 Git 工具）

### 安装

```bash
# 克隆仓库
git clone https://github.com/username/cat.git
cd cat

# 方式一：使用 pip
pip install -e .

# 方式二：使用 uv（推荐）
uv sync

# 验证安装
cat-mcp --help
```

### 原生安装（Linux/macOS）

提供安装、升级、回滚和卸载脚本：

```bash
# 安装到 /opt/cat-mcp
sudo ./install

# 或指定安装路径
./install --prefix=$HOME/.local/opt/cat-mcp

# 升级
./upgrade

# 回滚
./rollback --backup /opt/cat-mcp/backups/pre-YYYYMMDDHHMMSS

# 卸载（保留工程数据）
./uninstall

# 完全清除
./uninstall --purge
```

详见 [安装文档](#安装)。

### Docker 部署

```bash
# 构建并启动
docker compose up -d

# 查看日志
docker compose logs -f

# 停止
docker compose down
```

---

## 配置

Cat 使用 YAML 配置文件（默认路径 `/etc/cat/cat.yaml`）：

```yaml
# 服务器配置
host: "127.0.0.1"
port: 9100

# 认证
auth:
  enabled: true
  # JWT 密钥（生产环境必改）
  secret: "your-256-bit-secret"

# 项目管理
projects:
  - id: "my-project"
    display_name: "My Project"
    root: "/srv/projects/my-project"

# 授权规则
grants:
  - subject: "ai-agent"
    project_id: "my-project"
    scopes: "read,write"
    allowed_prefixes: "/"

# 限额
limits:
  directory_depth: 10
  page_entries: 100
  max_search_results: 500
  max_concurrent_per_subject: 5
```

---

## MCP 工具清单

### 只读工具

| 工具名 | 描述 |
|--------|------|
| `cat_list_directory` | 分层目录浏览与稳定分页 |
| `cat_read_file` | 文件读取（文本/二进制，偏移/范围） |
| `cat_search_text` | 有界全文搜索 |
| `cat_find_files` | 按名称/模式搜索文件 |
| `cat_project_info` | 工程概要与构建清单发现 |
| `cat_git_status` | Git 仓库状态（porcelain v2） |
| `cat_git_diff` | Git 差异对比（白名单 target） |
| `cat_list_changes` | 操作历史记录查询 |
| `cat_diff_file` | 文件版本差异或 Git 基准对比 |
| `cat_read_chunk` | 分块文件读取 |
| `cat_get_capabilities` | 服务器能力查询 |
| `cat_list_projects` | 工程列表 |
| `cat_get_operation` | 操作状态/结果查询 |

### 写入工具

| 工具名 | 描述 |
|--------|------|
| `cat_write_file` | 创建/替换文件（create/replace） |
| `cat_edit_file` | 精确局部编辑（replace_exact/行范围/插入） |
| `cat_apply_patch` | 统一差异补丁 |
| `cat_create_directory` | 安全创建子目录 |
| `cat_move_path` | 文件/目录移动和重命名 |
| `cat_copy_file` | 文件复制（不覆盖） |
| `cat_trash_path` | 软删除（移入回收区） |
| `cat_begin_upload` | 启动分块上传 |
| `cat_upload_chunk` | 上传单个数据块 |
| `cat_finish_upload` | 完成分块上传并原子提交 |
| `cat_abort_upload` | 中止分块上传 |

### 管理工具

| 工具名 | 描述 |
|--------|------|
| `cat_create_workspace` | 创建工作空间 |
| `cat_close_workspace` | 关闭工作空间 |
| `cat_change_cwd` | 切换工作目录 |
| `cat_query_audit` | 查询审计日志 |

---

## 安全特性

### 多层安全模型

```
客户端认证 (JWT/OAuth2)
  → 工程授权 (ACL)
    → 工作空间隔离
      → 路径解析 (fd-relative, 符号链接校验)
        → 敏感文件过滤 (全局拒绝列表)
          → 写入锁 (版本条件、并发控制)
            → 原子写入 (崩溃安全、回滚)
              → 操作审计 (全量记录)
```

### 安全约束

- **路径遍历防护**：所有路径通过 `SecurePathResolver` 解析，禁止逃逸项目根
- **敏感文件保护**：`.env`、`*.key`、`*.pem`、`.git` 内部等自动拒绝
- **命令执行保护**：Git 命令使用固定 argv 白名单，禁用外部 diff/pager
- **写入原子性**：所有写入操作使用 AtomicWriter 模式，崩溃后安全恢复
- **操作幂等性**：同一 `request_id` 重复请求安全处理，不会重复写入

---

## 架构

```
┌──────────────────────┐
│   AI 编码客户端       │  MCP 协议 (Streamable HTTP)
└─────────┬────────────┘
          │
┌─────────▼────────────┐
│   Caddy 反向代理      │  TLS, 鉴权, 安全头
└─────────┬────────────┘
          │
┌─────────▼────────────┐
│   Cat MCP Server      │  Starlette + MCPServer
│                        │
│  ┌──────────────────┐ │
│  │  ACL 授权         │ │  主体→工程→路径→动作
│  ├──────────────────┤ │
│  │  安全路径解析     │ │  fd-relative, O_NOFOLLOW
│  ├──────────────────┤ │
│  │  写入协调         │ │  锁→原子→幂等→审计
│  ├──────────────────┤ │
│  │  文件系统操作     │ │  读/写/编/移/删/搜索
│  ├──────────────────┤ │
│  │  Git 只读         │ │  status/diff（安全隔离）
│  ├──────────────────┤ │
│  │  持久化状态       │ │  SQLite (WAL 模式)
│  └──────────────────┘ │
└────────────────────────┘
```

---

## 开发

### 设置开发环境

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 安装 pre-commit（可选）
pip install pre-commit
```

### 检查代码质量

```bash
# 语法检查
ruff check src/

# 类型检查
mypy src/

# 运行测试
pytest

# 性能基准测试
./benchmark --quick
```

### 代码结构

```
src/
├── cat_mcp/
│   ├── __init__.py          # 包元数据
│   ├── __main__.py          # CLI 入口
│   ├── config.py            # 配置加载
│   ├── errors.py            # 统一错误码
│   ├── server.py            # MCP 服务器（应用工厂）
│   ├── state.py             # SQLite 持久化
│   ├── acl.py               # 访问控制
│   ├── auth.py              # 认证
│   ├── workspace.py         # 工作空间管理
│   ├── projects.py          # 工程注册
│   └── fs/
│       ├── atomic.py        # 原子写入
│       ├── audit.py         # 审计日志
│       ├── chunked_io.py    # 分块上传/下载
│       ├── copier.py        # 文件复制
│       ├── directory.py     # 目录浏览
│       ├── directory_ops.py # 目录创建
│       ├── editor.py        # 文件编辑
│       ├── finder.py        # 文件搜索
│       ├── git_tools.py     # Git 只读工具
│       ├── history.py       # 变更历史
│       ├── idempotency.py   # 幂等检查
│       ├── lock.py          # 写入锁
│       ├── mover.py         # 移动/重命名
│       ├── patcher.py       # 差异补丁
│       ├── paths.py         # 安全路径解析
│       ├── project_scanner.py # 工程扫描
│       ├── quota.py         # 配额管理
│       ├── reader.py        # 文件读取
│       ├── searcher.py      # 全文搜索
│       ├── sensitive.py     # 敏感文件过滤
│       ├── trash.py         # 软删除
│       └── writer.py        # 文件写入
├── Dockerfile
├── docker-compose.yml
├── Caddyfile
├── install                  # 安装脚本
├── upgrade                  # 升级脚本
├── rollback                 # 回滚脚本
├── uninstall                # 卸载脚本
├── benchmark                # 性能基准
├── cat-mcp.service          # systemd 服务
└── pyproject.toml
```

---

## 许可

本项目采用 MIT 许可证 — 详见 [LICENSE](LICENSE) 文件。

### 第三方许可

依赖项目的许可证信息存放在 [`doc/NOTICE.md`](doc/NOTICE.md) 中。
