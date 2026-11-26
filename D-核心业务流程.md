# D-核心业务流程

## 总述

Cat 的核心业务场景是：AI MCP 客户端（如 ChatGPT、Claude Code 等）通过公网 HTTPS 远程访问服务器上授权的工程目录，完成"理解工程结构 -> 定位目标代码 -> 精确修改文件"的完整闭环。服务端负责确定性执行文件操作，客户端负责分析、设计和编程决策，两者通过 MCP 工具契约协同工作。

主体间交互关系：

- **AI MCP 客户端**：发起工具调用，驱动业务前进，编排多步任务
- **Cat 服务端**：验证授权、执行文件操作、维护版本与一致性、记录审计
- **文件系统**：真实工程目录，被 Cat 以安全路径层保护性访问
- **OAuth IdP**：提供身份认证，签发和校验 token

所有写操作遵循"锁定 -> 校验 -> 备份 -> 原子写入 -> 审计"的一致性协议，确保网络中断、并发冲突和进程崩溃场景下不静默破坏文件。

---

## 1. 修订记录

| 版本 | 日期 | 修订说明 | 修订人 |
|------|------|----------|--------|
| v1.0 | 2026-11-26 | 初稿，基于 v1.2 总体需求方案 §8、§9 及 §4、§10 提取 | Cat 文档 |

---

## 2. 业务参与角色

| 角色 | 说明 | 职责 |
|------|------|------|
| AI MCP 客户端/用户 | ChatGPT、Claude Code、Codex 等调用 Cat MCP 工具的 AI 代理或人类用户 | 分析代码、设计修改、编排任务流、确认审批 |
| Cat 服务端 | 公网 MCP 服务，负责文件操作、状态维护与一致性保障 | 鉴权、路径安全、文件读写、版本管理、锁与事务、审计 |
| 文件系统 | 服务器上授权的真实工程目录 | 存储文件内容、提供 fd-relative 安全路径操作能力 |
| OAuth IdP | 外部身份提供商（如 GitHub OAuth） | 签发和验证 access token，确认主体身份 |

---

## 3. 工程理解流程

**源文档：§8.1**

AI 客户端从未知工程开始，逐步定位到目标代码文件，是后续一切修改的前置步骤。

```mermaid
sequenceDiagram
    participant A as AI MCP 客户端
    participant C as Cat 服务端
    participant FS as 文件系统
    participant IdP as OAuth IdP

    Note over A,IdP: 1. 认证与工具发现
    A->>+IdP: OAuth 认证流程（PKCE）
    IdP-->>-A: access token
    A->>+C: MCP 初始化（携带 token）
    C->>IdP: 校验 token 签名/签发者/受众/过期
    IdP-->>C: 有效
    C-->>-A: 工具列表与能力声明

    Note over A,FS: 2. 工程发现与选择
    A->>+C: cat_list_projects(query?, cursor?, limit?)
    C-->>-A: 授权工程列表（ID、显示名、权限摘要）

    A->>+C: cat_open_workspace(project_id, cwd?)
    Note right of C: 生成不可预测 workspace_id<br/>返回 cwd_version
    C-->>-A: workspace_id, cwd_version

    Note over A,FS: 3. 顶层工程概览
    A->>+C: cat_project_info(workspace_id)
    C-->>-A: 名称/语言估计/Git 概况

    A->>+C: cat_list_directory(depth=1)
    C-->>-A: 根目录条目

    Note over A,FS: 4. 逐层深入——逼近目标
    A->>+C: cat_list_directory(path="src/", depth=?)

    A->>+C: cat_find_files(glob="**/*Service*.java")

    A->>+C: cat_search_text(query="class UserService", mode="literal")

    Note over A,FS: 5. 读取源码
    A->>+C: cat_stat(path="src/main/java/UserService.java")
    C-->>-A: 类型/大小/版本/编码

    A->>+C: cat_read_file(path=..., start_line=?, end_line=?, max_bytes=?)
    Note right of C: 分段读取，保留版本号<br/>行号 1-based 闭区间<br/>支持 UTF-8/BOM/LF/CRLF
    C-->>-A: 内容 + 版本 + 编码/换行

    Note over A,FS: 6. 切换到目标工作目录
    A->>+C: cat_set_cwd(workspace_id, path=..., expected_cwd_version)
    Note right of C: 逻辑 CWD，不切换进程目录<br/>不能越出工程根
    C-->>-A: 新 cwd + cwd_version
```

