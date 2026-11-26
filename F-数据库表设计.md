# Cat 数据库表设计

## 1. 修订记录

| 版本 | 日期 | 修订说明 | 修订人 |
|---|---|---|---|
| v1.0 | 2026-11-26 | 初始版本，基于 Cat 总体需求与技术实现方案 §6 数据模型和 §9 写入一致性设计 | Cat 架构 |

## 2. 数据库概览

Cat 使用 SQLite 作为单进程状态存储引擎，不引入数据库服务器。

### 选型依据

- **嵌入式**：SQLite 是 Python 标准库组件，零外部依赖，适合单进程、单 owner 的部署模型
- **WAL 模式**：Write-Ahead Logging 模式下，读操作不阻塞写操作，写操作不阻塞读操作，提升并发性
- **单进程**：Cat V1 为单进程架构，不跨线程共享未受保护连接，不持有数据库事务等待网络 I/O
- **单 owner**：状态和工程目录在同一个 OS 用户下管理，不涉及多租户数据库隔离
- **不跨服务器**：V1 不支持横向扩展或多 worker 并发写；如需跨信任域隔离则配置独立 worker 进程和独立数据库文件

### 数据库文件位置

默认路径：`/var/lib/cat/cat.db`（可配置），与审计、备份目录分开存储，不暴露给 MCP 工具。

### 连接配置

- 启用 WAL 模式：`PRAGMA journal_mode=WAL;`
- 启用外键约束（逻辑校验辅助）：`PRAGMA foreign_keys=OFF;` — 物理外键禁用，依赖应用层逻辑控制引用完整性
- 同步模式：`PRAGMA synchronous=NORMAL;`（WAL 模式下 NORMAL 足够安全）
- 繁忙超时：`PRAGMA busy_timeout=5000;`

## 3. 表结构设计

### 3.1 projects — 工程注册表

注册管理员配置的工程目录。工程 ID 不依赖目录名，由系统生成不可预测随机 ID（如 UUID 或 secrets.token_urlsafe 生成的十六进制串）。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | TEXT | PRIMARY KEY | 不可预测随机 ID（如 UUID），非自增 |
| display_name | TEXT | NOT NULL | 工程显示名称，供客户端识别 |
| root_path | TEXT | NOT NULL | 工程在服务器上的真实绝对路径 |
| root_identity | TEXT | NOT NULL | 根路径的稳定文件系统标识（如 realpath 规范化结果后的哈希），用于检测目录重命名/重建 |
| enabled | INTEGER | NOT NULL DEFAULT 1 | 启用状态：1=启用，0=停用 |
| policy_id | TEXT | | 关联的访问策略 ID，保留给未来 ACL 扩展 |
| generation | INTEGER | NOT NULL DEFAULT 1 | 工程配置版本号，每次配置变更递增；workspace 打开时锁定当前 generation，变更后旧 workspace 需要重新打开 |
| created_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 创建时间（ISO 8601） |
| updated_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 最后更新时间 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT    PRIMARY KEY,
    display_name    TEXT    NOT NULL,
    root_path       TEXT    NOT NULL,
    root_identity   TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    policy_id       TEXT,
    generation      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### 3.2 grants — 授权表

定义主体对工程的访问权限。每次请求校验时，取 scopes 与工程 access 上限的交集。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增主键 |
| subject | TEXT | NOT NULL | 授权主体标识（OAuth subject、用户 ID），不可预测 |
| project_id | TEXT | NOT NULL | 关联的工程 ID |
| scopes | TEXT | NOT NULL | 授予的权限范围，逗号分隔，如 "read,write"；可用值：discover/read/write/delete/restore/audit |
| allowed_prefixes | TEXT | | 允许访问的路径前缀列表（JSON 数组），NULL 表示工程内全部路径 |
| expires_at | TEXT | | 授权过期时间（ISO 8601），NULL 表示永不过期 |
| created_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 创建时间 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS grants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    subject           TEXT    NOT NULL,
    project_id        TEXT    NOT NULL,
    scopes            TEXT    NOT NULL,
    allowed_prefixes  TEXT,
    expires_at        TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### 3.3 workspaces — 工作空间会话表

