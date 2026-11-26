# E-数据模型设计

> 项目：Cat — 远程工程文件系统 MCP 服务
> 版本：v1.0
> 日期：2026-11-26

## 总述

Cat 系统的数据模型围绕六个核心领域组织：工程注册（Project）、访问授权（Grant）、工作空间会话（Workspace）、写操作事务（Operation）、版本快照（Revision）和审计追踪（Audit），外加配置模型、路径模型与错误模型。所有实体持久化于单进程 SQLite（WAL 模式），不使用独立数据库服务器。数据模型遵循"逻辑外键、应用层约束"原则，禁用物理外键。ID 字段使用不可预测随机值（UUID / secrets.token_urlsafe），防御枚举攻击。配置通过 TOML 文件加载，部署者提供实际路径和身份提供商端点，所有真实路径、凭据和运行期数据不出现在公共仓库中。

---

## 1. 修订记录

| 版本 | 日期 | 修订内容 | 修订人 |
|------|------|----------|--------|
| v1.0 | 2026-11-26 | 初稿 — 依据总体方案 §6、§11、§12 提取全部数据模型 | Cat |

---

## 2. 数据模型总览

Cat 系统的核心领域围绕六个逻辑域组织，涵盖工程注册到访问授权、工作空间会话管理、写入操作事务、版本快照持久化以及全量审计追踪：

| 领域 | 核心实体 | 职责 |
|------|----------|------|
| **工程注册** | Project | 注册并管理被授权的真实工程目录，每个工程对应一个服务器文件系统路径 |
| **授权** | Grant | 定义主体（AI/用户）对工程的访问范围、动作类型与有效期限 |
| **工作空间** | Workspace | 维护 AI 在工程内的当前工作目录（CWD）、上下文版本和租期，不同会话隔离 |
| **操作事务** | Operation | 记录每次写操作的元数据，支持幂等重试、版本冲突检测与崩溃恢复 |
| **版本快照** | Revision | 保存写操作前后的文件内容引用，用于单文件回滚与差异对比 |
| **审计** | Audit | 记录所有受控操作的调用方、操作、路径、结果与关联 ID |

**实体关系概要：**
- 一个 Project 包含 0..N 个 Grant，每个 Grant 绑定一个 subject（主体）。
- 一个 subject 在一个 Project 上可打开 0..N 个 Workspace（不同 AI 独立）。
- 一个 Workspace 产生 0..N 个 Operation（写操作事务）。
- 一个 Operation 产生 0..N 个 Revision（写前快照、写后哈希）。
- 一个 Operation 对应 1 条 Audit 记录或由 Audit 实体独立记录。

所有实体**持久化**于 SQLite 数据库（`/var/lib/cat/` 下），MCP 工具只通过业务 API 间接访问；仅 Workspace 和 Operation 的部分字段暴露给 MCP 响应。

**物理外键禁用原则**：实体间关系通过应用层逻辑维护，不建立 SQLite 物理外键约束。

---

## 3. 核心实体定义

### 3.1 Project（工程）

**持久化**：是 | **暴露给 MCP 工具**：是（`cat_list_projects`、`cat_open_workspace`）

描述被管理的一个真实工程目录。由管理员通过配置或独立管理面注册，不接受 AI 任意路径。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | TEXT | PK, NOT NULL, 唯一 | 工程标识符，不依赖目录名，管理员指定 |
| display_name | TEXT | NOT NULL | 人可读的工程显示名，用于 MCP 工具返回 |
| root_path | TEXT | NOT NULL | 工程在服务器上的真实绝对路径，如 `/srv/cat/projects/demo-app` |
| root_identity | TEXT | NOT NULL | 工程根路径的确定性标识（如 realpath + 设备/inode），用于检测目录重命名/移动 |
| enabled | INTEGER | NOT NULL, DEFAULT 1 | 布尔值，是否启用（1=启用，0=禁用） |
| policy_id | TEXT | NOT NULL | 关联的安全策略标识符，控制路径解析规则（symlink、挂载、特殊文件等策略） |
| generation | INTEGER | NOT NULL, DEFAULT 1 | 工程元数据版本号，每修改配置递增，用于检测并发配置变更 |