### 关键决策点

| 决策点 | 条件 | 结果 |
|--------|------|------|
| 工程是否授权 | list_projects 返回值 | 未授权工程不返回任何信息 |
| workspace 是否过期 | 每次调用校验 | WORKSPACE_EXPIRED，需重新 open |
| cwd 版本变更 | set_cwd 后旧相对路径写请求 | CWD_CONFLICT |
| 路径是否安全 | 逐级 dir_fd + O_NOFOLLOW 校验 | UNSAFE_PATH / PATH_OUTSIDE_ROOT |

### 数据一致性保障

- workspace 绑定登录主体，不同 AI 即使同一账号也独立 workspace
- 每次请求重验授权和工程 generation
- cwd 变更后旧请求被拒绝，防止混淆工作目录
- 读取时 expected_version 可传入，中途内容改变则返回 VERSION_CONFLICT

---

## 4. 单文件修改流程

**源文档：§8.2**

AI 客户端对一个已知版本的文件执行精确编辑或覆盖写入，是 Cat 最核心的写操作路径。

```mermaid
sequenceDiagram
    participant A as AI MCP 客户端
    participant C as Cat 服务端
    participant FS as 文件系统
    participant SQL as SQLite 状态库

    Note over A,SQL: 主路径：正常修改
    A->>+C: cat_read_file(path, expected_version?)
    C->>FS: 授权分段读取
    FS-->>C: bytes
    C-->>-A: text + 版本 V1

    A->>+C: cat_edit_file(dry_run=true, expected_version=V1, edits)
    Note right of C: 内存中校验所有匹配<br/>拒绝重叠范围、模糊替换<br/>不修改工程
    C-->>-A: diff 预览 + 匹配摘要

    A->>+C: cat_edit_file(request_id, expected_version=V1, edits)
    Note right of C: 进入写入一致性协议（详见 §6）

    C->>SQL: 工程 + 路径级锁定
    SQL-->>C: 锁获取

    alt 版本未变化
        C->>FS: 保存旧文件副本到备份存储
        C->>SQL: 记录 PREPARED
        C->>FS: 创建临时文件 → 写入 → fsync → 原子 rename
        C->>FS: fsync 父目录
        C->>SQL: 记录 COMMITTED + 新版本 V2
        C-->>-A: 回执 + request_id + 版本 V2

        Note over A,C: 确认回读
        A->>+C: cat_read_file(expected_version=V2)
        C-->>-A: 确认内容一致
    else 外部已修改
        C-->>-A: VERSION_CONFLICT
        Note over A: 重新读取当前版本<br/>重新设计编辑策略<br/>不自动覆盖
    end

    Note over A,SQL: 处理路径：版本冲突
    A->>+C: cat_read_file(path)  ← 重新获取最新版本
    C-->>-A: text + 版本 V2'
    A->>C: cat_edit_file(dry_run, expected_version=V2', ...)
    A->>C: cat_edit_file(request_id, expected_version=V2', ...)
    Note right of C: 以新版本为基线重试
    C-->>-A: 成功回执或再次冲突
```

### 网络中断与幂等重试

```mermaid
sequenceDiagram
    participant A as AI MCP 客户端
    participant C as Cat 服务端

    Note over A,C: 场景：提交 edit_file 后网络中断
    A->>+C: cat_edit_file(request_id="r_abc", ...)
    Note over A: 网络断开等待超时

    Note over A: AI 恢复连接后：
    A->>+C: cat_get_operation(request_id="r_abc")
    alt request_id 已执行并提交
        C-->>-A: 已完成 + 回执 + 版本 V2
    else request_id 在执行中（PREPARED）
        C-->>-A: 进行中 + 状态
        Note over A: 等待或稍后重试
    else request_id 不存在
        C-->>-A: 未知
        Note over A: 可安全重试（使用同一 request_id）
    end

    Note over A,C: 关键规则
    Note left of A: 同 request_id + 同参数 = 原回执<br/>同 request_id + 不同参数 = IDEMPOTENCY_CONFLICT<br/>不同 request_id + 同请求 = 二次写入（危险）
```