AI 客户端打开一个工程时创建的工作空间。Workspace ID 为不可预测随机 ID，必须绑定登录主体。不同 AI 即使使用同一账号，也分别打开独立 workspace，避免 CWD 串扰。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | TEXT | PRIMARY KEY | 不可预测随机 ID（如 UUID 或 secrets.token_urlsafe） |
| subject | TEXT | NOT NULL | 关联的登录主体 |
| project_id | TEXT | NOT NULL | 关联的工程 ID |
| cwd | TEXT | NOT NULL | 当前工作目录（工程相对路径），默认为 "" 表示工程根 |
| cwd_version | INTEGER | NOT NULL DEFAULT 1 | 当前工作目录的版本号，set_cwd 时递增 |
| project_generation | INTEGER | NOT NULL | 打开时对应的工程 generation，用于检测工程配置变更 |
| expires_at | TEXT | NOT NULL | 会话过期时间（ISO 8601），建议空闲 2 小时、最长 24 小时（可配置） |
| status | TEXT | NOT NULL DEFAULT 'active' | 状态：active（活跃）、expired（过期）、closed（关闭） |
| created_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 创建时间 |
| updated_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 最后更新时间 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS workspaces (
    id                 TEXT    PRIMARY KEY,
    subject            TEXT    NOT NULL,
    project_id         TEXT    NOT NULL,
    cwd                TEXT    NOT NULL DEFAULT '',
    cwd_version        INTEGER NOT NULL DEFAULT 1,
    project_generation INTEGER NOT NULL,
    expires_at         TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'active',
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### 3.4 operations — 写操作事务表

记录每个写操作的执行状态，用于幂等、恢复和审计追溯。状态机见第 5 节。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增主键 |
| request_id | TEXT | NOT NULL UNIQUE | 客户端提供的请求唯一 ID，用于幂等重试 |
| subject | TEXT | NOT NULL | 执行主体 |
| workspace_id | TEXT | NOT NULL | 执行时的工作空间 ID |
| project_id | TEXT | NOT NULL | 目标工程 ID |
| input_digest | TEXT | | 请求输入摘要（SHA-256），用于重试校验 |
| path | TEXT | NOT NULL | 操作目标路径（工程相对路径） |
| operation_type | TEXT | NOT NULL | 操作类型：write（写入）、edit（编辑）、patch（补丁）、mkdir（创建目录）、move（移动）、copy（复制）、trash（删除）、restore（恢复） |
| status | TEXT | NOT NULL DEFAULT 'prepared' | 状态：prepared/committed/rolled_back/in_doubt |
| before_version | TEXT | | 操作执行前的文件版本哈希（SHA-256），NULL 表示文件不存在（新建） |
| before_blob_ref | TEXT | | 操作前文件内容的备份引用（指向备份存储中的旧版本快照），用于恢复 |
| after_hash | TEXT | | 操作执行后的文件内容哈希（SHA-256） |
| after_version | TEXT | | 操作执行后的文件版本标识符 |
| bytes | INTEGER | | 操作涉及的数据字节数 |
| started_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 操作开始时间 |
| completed_at | TEXT | | 操作完成时间 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT    NOT NULL UNIQUE,
    subject         TEXT    NOT NULL,
    workspace_id    TEXT    NOT NULL,
    project_id      TEXT    NOT NULL,
    input_digest    TEXT,
    path            TEXT    NOT NULL,
    operation_type  TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'prepared',
    before_version  TEXT,
    before_blob_ref TEXT,
    after_hash      TEXT,
    after_version   TEXT,
    bytes           INTEGER,
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);
```

### 3.5 revisions — 文件版本快照表

记录每次写操作产生的文件版本信息，用于查询变更历史、diff 对比和文件恢复。恢复对象受严格文件系统权限保护，定期清理。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增主键 |
| operation_id | INTEGER | NOT NULL | 关联的操作 ID |
| project_id | TEXT | NOT NULL | 工程 ID |
| path | TEXT | NOT NULL | 文件相对路径 |
| before_blob_ref | TEXT | | 旧版本内容在备份存储中的引用（操作前快照） |
| after_hash | TEXT | NOT NULL | 新版本文件内容哈希（SHA-256） |
| after_blob_ref | TEXT | | 新版本内容在备份存储中的引用（操作后快照） |
| mode | TEXT | NOT NULL | 操作模式：create/create_or_replace/replace/edit/patch/move/copy/trash/restore |
| bytes | INTEGER | | 快照记录的字节数差异 |
| expires_at | TEXT | | 快照过期时间（ISO 8601），建议默认保留 7 天 |
| created_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 记录创建时间 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id    INTEGER NOT NULL,
    project_id      TEXT    NOT NULL,
    path            TEXT    NOT NULL,
    before_blob_ref TEXT,
    after_hash      TEXT    NOT NULL,
    after_blob_ref  TEXT,
    mode            TEXT    NOT NULL,
    bytes           INTEGER,
    expires_at      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### 3.6 audit_log — 审计日志表

记录每个鉴权和操作事件。审计失败时写操作 fail closed 或进入可恢复未决状态。日志默认不存文件正文、diff 和 token。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| event_id | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增事件 ID |
| subject | TEXT | NOT NULL | 操作的登录主体 |
| project_id | TEXT | | 目标工程 ID，NULL 表示非工程级操作 |
| workspace_id | TEXT | | 工作空间 ID |
| action | TEXT | NOT NULL | 操作动作：discover/open_workspace/close_workspace/read/write/edit/patch/mkdir/move/copy/trash/restore/list_directory/find_files/search_text/stat/set_cwd/project_info/list_changes/diff_file/authorization 等 |
| canonical_relative_path | TEXT | | 规范化后的工程相对路径 |
| outcome | TEXT | NOT NULL | 结果：allowed/denied/succeeded/failed |
| bytes | INTEGER | | 操作数据字节数 |
| error_code | TEXT | | 失败时的错误码 |
| request_id | TEXT | | 关联的请求 ID |
| correlation_id | TEXT | | 关联 ID（用于追溯一组相关事件） |
| timestamp | TEXT | NOT NULL DEFAULT (datetime('now')) | 事件时间戳 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS audit_log (
    event_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    subject                  TEXT    NOT NULL,
    project_id               TEXT,
    workspace_id             TEXT,
    action                   TEXT    NOT NULL,
    canonical_relative_path  TEXT,
    outcome                  TEXT    NOT NULL,
    bytes                    INTEGER,
    error_code               TEXT,
    request_id               TEXT,
    correlation_id           TEXT,
    timestamp                TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### 3.7 config — 配置键值表（可选）

用于运行时配置覆盖，方便热更新部分设置而无需重启进程。仅存储不影响核心安全的可调参数。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| key | TEXT | PRIMARY KEY | 配置键名（点分隔命名空间，如 'backup.retention_days'） |
| value | TEXT | NOT NULL | 配置值（JSON 编码，支持字符串/数字/布尔/数组） |
| description | TEXT | | 可读描述 |
| updated_at | TEXT | NOT NULL DEFAULT (datetime('now')) | 最后更新时间 |

**DDL：**

```sql
CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## 4. 表关系设计