**关系：** Project.id 被 Grant.project_id、Workspace.project_id 引用。

---

### 3.2 Grant（授权）

**持久化**：是 | **暴露给 MCP 工具**：否（仅服务端内部校验）

定义主体对某个工程的访问授权。每次 MCP 工具调用均需重新校验。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| subject | TEXT | NOT NULL | 授权主体标识符，来自 OAuth token 的 sub 声明 |
| project_id | TEXT | NOT NULL, 引用 Project.id | 关联的工程 ID |
| scopes | TEXT | NOT NULL | 逗号分隔的权限范围：`discover,read,write,delete,restore,audit` 等。取 OAuth scope 与 ACL 的并集 |
| allowed_prefixes | TEXT | 可空 | 路径前缀白名单（JSON 数组），限制主体只能访问工程下特定子路径；空表示工程全部 |
| expires_at | TEXT (ISO 8601) | NOT NULL | 授权到期时间戳，过期后所有操作返回 FORBIDDEN |

**约束：**
- (subject, project_id) 联合唯一。
- scopes 值必须属于预定义动作集合：discover < read < write < delete < restore，不可自定义字符串。

---

### 3.3 Workspace（工作空间）

**持久化**：是 | **暴露给 MCP 工具**：是（`cat_open_workspace` 返回 id，后续工具通过 workspace_id 引用）

AI 在某个工程内打开的逻辑会话，维护虚拟当前工作目录（CWD）及上下文版本。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | TEXT | PK, NOT NULL | 不可预测随机 ID（如 `ws_<urandom>`），不可自增，必须绑定登录主体 |
| subject | TEXT | NOT NULL | 创建该 workspace 的主体标识，每次请求重验与当前 token subject 一致 |
| project_id | TEXT | NOT NULL, 引用 Project.id | 关联的工程 ID |
| cwd | TEXT | NOT NULL, DEFAULT '/' | 逻辑当前工作目录（相对工程根），不允许超出工程根 |
| cwd_version | INTEGER | NOT NULL, DEFAULT 1 | CWD 变更版本号，每次 `cat_set_cwd` 递增；写工具传入此版本，不匹配则拒绝 |
| project_generation | INTEGER | NOT NULL | 打开时的 Project.generation，工程配置变更后旧 workspace 失效 |
| expires_at | TEXT (ISO 8601) | NOT NULL | 工作空间过期时间，建议空闲 2 小时、最长 24 小时（可配置） |
| status | TEXT | NOT NULL, DEFAULT 'active' | 状态枚举：`active` / `idle` / `expired` / `closed` |

**关系：** Workspace.id 被 Operation.workspace_id、Audit.workspace_id 引用。

**约束**：
- id 长度建议 32 字符以上，仅含小写字母、数字和下划线。
- status 变动链路：active → idle（超时）→ expired；或 active → closed。
- 同 subject 在不同 workspace 的 CWD 相互独立。

---

### 3.4 Operation（操作事务）

**持久化**：是 | **暴露给 MCP 工具**：是（`cat_get_operation` 返回状态和回执）

记录一次写操作的生命周期，支持幂等重试、版本冲突检测与崩溃恢复。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | TEXT | PK, NOT NULL | 全局唯一操作 ID（如 `op_<urandom>`） |
| subject | TEXT | NOT NULL | 发起该操作的主体标识 |
| workspace_id | TEXT | NOT NULL, 引用 Workspace.id | 操作所属的工作空间 |
| request_id | TEXT | NOT NULL, UNIQUE | 客户端提供的幂等请求 ID，用于重试检测 |
| input_digest | TEXT | NOT NULL | 请求输入的 SHA-256 摘要，用于重放时校验参数是否一致；不同参数同 ID 返回 IDEMPOTENCY_CONFLICT |
| path | TEXT | NOT NULL | 操作的目标路径（规范化后的相对路径） |
| status | TEXT | NOT NULL | 状态枚举：`PREPARED` / `COMMITTED` / `FAILED` / `IN_DOUBT` |
| before_version | TEXT | 可空（创建操作时为 NULL） | 操作前的文件版本哈希（SHA-256） |
| after_version | TEXT | 可空（失败时 NULL） | 操作后的文件版本哈希（SHA-256） |
| started_at | TEXT (ISO 8601) | NOT NULL | 操作开始时间 |
| completed_at | TEXT (ISO 8601) | 可空 | 操作完成时间（COMMITTED/FAILED 时填充） |