### 关键决策点

| 决策点 | 条件 | 结果 |
|--------|------|------|
| expected_version 是否匹配 | 与文件系统当前内容 SHA-256 比较 | 匹配则继续，不匹配则 VERSION_CONFLICT |
| dry_run 模式 | 参数 dry_run=true | 只预览不修改，提交时重新校验版本 |
| 编辑匹配约束 | replace_exact 必须唯一匹配、不重叠 | 不满足则 AMBIGUOUS_MATCH / 明确错误 |
| request_id 幂等 | 同 ID 同参数 | 返回原回执不二次写入 |
| 写模式 create vs replace | 文件已存在 + create | ALREADY_EXISTS |
| write_file 无条件覆盖 | 未传 expected_version | 拒绝（不提供 force） |

### 数据一致性保障

- 每个写操作绑定唯一 request_id，支持幂等重试
- 版本校验（SHA-256）确保操作基于最新内容
- 原子 rename 确保临时文件写入不破坏原文件
- 写入完成后回读确认（AI 主动调用）
- 冲突时不自动覆盖，由 AI 重新设计操作
- 跨文件修改由 AI 编排，不作多文件原子事务声明

---

## 5. 创建/移动/删除/恢复流程

**源文档：§8.3**

### 5.1 创建文件/目录

```mermaid
flowchart TD
    Start([AI 调用创建工具]) --> Stat{前置 stat 确认}
    Stat -->|文件已存在 + mode=create| Error1[拒绝: ALREADY_EXISTS]
    Stat -->|文件不存在 + mode=create| ParentDir{父目录是否存在}
    ParentDir -->|父目录已存在| Write[创建文件<br/>= 原子写入（no-replace）]
    ParentDir -->|父目录不存在 + parents=false| Error2[拒绝: 父目录不存在]
    ParentDir -->|父目录不存在 + parents=true| Mkdir[创建父目录链] --> Write
    Write --> Return[返回创建回执]

    Start2([AI 调用 cat_create_directory]) --> DirStat{已有同名文件?}
    DirStat -->|是| Error3[拒绝: ALREADY_EXISTS（不是目录）]
    DirStat -->|不存在或已目录| DoMkdir[mkdir / mkdir -p]
    DoMkdir --> Return2[返回创建回执]
```

### 5.2 移动/复制

```mermaid
flowchart TD
    Start([AI 调用 move/copy]) --> Auth{源 + 目的都授权?}
    Auth -->|任一未授权| Reject[拒绝: FORBIDDEN]
    Auth -->|已授权| Cross{跨工程?}
    Cross -->|是| Reject2[拒绝: V1 禁止跨工程移动]
    Cross -->|同工程| DestExists{目的已存在?}
    DestExists -->|是| Reject3[拒绝: 默认不覆盖]
    DestExists -->|不存在| Execute[执行 move/copy]
    Execute --> Return[返回回执 + 新版本]
```

### 5.3 删除（回收区模式）

```mermaid
flowchart TD
    Start([AI 调用 cat_trash_path]) --> Check{路径是工程根?}
    Check -->|是| Reject[永久拒绝: 不可删除工程根]
    Check -->|否| Scope{递归删除?}
    Scope -->|recursive=true| Gate(需服务端独立审批<br/>提供清单摘要<br/>严格大小上限)
    Scope -->|recursive=false| Trash[移至工程专属回收区]
    Gate -->|审批通过| Trash
    Gate -->|审批不通过| Reject2[拒绝]
    Trash --> Audit[审计记录]
    Audit --> Return[返回回执]
```

**回收区规则**：
- 删除默认移至工程专属回收区（外部目录，不经 MCP 暴露）
- 非立即物理删除
- 递归目录删除需服务端独立审批、清单摘要和严格大小上限
- 删除路径本身不可删除
- 回收区清理由管理员计划执行