### 4.1 实体关系

```
projects  1 ── * grants        一个工程可被多个主体授权
projects  1 ── * workspaces    一个工程可被多个工作空间同时使用
projects  1 ── * operations    一个工程包含多次写操作
projects  1 ── * revisions     一个工程包含多个版本快照

workspaces 1 ── * operations   一个工作空间可发起多次操作

operations 1 ── 0..1 revisions 一次操作产生至多一个版本快照（读类操作不产生）
operations 1 ── 1 audit_log    一次操作对应一条审计记录（一对一的逻辑关系）
```

### 4.2 逻辑外键（物理外键禁用，应用层保证引用完整性）

| 引用方 | 被引表 | 引用字段 | 说明 |
|---|---|---|---|
| grants.project_id | projects.id | project_id | 授权指向已注册工程 |
| workspaces.project_id | projects.id | project_id | 工作空间关联注册工程 |
| workspaces.subject | grants.subject | subject（配合 project_id） | 打开工作空间时校验主体对该工程的授权 |
| operations.workspace_id | workspaces.id | workspace_id | 操作属于一个工作空间 |
| operations.project_id | projects.id | project_id | 操作属于一个工程 |
| revisions.operation_id | operations.id | operation_id | 版本快照关联一个操作 |
| audit_log.project_id | projects.id | project_id | 审计事件关联工程（允许 NULL，表示非工程操作） |
| audit_log.workspace_id | workspaces.id | workspace_id | 审计事件关联工作空间（允许 NULL） |

