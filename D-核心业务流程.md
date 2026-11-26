# D-核心业务流程

> 版本：v1.1（设计审查修订；尚未实现）
> 依据：总体方案 v1.2、A/B、C/E/F、开发实施计划及 CAT-01–40。

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
| v1.1 | 2026-11-26 | 对齐客户端契约、权限与事务状态机；修正创建、恢复、崩溃和分块流程 | 设计复审 |

---

## 2. 业务参与角色

| 角色 | 说明 | 职责 |
|------|------|------|
| AI MCP 客户端/用户 | ChatGPT、Claude Code、Codex 等调用 Cat MCP 工具的 AI 代理或人类用户 | 分析代码、设计修改、编排任务流、确认审批 |
| Cat 服务端 | 公网 MCP 服务，负责文件操作、状态维护与一致性保障 | 鉴权、路径安全、文件读写、版本管理、锁与事务、审计 |
| 文件系统 | 服务器上授权的真实工程目录 | 存储文件内容、提供 fd-relative 安全路径操作能力 |
| OAuth IdP | 经 CAT-05/06 核验支持目标 MCP 授权流程的外部身份提供方 | 签发和验证 access token，确认主体身份 |

---

## 3. 工程理解流程

**源文档：§8.1**

AI 客户端从未知工程开始，逐步定位到目标代码文件，是后续一切修改的前置步骤。

下图为授权工程的主路径。括号中省略的公共参数仍按 C 校验；P1 工具仅在相应功能交付后可调用。

```mermaid
sequenceDiagram
    participant A as AI MCP 客户端
    participant I as OAuth IdP
    participant C as Cat 服务端
    participant F as 安全文件层
    A->>I: OAuth 授权及 PKCE（适用时）
    I-->>A: access token
    A->>C: initialize（认证请求）
    C->>C: 验证令牌并协商协议版本
    C-->>A: 初始化响应
    A->>C: initialized 通知及 tools/list
    C-->>A: 实际可用工具及 Schema
    A->>C: cat_list_projects
    C-->>A: 授权工程列表
    A->>C: cat_open_workspace(project_id)
    C-->>A: workspace_id / cwd_version
    opt P1 工程概要已启用
        A->>C: cat_project_info(workspace_id)
        C-->>A: 授权范围内概要
    end
    A->>C: cat_list_directory(workspace_id, path_base=root, depth=1)
    C->>F: 安全遍历及分页
    F-->>C: 有界目录元数据
    C-->>A: 条目及游标
    A->>C: cat_find_files(workspace_id, glob)
    C-->>A: 规范相对路径
    A->>C: cat_search_text(workspace_id, query, mode=literal)
    C-->>A: 匹配路径 / 行号 / 版本
    A->>C: cat_stat(workspace_id, path, path_base=root)
    C-->>A: 文件属性及版本
    A->>C: cat_read_file(workspace_id, path, path_base=root)
    C-->>A: 文本、版本 V1、续读位置
    loop 存在后续分段
        A->>C: cat_read_file(workspace_id, path, expected_version=V1, continuation)
        C-->>A: 同版本分段或 VERSION_CONFLICT
    end
    A->>C: cat_set_cwd(workspace_id, path, expected_cwd_version)
    C-->>A: 新 cwd / cwd_version
```

continuation 表示 C 文档中的 start_line 或 byte_offset；不是新增参数。返回的工程相对路径再次调用时显式 path_base=root。未启用 P1 时直接使用目录、查找及读取。

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
- CWD 变更后旧相对变更请求被拒绝；显式 root 定位不依赖 CWD
- 读取时 expected_version 可传入，中途内容改变则返回 VERSION_CONFLICT

---

## 4. 单文件修改流程

**源文档：§8.2**

AI 客户端对一个已知版本的文件执行精确编辑或覆盖写入，是 Cat 最核心的写操作路径。

读取、预览和正式提交必须绑定同一原始版本；所有正式变更使用 C 的幂等键和公共授权上下文。预览不是提交或审批。