**关系：** Operation.id 被 Revision.operation_id、Audit.event_id（通过 correlation_id）引用。

**状态机**：
```
PREPARED ──→ COMMITTED
     │            │
     └──→ FAILED  └──→ (只读不可变)
     │
     └──→ IN_DOUBT（崩溃恢复进入）
```

---

### 3.5 Revision（版本快照）

**持久化**：是 | **暴露给 MCP 工具**：否（通过 `cat_diff_file`、`cat_restore_file` 间接访问）

保存写操作前后的文件内容引用，用于差异对比和单文件回滚。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| id | TEXT | PK, NOT NULL | 全局唯一快照 ID（如 `rev_<urandom>`） |
| operation_id | TEXT | NOT NULL, 引用 Operation.id | 关联的操作事务 ID |
| before_blob_ref | TEXT | 可空（新建文件时为 NULL） | 操作前文件的备份对象引用（受限对象存储路径或 blob 键） |
| after_hash | TEXT | NOT NULL | 操作后文件内容的 SHA-256 哈希 |
| mode | TEXT | NOT NULL | 操作模式枚举：`create` / `replace` / `edit` / `patch` / `move` / `copy` / `trash` |
| expires_at | TEXT (ISO 8601) | NOT NULL | 快照保留过期时间，默认 7 天后可回收（受 backup_retention_days 控制） |

**约束：**
- before_blob_ref 指向受限对象存储中的旧版本备份，需加密或严格 OS 权限保护。
- 同一 operation_id 下可有多个 Revision（如 copy 操作涉及源和目标）。

---

### 3.6 Audit（审计事件）

**持久化**：是 | **暴露给 MCP 工具**：否（审计数据独立存储，权限隔离；`cat_list_changes` 返回精简版本）

记录所有受控操作的主体、路径、动作、结果和关联信息，实现完整审计追踪。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| event_id | TEXT | PK, NOT NULL | 全局唯一审计事件 ID（如 `evt_<urandom>`） |
| subject | TEXT | NOT NULL | 发起操作的主体标识 |
| project_id | TEXT | NOT NULL | 操作发生的工程 ID |
| workspace_id | TEXT | 可空 | 操作所在的工作空间 ID（非写操作可为 NULL） |
| action | TEXT | NOT NULL | 操作类型枚举：`read_file` / `write_file` / `edit_file` / `create_directory` / `move_path` / `copy_file` / `trash_path` / `restore_file` / `open_workspace` / `close_workspace` / `set_cwd` / `list_directory` / `search_text` / `find_files` / `list_projects` / `stat` / `project_info` / `list_changes` / `get_operation` / `diff_file` 等 |
| canonical_relative_path | TEXT | 可空 | 规范化的项目相对路径（目录操作、非文件操作可为 NULL） |
| outcome | TEXT | NOT NULL | 结果枚举：`ok` / `denied` / `conflict` / `error` / `not_found` |
| bytes | INTEGER | NOT NULL, DEFAULT 0 | 传输或操作的字节数（搜索读取字节、文件写入大小等） |
| timestamp | TEXT (ISO 8601) | NOT NULL | 事件发生时间戳 |
| correlation_id | TEXT | 可空 | 关联 ID（如 request_id），用于串联同一请求的多个审计事件 |

**约束：**
- 审计数据单独存储、权限隔离，与工程文件不在同一目录。
- 日志默认不存文件正文、diff、token。
- 审计失败时写操作 fail closed 或进入可恢复未决状态。

---

## 4. 配置模型

配置以 TOML 文件管理，默认路径由管理员指定，环境变量可覆盖敏感项。

### 4.1 `[server]` — 服务端

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| listen | string | `"127.0.0.1:8765"` | 服务监听地址，公网部署建议反向代理监听 443 |
| public_url | string | — | 公网可访问的 MCP endpoint URL，用于 OAuth 重定向及客户端配置 |