### 4.3 索引设计

| 索引名称 | 表 | 索引列 | 类型 | 说明 |
|---|---|---|---|---|
| idx_grants_subject_project | grants | subject, project_id | UNIQUE | 按主体和工程查询授权的核心索引 |
| idx_grants_expires_at | grants | expires_at | INDEX | 快速筛选未过期授权 |
| idx_workspaces_subject | workspaces | subject | INDEX | 按主体查询其活跃工作空间 |
| idx_workspaces_status_expires | workspaces | status, expires_at | INDEX | 定期清理过期/关闭的工作空间 |
| idx_operations_request_id | operations | request_id | UNIQUE | 幂等重试：按 request_id 查找已执行操作 |
| idx_operations_workspace | operations | workspace_id | INDEX | 按工作空间查询操作历史 |
| idx_operations_project_status | operations | project_id, status | INDEX | 崩溃恢复：扫描指定工程的 PREPARED 操作 |
| idx_operations_project_path | operations | project_id, path | INDEX | 按工程和路径查询操作历史 |
| idx_revisions_operation | revisions | operation_id | UNIQUE | 按操作 ID 查找版本快照 |
| idx_revisions_project_path_time | revisions | project_id, path, created_at | INDEX | 查询文件的历史版本列表 |
| idx_revisions_expires_at | revisions | expires_at | INDEX | 定期清理过期版本快照 |
| idx_audit_subject | audit_log | subject | INDEX | 按主体审计追溯 |
| idx_audit_project | audit_log | project_id | INDEX | 按工程审计查询 |
| idx_audit_timestamp | audit_log | timestamp | INDEX | 按时间范围检索审计事件 |
| idx_audit_action_outcome | audit_log | action, outcome | INDEX | 按动作和结果过滤审计事件 |

## 5. 状态机

### 5.1 Operation 状态流转

```
                    +---> COMMITTED (成功写入)
                    |
    PREPARED -------+---> ROLLED_BACK (写入失败，清理)
                    |
                    +---> IN_DOUBT (崩溃恢复时无法确定)
```

### 5.2 状态说明

| 状态 | 含义 | 触发条件 | 后续动作 |
|---|---|---|---|
| **PREPARED** | 操作已记录、旧版本已备份、临时文件已写入但尚未原子替换 | 保存旧字节到备份存储后，执行原子 rename 前 | 正常流程：执行原子 rename → 标记 COMMITTED；失败：标记 ROLLED_BACK 并清理临时文件 |
| **COMMITTED** | 原子替换成功、新版本已落盘、审计已记录 | fsync 父目录成功，原子 rename 完成 | 返回成功给客户端；可在 revisions 表中查询变更历史 |
| **ROLLED_BACK** | 操作明确失败，临时文件已清理 | fsync 或 rename 或权限校验失败 | 返回错误给客户端；客户端可重试（同 request_id 返回相同错误） |
| **IN_DOUBT** | 进程在 PREPARED 之后 COMMITTED 之前崩溃，重启后无法判断 rename 是否执行 | 重启时扫描到 PREPARED 状态操作 | 按 before/after hash 判断文件实际状态：如已应用则推进 COMMITTED；如未应用则回滚 ROLLED_BACK；无法判断时冻结该路径等待管理员 |