```mermaid
sequenceDiagram
    participant A as AI MCP 客户端
    participant C as Cat 工具层
    participant O as 统一变更协调器
    participant F as 安全文件层
    A->>C: cat_read_file(workspace_id, path)
    C-->>A: 原始文本 / V1
    A->>C: cat_edit_file(dry_run=true, expected_version=V1, edits)
    C->>C: 授权、原版本精确定位、重叠检查
    C-->>A: 有界 diff / 原版本 / 预览摘要
    Note over A,C: 按客户端和服务端策略完成必要审批
    A->>C: cat_edit_file(request_id, expected_version=V1, edits)
    C->>O: 校验身份及规范请求，查询或预留幂等记录
    O->>O: 获取路径锁并重验 ACL/CWD/路径/版本
    alt 旧版本已变化
        O-->>C: 确定失败 VERSION_CONFLICT
        C-->>A: 重新读取并生成新请求
    else 条件满足
        O->>F: 按第6章执行持久原子提交
        F-->>O: 真实文件状态及持久化证据
        O->>O: 同一数据库事务记录结果、快照和审计
        O-->>C: COMMITTED / 原回执 / V2
        C-->>A: operation_id / V2
        A->>C: cat_read_file(expected_version=V2)
        C-->>A: 同版本回读或后续变更冲突
    end
```

### 网络中断与幂等重试

断线后先查询原工作空间与请求 ID，而非重新生成 ID。对未确认提交的请求，必须区分未受理、处理中与未决。

```mermaid
flowchart TD
    A["断线后以原 workspace_id 和 request_id 查账"] --> B{"当前授权及作用域有效?"}
    B -->|否| X["拒绝; 不泄露记录"]
    B -->|是| S{"查询状态"}
    S -->|COMMITTED| R["采用持久原回执"]
    S -->|IN_PROGRESS| W["继续查询; 不启动第二次操作"]
    S -->|FAILED| F["已终结; 重新评估后才可用新 ID"]
    S -->|IN_DOUBT| I["管理员核验; 禁止重放"]
    S -->|UNKNOWN| U{"服务确认未受理且处于安全窗口?"}
    U -->|是| T["同原作用域、ID、参数重放"]
    U -->|否| I
```

同 ID 同参数仅查回执；不同参数返回 IDEMPOTENCY_CONFLICT。已受理请求保存首次规范路径，重放不得因 CWD 变化转向别处。原 workspace 过期/关闭时仅允许 C 定义的授权查账例外；不得借查账恢复写权限。

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

文件创建与目录创建是两个独立工具。父目录不存在时先明确调用 create_directory，不将 parents 参数传给 write_file。

```mermaid
flowchart TD
    A["cat_write_file(mode=create)"] --> B{"父目录存在且安全?"}
    B -->|否| X["拒绝; 先显式创建目录"]
    B -->|是| C{"目标不存在?"}
    C -->|否| Y["ALREADY_EXISTS"]
    C -->|是| N["统一协调器执行原子 no-replace"]
    D["cat_create_directory"] --> E{"目标不存在且父目录已存在或 parents=true?"}
    E -->|否| Z["拒绝; 不覆盖"]
    E -->|是| P["预检路径链; 逐级安全 mkdir"]
    P --> L["记录已创建目录; 部分失败如实报告"]
    N --> R["持久结果与审计"]
    L --> R
```

### 5.2 移动/复制

```mermaid
flowchart TD
    A["move / copy"] --> B{"同工程、源目的及祖先授权?"}
    B -->|否| X["拒绝"]
    B -->|是| C["稳定顺序锁源/目的"]
    C --> D{"安全类型、源版本、目的不存在?"}
    D -->|否| X
    D -->|是| E{"move 或 copy?"}
    E -->|move| M["同文件系统 no-replace; 子树全授权; 非自身子树"]
    E -->|copy| N["仅普通文件; 安全 fd 流式复制"]
    M --> R["持久结果、审计、必要的 CWD 失效"]
    N --> R
```

### 5.3 删除（回收区模式）