### 5.4 恢复

```mermaid
flowchart TD
    Start([AI 调用 cat_restore_file]) --> Current{目标路径当前状态}
    Current -->|存在 + 版本与恢复基线不一致| Reject[拒绝: 不覆盖后续改动]
    Current -->|存在 + 版本匹配| Restore[从回收区/备份恢复]
    Current -->|已不存在| Restore
    Restore --> Return[返回回执 + 新版本]
```

### 关键决策点

| 决策点 | 条件 | 结果 |
|--------|------|------|
| 创建前确认 | stat 确认文件不存在 | 已存在 + create → ALREADY_EXISTS |
| 父目录检查 | 创建时检查父目录存在 | 不存在 + 无 parents → 拒绝 |
| 移动/复制是否覆盖 | 目标已存在 | 默认拒绝，不提供 force 覆盖 |
| 跨工程移动 | V1 限制 | 直接拒绝 |
| 路径是否为工程根 | 路径等于工程根 | 永远不可删除、移动或覆盖 |
| 恢复版本冲突 | 当前已有后续修改 | 拒绝恢复，防止覆盖 |
| 递归删除安全门 | recursive=true | 需独立审批 + 清单 + 上限 |

### 数据一致性保障

- create 使用 no-replace 语义的原子写入
- 删除不立即物理删除，可恢复
- 恢复操作检查当前版本，不覆盖后续变更
- 所有操作走完整权限校验链

---

## 6. 写入一致性流程（原子提交协议）

**源文档：§9**

Cat 的写入一致性协议确保单文件写入在并发、崩溃、网络中断等场景下不静默破坏文件。

### 6.1 正常写入事务

```mermaid
flowchart TD
    Start([写入请求到达]) --> Lock[1. 获取锁]
    Lock -->|锁键: project_id + 规范路径<br/>移动: 锁定源+目的<br/>目录: 层级锁/工程锁| AuthCheck[2. 校验权限]
    AuthCheck --> PathCheck[3. 校验路径安全]
    PathCheck --> TypeCheck[4. 校验文件类型]
    TypeCheck --> VersionCheck[5. 校验 expected_version]
    VersionCheck -->|匹配| SaveOld[6. 保存旧字节到受限对象存储]
    SaveOld --> Step7[7. SQLite 记录 PREPARED]
    Step7 --> TempWrite[8. 同目录创建独占临时文件]
    TempWrite --> WriteContent[9. 写入内容]
    WriteContent --> Permission[10. 保留文件许可属性]
    Permission --> Fsync1[11. fsync 临时文件]
    Fsync1 --> AtomicRename[12. 原子 rename<br/>新建时使用 no-replace 语义]
    AtomicRename --> FsyncDir[13. fsync 父目录]
    FsyncDir --> Step14[14. SQLite 记录 COMMITTED + 新版本]
    Step14 --> Audit[15. 审计记录]
    Audit --> Cleanup[16. 清理临时文件]
    Cleanup --> Return[返回成功回执]

    VersionCheck -->|不匹配| Conflict[VERSION_CONFLICT]
    Conflict --> CleanupErr[清理临时文件<br/>不宣称已写入]
    CleanupErr --> ReturnErr[返回冲突错误]

    Step7 -->|任意步骤崩溃| CrashRecovery[见 6.2 崩溃恢复]

    AtomicRename -->|重命名失败| FailCleanup[清理临时文件<br/>不宣称已写入]
    FailCleanup --> ReturnFail[返回写入失败]
```

### 6.2 崩溃恢复

```mermaid
flowchart TD
    Start([服务重启]) --> Scan[扫描 SQLite 中所有 PREPARED 记录]
    Scan --> Eval{根据 before_hash + after_hash + 目标路径状态判断}
    Eval -->|文件内容 hash = after_hash| Committed[已提交<br/>→ 更新记录为 COMMITTED]
    Eval -->|文件内容 hash = before_hash| Uncommitted[未提交<br/>→ 清理备份和临时文件<br/>→ 标记 ABORTED]
    Eval -->|无法确定<br/>（hash 均不匹配/缓存文件丢失等）| InDoubt[IN_DOUBT<br/>→ 冻结该路径<br/>→ 等待管理员介入]
    Committed --> Done[恢复完成]
    Uncommitted --> Done
    InDoubt --> Alarm[管理员介入，不可盲重试]
```