### 4.2 `[auth]` — 鉴权

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| issuer | string | — | OAuth issuer 标识，用于验证 access token 的 iss 声明 |
| audience | string | `"cat"` | OAuth audience，校验 token aud 声明 |
| require_auth | boolean | `true` | 是否强制鉴权；本地开发模式可设为 false，公网必须 true |

### 4.3 `[[projects]]` — 工程注册列表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 工程标识符，与 Project.id 对应，不依赖目录名 |
| root | string | 工程真实绝对路径，与 Project.root_path 对应 |
| access | array of string | 该项目允许的动作上限，如 `["read", "write"]`；最终权限取 ACL 与 access 的交集 |

约束：access 值取值范围同 Grant.scopes：`discover`、`read`、`write`、`delete`、`restore`、`audit`。

### 4.4 `[limits]` — 限额与阈值

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| directory_depth | integer | `8` | 目录浏览最大递归深度 |
| page_entries | integer | `200` | 分页单页最大条目数 |
| max_tool_response_bytes | integer | `65536` | 单次 MCP 工具响应最大字节数（64 KiB） |
| max_inline_write_bytes | integer | `262144` | 单次内联写入（非分块）最大字节数（256 KiB） |
| max_search_seconds | integer | `10` | 搜索操作最大耗时（秒），超时明确返回 |
| max_search_results | integer | `100` | 搜索返回的最大匹配结果数 |
| workspace_idle_seconds | integer | `7200` | 工作空间空闲超时秒数（2 小时） |
| max_concurrent_per_subject | integer | `4` | 每主体最大并发操作数 |
| backup_retention_days | integer | `7` | 备份和快照保留天数 |

### 4.5 `[security]` — 安全策略

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| allow_shell | boolean | `false` | 是否允许任意 Shell 执行（V1 固定为 false） |
| follow_symlinks | boolean | `false` | 是否跟随符号链接（V1 固定为 false） |
| allow_recursive_delete | boolean | `false` | 是否允许递归删除目录（需服务端独立审批） |
| deny_sensitive_files | boolean | `true` | 是否拒绝敏感文件（.env、私钥、凭据、Git 内部配置等） |

---

## 5. 路径模型

Cat 的路径模型基于工程相对路径、逻辑 CWD 和严格的安全校验，定义如下：

### 5.1 路径类型

| 概念 | 说明 |
|------|------|
| **绝对路径** | 客户端禁止传入服务器绝对路径。工具内部使用工程根 + 规范化后的相对路径拼接 |
| **相对路径** | 参数 `path` 默认相对 Workspace.cwd。可用 `path_base=root` 从工程根绝对定位 |
| **CWD** | 逻辑虚拟当前工作目录，由 Workspace 实体维护。支持 `.`、`..` 和子目录，但不得越出工程根。永不切换系统进程工作目录 |
| **path_base** | 可选参数，值仅为 `root`（从工程根定位）或省略（从 CWD 定位） |

### 5.2 路径规范化规则

1. 拒绝服务器绝对路径、URL 协议前缀、NUL 字符、Windows 驱动器/UNC 等歧义形式。
2. 拒绝 `..` 序列逸出工程根。
3. 所有路径在授权校验前完成规范化（移除冗余分隔符、解析 `.`/`..`）。
4. 每个 MCP 工具响应返回 `project_id`、`workspace_id`、`cwd`、`cwd_version` 和规范化后的相对路径。

### 5.3 安全约束

| 约束 | 行为 |
|------|------|
| 符号链接 | 默认禁止穿越及写入；可显示链接状态，但不返回外部目标 |
| 特殊文件 | 拒绝特殊设备、FIFO、socket、未获授权子挂载、危险 hardlink |
| fd-relative | 使用 `dir_fd` 逐级打开父目录，`O_NOFOLLOW` / `O_DIRECTORY` 确保安全路径解析 |
| 根保护 | 工程根本身不可删除、移动或覆盖 |
| 路径授权 | 根目录、目录祖先及目的路径均需授权校验 |
| CWD 版本 | 写调用必须传入 `cwd_version`，CWD 变更后旧版本号写请求返回 CWD_CONFLICT |
| 敏感文件 | `.env`、私钥、凭据、生产配置、Git 内部配置等按 denylist 策略统一拒绝，所有工具（读/写/搜索/备份）执行相同策略 |