```mermaid
flowchart TD
    A["cat_trash_path"] --> B{"工程根 / 无授权 / 版本冲突?"}
    B -->|是| X["拒绝"]
    B -->|否| C{"非空目录?"}
    C -->|否| T["准备隔离回收对象及清单"]
    C -->|是| D{"recursive=true 且已启用、审批、完整授权及限额?"}
    D -->|否| X
    D -->|是| T
    T --> E{"同文件系统可安全原子移动?"}
    E -->|否| X
    E -->|是| F["持久 PREPARED; 原子移入回收区; fsync"]
    F --> R["结果/修订记录/审计"]
```

回收区在工程外，禁止 MCP 直接访问。工程根不可删除、移动或覆盖；递归目录删除默认禁用。管理员按保留期和配额清理；P1 的 MCP 恢复只支持单文件，目录回收材料由管理员处理。

### 5.4 单文件恢复（P1）

```mermaid
flowchart TD
    A["cat_restore_file"] --> B{"revision 同工程同路径、当前授权、有效且哈希匹配?"}
    B -->|否| X["拒绝; 不猜测历史"]
    B -->|是| C{"当前存在状态和 expected_version 一致?"}
    C -->|否| Y["VERSION_CONFLICT"]
    C -->|是| D{"目标原本不存在?"}
    D -->|是| N["仅 expected_version=absent; 原子 no-replace"]
    D -->|否| M["按当前哈希条件替换"]
    N --> R["新操作 / 新版本 / 审计; 保留历史"]
    M --> R
```

所有路径操作都使用第6章协调器；move/trash 的目录版本与完整子树清单遵循 C。不同操作的预期前后状态必须分别记录，不把删除、目录创建、移动或复制简化成单文件覆盖。复制不改变源；移动/trash 必须对源与目的（包括内部回收对象）状态同时验证。多级 mkdir 不能保证跨目录事务，明确记录实际新增路径与部分失败。

---

## 6. 写入一致性流程（原子提交协议）

**源文档：§9**

Cat 的写入一致性协议确保单文件写入在并发、崩溃、网络中断等场景下不静默破坏文件。

### 6.1 正常写入事务

统一状态机与 E/F 一致：RESERVED → PREPARED → COMMITTED / FAILED / IN_DOUBT。FAILED 仅表示确认终止（包括明确记录的 mkdir 部分效果）；可能已改变目标且无法确认的情况不得当作 FAILED。锁由单进程协调器管理，不是 SQLite 路径锁。SQLite 短事务不跨文件 I/O；同步模式 FULL。所有正式 mutation 共用此协议。

```mermaid
flowchart TD
    A["鉴权、校验 Schema、规范请求摘要"] --> B{"幂等键已存在?"}
    B -->|是| R["重新鉴权后返回原结果/状态或 IDEMPOTENCY_CONFLICT"]
    B -->|否| C["短事务持久预留 RESERVED"]
    C --> D["稳定顺序路径锁; 重验授权、CWD、根身份、类型、版本、配额"]
    D --> E{"条件、审批及审计预留通过?"}
    E -->|否| F["确定失败; 记录 FAILED/审计"]
    E -->|是| G["构造目标字节或完整操作清单; 备份并 fsync"]
    G --> H["持久 PREPARED: 前后状态、路径身份、备份/暂存引用、预期结果"]
    H --> I["重新核验; 执行受控文件变更"]
    I --> J["fsync 受影响文件和全部父目录; 校验实际状态"]
    J --> K{"结果可证明且最终数据库事务成功?"}
    K -->|是| L["同一短事务: COMMITTED + revision + audit + 安全回执"]
    K -->|否| M["保留 journal/备份; IN_DOUBT 或 PREPARED 待恢复; 冻结范围"]
    L --> N["释放锁并返回成功"]
    F --> Q["释放锁并返回明确失败"]
    M --> Z["返回 IN_DOUBT 或断线; 禁止重放"]
```

旧内容备份、暂存对象及其目录需先可靠持久化；PREPARED 必须在第一次可能改变工程目标的操作前持久化完整可恢复清单（包括预期 after 版本，不能等 rename 后才计算）。普通文本/二进制写使用同目录独占临时文件，保留必要属性、fsync、原子 rename/no-replace，再 fsync 父目录。移动跨父目录时同步两个父目录；软删除回收区必须满足同文件系统及持久清单要求。无需生成文件的操作按实际类型执行。若 rename 前失败且确认没有目标变更，才能确定 FAILED；rename 后 fsync 或 DB 提交失败属于可能变更，必须保持未决并阻止进一步写。清理失败不得把已提交结果改写成失败。

