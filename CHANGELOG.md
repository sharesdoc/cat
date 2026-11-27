# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — Unreleased

### Added

#### 工程基础
- Initial project scaffold with pyproject.toml and uv-based build system.
- CLI entry point with argparse argument parsing.
- MCP server shell (MCPServer class with tool registration).
- Configuration loading skeleton (dataclass-based config model).
- Unified error code enumeration and error response structure.
- Test harness with pytest and import verification tests.
- CI workflow (GitHub Actions) with Python 3.12/3.13, Ruff linting, and pytest.
- Community documentation: README, CONTRIBUTING, SECURITY, CHANGELOG, NOTICE.
- Configuration example derived from system design specification.

#### 认证与授权
- OAuth2/JWT 令牌验证和身份认证
- 工程注册、发现与授权列表
- 4 级 ACL（主体→工程→路径前缀→动作），默认拒绝

#### 安全路径与文件保护
- fd-relative 安全路径解析（O_NOFOLLOW, O_DIRECTORY）
- 敏感文件保护策略（可配置 denylist + exception）
- 独立工作空间与相对工作目录
- `.cat_trash`/`.cat_backup`/`.cat_uploads` 隔离保护

#### 只读文件操作
- 分层目录浏览与稳定分页（Glob 过滤）
- 文件属性读取与分段文本读取（偏移/范围）
- 按名称和路径查找文件
- 有界全文搜索（集成 ripgrep，标准库回退）

#### 写入操作
- 写入锁与版本条件校验（乐观并发控制）
- 原子文件提交与崩溃恢复（AtomicWriter）
- 变更请求幂等性检查
- 操作审计与备份保护
- 单文件创建与完整写入（create/replace 模式）
- 安全创建工程子目录（递归模式）
- 文件与目录安全移动重命名
- 单文件复制（不覆盖）
- 软删除及隔离回收区
- 单文件精确局部编辑（文本匹配、行范围、插入）
- 严格的单文件统一差异补丁

#### 工程增强
- 工程概要与构建清单发现（20+ 构建文件类型）
- Git 只读状态查询（porcelain v2 格式解析）
- Git 差异对比（白名单 target 限制）
- 变更历史查询与文件差异对比
- 单文件分块上传/下载（哈希校验，原子提交）
- 统一配额与有界工具执行

#### 部署与运维
- 多阶段 Docker 构建（distroless 运行时）
- Docker Compose 部署（Caddy 反向代理，自动 TLS）
- 原生安装/升级/回滚/卸载脚本
- systemd 服务定义
- 性能基准测试工具（合成十万文件）
- 发布门禁文档与隐私检查

### Security
- Git subprocess 固定 argv 白名单，禁用外部 diff/pager
- diff target 白名单限制（None/cached/HEAD），防止参数注入
- TOCTOU 窗口降低（路径重解析 + 合并安全检查）
- 敏感文件检查失败时保守拒绝
- `.cat_trash`/`.cat_backup`/`.cat_uploads` 加入敏感保护
- stderr 脱敏返回（完整信息记录到日志）
- 上传 session 绑定 subject+workspace，禁止跨主体