### 6.3 并发边界说明

| 情形 | Cat 保障 | 边界/风险 |
|------|----------|----------|
| 多个 Cat 客户端并发写同一文件 | Cat 内锁 + 版本校验确保只有一方成功 | 无 |
| 操作系统本地进程（IDE 等）并发编辑 | 无约束 | 写入前版本核验能检测部分冲突，但存在极窄 TOCTOU 窗口 |
| POSIX rename + compare-and-swap | 无通用原子 CAS | 外部编辑者需要统一协调协议 |
| 共享 live workspace 模式 | 替换前核验、写后检查、保留备份 | 不能承诺绝对无冲突 |

### 6.4 备份管理

| 属性 | 值 |
|------|-----|
| 备份路径 | 工程外独立存储 |
| 备份保留周期 | 7 天（建议默认值） |
| 备份配额 | 按数据策略调整 |
| 存储限制 | 默认不限但可配置配额；超量返回错误及重试建议 |

### 关键决策点

| 决策点 | 条件 | 结果 |
|--------|------|------|
| 锁键范围 | 单文件 = project_id + 路径；移动 = 源+目的；目录操作 = 层级锁 | 稳定顺序避免死锁 |
| PREPARED 后崩溃 | 重启后扫描 PREPARED | 判断已提交/未提交/IN_DOUBT |
| IN_DOUBT 处理 | hash 均不匹配 | 路径冻结，管理员介入 |
| 临时文件失败 | 任意步骤失败 | 清理临时文件，不宣称写入 |
| 外部编辑 | 本地进程不遵守 Cat 锁 | TOCTOU 窗口，无法严格保证 |

---

## 7. 权限校验流程

**源文档：§10 §7**

四级校验 + 默认 deny 策略，每次工具调用均需完整走通。

```mermaid
flowchart TD
    Start([请求到达]) --> L1{一级: 主体校验}
    L1 -->|token 有效 + 签名/签发者/受众/scope 校验通过| L2
    L1 -->|无效/过期/被撤销| Reject1[拒绝: UNAUTHENTICATED]

    L2{二级: 工程校验} -->|主体在该工程有授权 Grant| L3
    L2 -->|无对应 Grant| Reject2[拒绝: FORBIDDEN]

    L3{三级: 路径校验} -->|路径在 allowed_prefixes 内<br/>+ 安全路径检查|x26a0; 需同时满足
    L3 -->|路径越权或拒绝列表命中| Reject3[拒绝: FORBIDDEN / UNSAFE_PATH]

    L4{四级: 动作校验} -->|discover/read/write/delete/restore/audit<br/>动作权限匹配| Allow[允许执行]
    L4 -->|动作不被 Grant scopes 覆盖| Reject4[拒绝: FORBIDDEN]

    Note over Start,Allow: 敏感文件 denylist 额外过滤<br/>.env/私钥/凭据/生产配置/Git 内部配置等<br/>搜索/目录/读/写/备份统一执行
```

### 权限分离粒度

| 权限 | 对应工具举例 |
|------|-------------|
| discover | cat_list_projects |
| read | cat_read_file, cat_list_directory, cat_search_text, cat_stat, cat_project_info |
| write | cat_write_file, cat_edit_file, cat_apply_patch, cat_create_directory, cat_move_path, cat_copy_file |
| delete | cat_trash_path |
| restore | cat_restore_file |
| audit | cat_list_changes, cat_diff_file |

### 关键规则

- 默认 deny，未显式授权即为拒绝
- OAuth scopes 与 ACL 取交集，两者都必须通过
- 会话不能替代每次鉴权
- AI 不得扩大授权、修改 Cat 配置或自身权限
- 敏感文件 denylist 在所有工具中统一执行
- 审计失败时写操作 fail closed 或进入可恢复未决状态

---

## 8. 客户端接入流程