### 6.2 崩溃恢复

启动前恢复扫描覆盖 RESERVED、PREPARED 和 IN_DOUBT；禁止在恢复检查完成前开放受影响范围。依据持久 journal 中工程 generation、根身份、源/目的、before/after 清单、对象身份、预期存在性和备份/暂存证据判断，而非仅看目标 hash。

```mermaid
flowchart TD
    A["启动; 读取 RESERVED/PREPARED/IN_DOUBT"] --> B["锁定涉及路径; 校验根身份及清单"]
    B --> C{"可证明仅 RESERVED 且未改目标?"}
    C -->|是| F["FAILED: 终结原请求; 清理已证实孤立暂存"]
    C -->|否| D{"完整前后状态和持久证据能唯一判定?"}
    D -->|已应用| E["重验并 fsync; 原子写入最终结果/修订/审计"]
    D -->|未应用| G["FAILED: 保留必要证据; 安全清理"]
    D -->|明确部分 mkdir| P["记录已新增目录及部分失败; 不误删既有目录"]
    D -->|无法确定| H["IN_DOUBT; 冻结全部关联路径和层级冲突范围"]
    E --> I["COMMITTED"]
    H --> J["管理员核验; 仅按证据决议"]
```

before/after 相同的无实质变化、路径被重建、外部修改、清单不全或审计未能落盘时不能以 hash 相等自动宣称成功。已知无变化的操作可在变更前直接持久终结为 COMMITTED（no-op 回执），不做不必要的 rename。目录操作按身份和完整清单验证，不能安全判定则冻结。管理员决议也必须留下审计与结果，不能删除未决记录或盲目覆盖。

### 6.3 并发边界说明

| 情形 | Cat 保障 | 边界/风险 |
|---|---|---|
| 多个 Cat 客户端同版本写入 | 共享锁、版本及条件检查；实际不同内容变更最多一方成功 | 相同内容 no-op 可成功，不表示第二次实际写入 |
| 本地 IDE 或外部进程 | 写前再核验、写后校验及备份 | 非协作进程不受 Cat 锁约束，存在 TOCTOU |
| 严格生产模式 | 专用受控工程/隔离工作树或统一写协调 | 无法满足 OS 边界时拒绝公网写 |
| 共享 live workspace | 明确告知风险并由维护者协调 | 不承诺绝对无丢失或 POSIX 通用 CAS |

### 6.4 备份、清理与幂等保留

备份对象位于工程外，严格 OS 权限或加密；默认保留7天，配置有限正数配额，不允许默认无限。保留期不能直接删除未决事务、活动上传或仍被引用的对象。清理按状态、引用、期限和配额锁定执行，保留历史元数据并标记快照不可用。幂等回执与备份保留分开；在 workspace 仍可提交或存在未决操作时不得移除请求键。过期窗口外不允许从 UNKNOWN 推断可以重放；优先保留最小请求墓碑，防止重新执行。

### 6.5 分块传输（P1，CAT-33）

```mermaid
flowchart TD
    A["begin_upload: 授权、目标条件、总大小/哈希、配额"] --> B["创建绑定主体/workspace/目标的暂存会话"]
    B --> C["upload_chunk: 核对序号、偏移、块哈希"]
    C --> D{"重复块或下一合法块?"}
    D -->|否| X["拒绝; 目标不变"]
    D -->|是| E["确认暂存持久化及已接受块记录"]
    E --> F{"全部字节接收?"}
    F -->|否| C
    F -->|是| G["finish_upload: 校验总哈希、当前授权、旧版本"]
    G --> H["统一文件变更事务; 仅此处修改目标"]
    H --> R["持久回执 / 新版本"]
    B --> I["abort/过期: 仅清理已确定未提交暂存"]
    E --> I
```

每次操作重验会话绑定、租期和配额；重复块必须相同，断线或重启按持久块清单与暂存内容恢复，不确定时拒绝继续/保留未决提交。finish 绑定唯一正式请求 ID；不得把最后一个 upload_chunk 当作自动提交。下载 read_chunk 以原始版本固定字节范围和校验，版本变化必须重新开始。