---

## 6. 错误模型

### 6.1 统一响应格式

所有 MCP 工具返回结构化 JSON，格式如下：

```json
{
  "ok": true,
  "request_id": "r_example",
  "workspace_id": "ws_example",
  "data": { ... },
  "error": null
}
```

失败时 `ok` 为 `false`，`error` 字段包含结构化错误信息：

```json
{
  "ok": false,
  "request_id": "r_example",
  "workspace_id": null,
  "data": null,
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "文件版本不匹配：期望 sha256:abc，实际 sha256:def",
    "retryable": true,
    "details": {
      "expected_version": "sha256:abc",
      "actual_version": "sha256:def",
      "path": "src/main.java"
    }
  }
}
```

### 6.2 错误码表

| 错误码 | 含义 | 可重试 | 客户端处理 |
|--------|------|--------|-----------|
| `UNAUTHENTICATED` | 未认证 | 否 | 重新获取 token 后重试 |
| `FORBIDDEN` | 无权限 | 否 | 申请对应授权 |
| `WORKSPACE_EXPIRED` | 工作空间已过期 | 是 | 重新 `cat_open_workspace` |
| `CWD_CONFLICT` | CWD 版本不匹配 | 是 | 调用 `cat_get_workspace` 获取当前版本 |
| `PATH_OUTSIDE_ROOT` | 路径超出工程根 | 否 | 修正路径参数 |
| `UNSAFE_PATH` | 不安全路径（symlink/特殊文件） | 否 | 拒绝且审计 |
| `NOT_FOUND` | 文件或目录不存在 | 否 | 确认路径后重试 |
| `ALREADY_EXISTS` | 文件或目录已存在 | 否 | 确认目标状态后改用 replace 模式 |
| `VERSION_CONFLICT` | 文件版本不匹配 | 是 | 重新 `cat_read_file` 获取最新版本后重试 |
| `AMBIGUOUS_MATCH` | 编辑匹配模糊（多匹配） | 是 | 收窄匹配锚点 |
| `PATCH_MISMATCH` | 补丁应用失败 | 是 | 重新生成补丁 |
| `CURSOR_EXPIRED` | 分页游标已过期 | 是 | 从新分页快照重启 |
| `TOO_LARGE` | 请求超出大小限制 | 是 | 分块重试 |
| `RATE_LIMITED` | 被限速 | 是（按 retry_after） | 等待 retry_after 时间后重试 |
| `APPROVAL_REQUIRED` | 需用户审批 | 否 | 等待真实授权流程 |
| `IDEMPOTENCY_CONFLICT` | 幂等 ID 冲突（不同请求参数） | 否 | 使用新 request_id 重试 |
| `IN_DOUBT` | 操作状态不确定（崩溃后） | 否 | 查询操作状态，联系管理员恢复 |

### 6.3 错误记录规范

- 所有结果避免泄露主机绝对路径和内部堆栈信息。
- MCP 工具业务错误使用 `isError=true` 和结构化内容返回。
- 认证/协议错误使用标准 HTTP 状态码和 MCP 协议语义。
- 审计失败时写操作 **fail closed** 或进入可恢复未决状态。

---

## 附录：持久化与 MCP 暴露矩阵

| 实体 | SQLite 持久化 | MCP 工具直接返回 | MCP 工具间接引用 |
|------|:------------:|:----------------:|:----------------:|
| Project | 是 | `cat_list_projects`、`cat_open_workspace` | — |
| Grant | 是 | — | 内部鉴权，不暴露 |
| Workspace | 是 | `cat_open_workspace`（返回 id、cwd、cwd_version） | 所有写工具通过 `workspace_id` 引用 |
| Operation | 是 | `cat_get_operation`（返回状态和回执） | `cat_list_changes`、`cat_diff_file` |
| Revision | 是 | — | `cat_diff_file`、`cat_restore_file` |
| Audit | 是 | — | `cat_list_changes`（返回精简摘要） |