**源文档：§4**

Cat 提供两条客户端接入路线，取决于用户的 ChatGPT 账户等级及客户端类型。

### A 路线：ChatGPT 原生 MCP（优先路线）

```mermaid
sequenceDiagram
    participant User as 用户
    participant ChatGPT as ChatGPT Web
    participant Cat as Cat 服务端
    participant IdP as OAuth IdP

    Note over User,Cat: 前置条件：公网 DNS + TLS 证书 + OAuth 配置
    User->>ChatGPT: Plugins → 添加自定义 MCP
    ChatGPT->>Cat: 输入 endpoint URL: https://cat.example.com/mcp
    ChatGPT->>IdP: OAuth 授权流程
    IdP-->>ChatGPT: access token
    ChatGPT->>Cat: MCP 初始化
    Cat-->>ChatGPT: 工具列表
    User->>ChatGPT: 检查确认工具

    Note over User,Cat: 验证测试（按顺序）
    User->>ChatGPT: cat_list_projects
    User->>ChatGPT: cat_open_workspace
    User->>ChatGPT: cat_read_file
    User->>ChatGPT: cat_write_file
    User->>ChatGPT: cat_edit_file

    Note over User,Cat: ChatGPT 按客户端要求确认写操作
    Note over User,Cat: 写工具 readOnlyHint=false<br/>写工具不伪装成只读
```

### B 路线：API / 其他客户端（备选路线）

```mermaid
sequenceDiagram
    participant Dev as 开发者/AI
    participant Client as API 客户端或 CLI
    participant Cat as Cat 服务端

    Dev->>Client: 配置远程 MCP endpoint + 授权令牌
    Client->>Cat: MCP 初始化
    Cat-->>Client: 工具列表
    Dev->>Client: 调用 Cat 工具（工具执行审批显式控制）
    Note over Dev,Client: 适用于：OpenAI Responses API<br/>Codex/Claude Code 等支持完整 MCP 的客户端
```

### 协议与传输

| 属性 | 说明 |
|------|------|
| 传输协议 | Streamable HTTP |
| 会话模型 | Cat workspace_id 是业务级上下文，与协议连接 ID 分离 |
| 持久化假设 | 不依赖 MCP session header 存在或会话持久化 |
| 授权审批 | 客户端不支持 elicitation 时返回结构化错误 |
| 兼容性 | MCP 2025-era 客户端及后续版本须经集成测试 |

### 关键决策点

| 决策点 | 条件 | 结果 |
|--------|------|------|
| 账户是否支持自定义写 | ChatGPT Plus vs Business/Enterprise/Edu/Pro | Pro 只读，Business+ 完整读写 |
| B 路线账户场景 | 当前网页对话不支持自定义写入 | 改用 API 或其他客户端 |
| 写工具审批 | 客户端/服务端共同控制 | 写操作需显式确认 |
| 工具注解伪装 | readOnlyHint/destructiveHint | 按真实语义配置，不规避审批 |

---

## 附：流程间依赖关系

```mermaid
flowchart LR
    Auth[认证授权] --> Explore[工程理解]
    Explore --> Edit[单文件修改]
    Explore --> Create[创建/移动/删除/恢复]
    Edit --> Consistency[写一致性协议]
    Create --> Consistency
    Consistency --> Audit[审计记录]

    style Auth fill:#4a90d9,color:#fff
    style Explore fill:#50b86c,color:#fff
    style Edit fill:#e8a838,color:#fff
    style Create fill:#e8a838,color:#fff
    style Consistency fill:#d94a4a,color:#fff
    style Audit fill:#8e6cc0,color:#fff
```

- **认证授权**是所有业务的前置依赖
- **工程理解**是文件修改和创建的前置依赖（必须先定位目标文件及其版本）
- **单文件修改**和 **创建/移动/删除/恢复** 共享同一套写入一致性协议
- **审计记录**是所有写操作的最终落点
- 跨文件修改不声明多文件原子事务，由 AI 客户端逐文件编排

---

*本文档对应源 `docs/Cat-总体需求与技术实现方案.md`，各章节编号以 v1.2 版本为准。*