---

## 7. 权限校验流程

**源文档：§10 §7**

四级校验 + 默认 deny 策略，每次工具调用均需完整走通。

```mermaid
flowchart TD
    A["请求到达"] --> B{"有效身份、token issuer/audience/expiry/scope?"}
    B -->|否| X["UNAUTHENTICATED"]
    B -->|是| C{"是否工程级工具?"}
    C -->|否| G["仅能力或授权工程发现; 不返回未授权元数据"]
    C -->|是| D{"主体、project、workspace、generation、租期及 Grant 有效?"}
    D -->|否| Y["FORBIDDEN 或 WORKSPACE_EXPIRED"]
    D -->|是| E{"scope 与 ACL 与 access 交集包含所需独立动作?"}
    E -->|否| Y
    E -->|是| F{"所有规范路径、祖先、源目的及 denylist 合法?"}
    F -->|否| Z["FORBIDDEN / UNSAFE_PATH / PATH_OUTSIDE_ROOT"]
    F -->|是| H{"配额及必要审批通过?"}
    H -->|否| W["RATE_LIMITED / APPROVAL_REQUIRED"]
    H -->|是| I["允许执行; 写入持锁再次验证"]
```

open_workspace 尚无 workspace；由有效主体和项目授权建立。get_operation 的历史只读例外按 C，不得重开权限。拒绝事件也记录脱敏审计；访问前必须完成路径策略，允许文件中命中秘密才拦截或脱敏。

### 权限分离粒度

| 权限 | 对应工具举例 |
|------|-------------|
| discover | cat_list_projects |
| read | cat_read_file, cat_list_directory, cat_search_text, cat_stat, cat_project_info |
| write | cat_write_file, cat_edit_file, cat_apply_patch, cat_create_directory, cat_move_path, cat_copy_file |
| delete | cat_trash_path |
| restore | cat_restore_file |
| audit | cat_list_changes |
| read（含历史路径授权） | cat_diff_file、cat_get_operation、cat_git_status、cat_git_diff、cat_read_chunk |
| workspace | cat_set_cwd、cat_close_workspace |

### 关键规则

- 默认 deny，未显式授权即为拒绝
- OAuth scopes、当前 ACL、项目 access 上限取交集，动作独立不继承；copy 需源 read 与目的 write，move 需源目的 write，restore 需 restore 及对应历史路径授权
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
    User->>ChatGPT: 按当前账户支持的自定义 MCP 设置添加
    ChatGPT->>Cat: 输入 endpoint URL: https://cat.example.com/mcp
    ChatGPT->>IdP: OAuth 授权流程
    IdP-->>ChatGPT: access token
    ChatGPT->>Cat: MCP 初始化
    Cat-->>ChatGPT: 初始化响应
    ChatGPT->>Cat: initialized 通知及 tools/list
    Cat-->>ChatGPT: 实际启用工具列表
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
    Cat-->>Client: 初始化响应
    Client->>Cat: initialized 通知及 tools/list
    Cat-->>Client: 实际启用工具列表
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
| 账户是否支持自定义写 | ChatGPT Plus vs Business/Enterprise/Edu/Pro | 以目标账户当时权限及 CAT-06 实测为准，不承诺套餐能力 |
| B 路线账户场景 | 当前网页对话不支持自定义写入 | 改用 API 或其他客户端 |
| 写工具审批 | 客户端/服务端共同控制 | 按客户端及服务端真实策略完成必要审批；审批拒绝不改变工程 |
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
- **工程理解**通常先于修改；已有可信路径时可直接调用，但服务端始终重验路径、版本或不存在条件
- **单文件修改**和 **创建/移动/删除/恢复** 共享同一套写入一致性协议
- **审计记录**覆盖所有调用及拒绝事件；写入成功前须与最终操作回执一致持久化
- 跨文件修改不声明多文件原子事务，由 AI 客户端逐文件编排

---

*本文档对应源 `docs/Cat-总体需求与技术实现方案.md`，各章节编号以 v1.2 版本为准。*