### 5.3 恢复规程（重启时）

1. 扫描所有 PREPARED 状态的操作记录
2. 对每条记录，检查目标路径的实际文件哈希：
   - 匹配 after_hash：认为 rename 已成功 → 推进为 COMMITTED
   - 匹配 before_version：认为 rename 未执行 → 回滚为 ROLLED_BACK
   - 两者都不匹配：外部或并发修改 → 标记为 IN_DOUBT，保留备份以供管理员处理
3. 输出恢复报告：已提交数、已回滚数、IN_DOUBT 数
4. 对有 IN_DOUBT 的路径：冻结写入访问，等待管理员手工处理

## 6. 数据一致性

### 6.1 WAL 模式

- 启用 SQLite WAL（Write-Ahead Logging）模式，实现读写并发
- WAL 模式下，读取不阻塞写入，写入不阻塞读取
- WAL 文件与主数据库文件共存，应用层负责 checkpoint（SQLite 自动管理，也可通过 `PRAGMA wal_checkpoint;` 手动触发）
- 定期运行 checkpoint 防止 WAL 文件无限增长

### 6.2 短事务

- 所有数据库操作采用短事务，不持有事务等待网络 I/O 或文件 I/O
- 每个事务只包含一条或一组紧密关联的 SQL 语句
- 文件写入的事务流程：
  1. 开始数据库事务
  2. 插入 PREPARED 操作记录 + 备份记录
  3. 提交数据库事务（短事务，文件操作在此之后）
  4. 执行文件操作（原子 rename + fsync）
  5. 开始新事务
  6. 更新操作状态为 COMMITTED + 插入审计记录
  7. 提交事务

### 6.3 fsync

- 原子 rename 后立即 fsync 父目录，确保目录元数据持久化
- 临时文件写入后 fsync 数据，确保文件内容持久化
- SQLite 由 synchronous=NORMAL 管理事务日志落盘

### 6.4 崩溃恢复

- 重启时执行恢复扫描（见第 5.3 节）
- 备份存储中的旧版本快照保留到 expires_at，恢复前不受清理
- 无法确认的 IN_DOUBT 状态冻结写入，等待管理员介入

### 6.5 备份策略

- 定期执行 SQLite 在线备份（`.backup` 命令或 `sqlite3_backup_init` API）
- 备份保留期限默认为 7 天（可配置，通过 `backup.retention_days`）
- 注意：备份和审计目录与数据库文件分开存储
- 备份空间配额约束由系统资源限制管理

## 7. SQL 关键查询

### 7.1 工程授权查询

```sql
-- 查询主体对所有工程的有效授权（含未过期）
SELECT
    p.id AS project_id,
    p.display_name,
    p.generation,
    g.scopes,
    g.allowed_prefixes
FROM grants g
JOIN projects p ON p.id = g.project_id
WHERE g.subject = :subject
  AND p.enabled = 1
  AND (g.expires_at IS NULL OR g.expires_at > datetime('now'));
```

```sql
-- 查询主体对特定工程是否具有指定权限
SELECT 1 FROM grants
WHERE subject = :subject
  AND project_id = :project_id
  AND scopes LIKE '%' || :required_scope || '%'
  AND (expires_at IS NULL OR expires_at > datetime('now'))
LIMIT 1;
```

### 7.2 工作空间打开与验证

```sql
-- 打开工作空间前验证授权
-- 先查授权，再查工程 generation，再插入 workspace
INSERT INTO workspaces (id, subject, project_id, cwd, project_generation, expires_at)
VALUES (:id, :subject, :project_id, :cwd, :generation, :expires_at);
```

```sql
-- 验证工作空间有效性（每次请求调用）
SELECT w.*, p.generation AS current_generation
FROM workspaces w
JOIN projects p ON p.id = w.project_id
WHERE w.id = :workspace_id
  AND w.subject = :subject
  AND w.status = 'active'
  AND w.expires_at > datetime('now');
```

```sql
-- 检测工程 generation 变更
-- 如果返回的 workspace.project_generation != project.current_generation，需让客户端重新打开
```

### 7.3 写操作创建与提交

```sql
-- 创建操作记录（PREPARED 状态）
INSERT INTO operations (request_id, subject, workspace_id, project_id, input_digest,
                        path, operation_type, before_version, before_blob_ref, bytes)
VALUES (:request_id, :subject, :workspace_id, :project_id, :input_digest,
        :path, :operation_type, :before_version, :before_blob_ref, :bytes);
```

```sql
-- 提交操作（原子 rename 成功后）
UPDATE operations
SET status = 'committed',
    after_hash = :after_hash,
    after_version = :after_version,
    completed_at = datetime('now')
WHERE request_id = :request_id AND status = 'prepared';
```

```sql
-- 回滚操作（文件操作失败）
UPDATE operations
SET status = 'rolled_back',
    completed_at = datetime('now')
WHERE request_id = :request_id AND status = 'prepared';
```

```sql
-- 幂等重试：按 request_id 查询已有操作
SELECT id, status, after_version, bytes
FROM operations
WHERE request_id = :request_id;
```

### 7.4 版本快照与审计

```sql
-- 写入操作完成后记录版本快照
INSERT INTO revisions (operation_id, project_id, path, before_blob_ref,
                       after_hash, after_blob_ref, mode, bytes, expires_at)
VALUES (:operation_id, :project_id, :path, :before_blob_ref,
        :after_hash, :after_blob_ref, :mode, :bytes, datetime('now', '+7 days'));
```

```sql
-- 查询文件的历史版本
SELECT r.id, r.after_hash, r.mode, r.created_at, o.request_id, o.subject
FROM revisions r
JOIN operations o ON o.id = r.operation_id
WHERE r.project_id = :project_id AND r.path = :path
ORDER BY r.created_at DESC;
```

```sql
-- 写入审计记录
INSERT INTO audit_log (subject, project_id, workspace_id, action,
                       canonical_relative_path, outcome, bytes, request_id, correlation_id)
VALUES (:subject, :project_id, :workspace_id, :action,
        :canonical_relative_path, :outcome, :bytes, :request_id, :correlation_id);
```

```sql
-- 按时间范围检索审计事件
SELECT * FROM audit_log
WHERE subject = :subject
  AND timestamp >= :from_time
  AND timestamp < :to_time
ORDER BY timestamp DESC
LIMIT :limit;
```

### 7.5 崩溃恢复扫描

```sql
-- 重启时扫描所有 PREPARED 状态的操作
SELECT o.id, o.request_id, o.project_id, o.path,
       o.before_version, o.before_blob_ref,
       o.after_hash, o.after_version
FROM operations o
WHERE o.status = 'prepared'
ORDER BY o.started_at ASC;
```

```sql
-- 将恢复后确认已提交的操作标记为 COMMITTED
UPDATE operations
SET status = 'committed',
    completed_at = datetime('now')
WHERE id = :operation_id AND status = 'prepared';
```

```sql
-- 将恢复后确认未提交的操作标记为 ROLLED_BACK
UPDATE operations
SET status = 'rolled_back',
    completed_at = datetime('now')
WHERE id = :operation_id AND status = 'prepared';
```

### 7.6 工作空间与操作清理

```sql
-- 清理过期 workspace（计划任务或按需执行）
UPDATE workspaces SET status = 'expired'
WHERE status = 'active' AND expires_at <= datetime('now');
```

```sql
-- 清理过期版本快照（计划任务或按需执行）
DELETE FROM revisions
WHERE expires_at IS NOT NULL AND expires_at <= datetime('now');
```

```sql
-- 删除过期备份引用对应的备份存储文件
-- 注意：revision 记录可保留用于审计追溯，仅清理备份存储中的物理文件
```
