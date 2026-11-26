# A-系统需求文档

> 项目：Cat — 远程工程文件系统 MCP 服务
> 版本：v1.0
> 日期：2026-11-26

## 总述

Cat 是面向 AI 编程的远程工程文件系统 MCP（Model Context Protocol）服务。远端 AI 通过标准 MCP 调用，直接读写公网服务器上经授权的真实工程目录，包括 Git 未跟踪、未提交文件以及非 Git 工程；不依赖 GitHub 提交作为文件交换中介。Cat 以单文件为读写和编辑单位，支持多工程发现、目录分级浏览、文件内容搜索、精确局部编辑、版本控制、权限审计与变更恢复。

**目标用户：** Cat 自托管维护者（安装、配置、管理 Cat 服务的运维人员）和 AI MCP 客户端（通过 MCP 协议接入 Cat 进行远程编程的 AI 应用，如 ChatGPT、Claude Code、OpenAI Responses API 等）。

**核心能力边界：** Cat 负责确定性地执行文件操作，不运行 AI 模型，不替代 IDE、编译器、版本控制或任务管理平台。V1 不提供任意 Shell、SSH、联网下载、远程桌面、自动提交/推送 Git、模型执行、自动修改部署配置或生产环境。文件系统权限不等于执行权限。

---

## 1. 修订记录

| 版本 | 日期 | 修订内容 | 修订人 |
|------|------|----------|--------|
| v1.0 | 2026-11-26 | 初始版本，从 Cat-总体需求与技术实现方案 v1.2 正向梳理全部需求 | 产品经理 |

---

## 2. 引言

### 2.1 背景

当前 AI 编程主要通过 GitHub 提交作为文件交换中介，流程冗余且无法实时感知工程目录中的未跟踪、未提交文件。AI 需要一个直接、安全、可授权的文件系统访问通道，能够从未知工程开始逐层理解并完成实际代码修改，而不将整个工程塞进上下文。

### 2.2 目标

以单文件为读写和编辑单位，提供以下核心能力：

- 发现多个工程，按目录深度分页浏览
- 搜索路径与文件内容
- 选择工程和当前相对工作目录
- 分段读取大文件
- 精确编辑局部内容
- 创建、移动、复制、删除文件及目录
- 查询并恢复变更

### 2.3 范围

Cat 需求范围覆盖 R01–R16 共 16 项需求，按 P0（28 项实施任务）、P1（10 项实施任务）、P2（2 项实施任务）三级优先级组织。本文档仅包含需求规格，不涉及技术实现细节、代码结构或测试计划。

### 2.4 术语定义

| 术语 | 定义 |
|------|------|
| MCP | Model Context Protocol，模型上下文协议，AI 应用与工具服务之间的标准化通信协议 |
| Streamable HTTP | MCP 的 HTTP 传输方式，支持服务端推送 |
| Workspace | 工作空间，AI 客户端在 Cat 上打开的独立会话，绑定登录主体和工程，持有虚拟 CWD |
| CWD | 当前工作目录（Current Working Directory），以工程根为基点的逻辑相对路径 |
| CWD Version | 工作目录版本号，用于检测 CWD 变更 |
| Project | 工程，由管理员注册的真实服务器目录，每个工程有独立 ID 和授权策略 |
| ACL | 访问控制列表，定义主体对工程的访问权限（discover/read/write/delete/restore/audit） |
| OAuth | 开放授权协议，用于第三方身份认证和授权 |
| SHA-256 | 安全哈希算法，用于文件内容的版本标识 |
| TOCTOU | Time-of-check Time-of-use，检查时刻与使用时刻之间的竞态窗口 |
| IdP | 身份提供者（Identity Provider），负责用户身份认证的服务 |

---

## 3. 用户角色

### 3.1 Cat 自托管维护者

- 安装、配置和升级 Cat 服务
- 管理反向代理（Caddy/Nginx）和 TLS 证书
- 注册并授权工程目录
- 配置 ACL 权限策略
- 配置 OAuth 身份提供者
- 管理审计日志和备份
- 监控系统运行状态和资源配额
- 执行升级、回滚和故障恢复

### 3.2 AI MCP 客户端

- 通过 MCP 协议连接 Cat 服务（ChatGPT、Claude Code、Codex、OpenAI Responses API 等）
- 完成 OAuth 身份认证
- 发现和打开授权工程
- 浏览目录结构、读取文件内容
- 搜索文件名和文件内容
- 执行文件编辑、创建、移动、复制、删除操作
- 查询操作状态、变更历史和文件版本
- 恢复文件到历史版本

---

## 4. 功能需求

### R01：公网 HTTPS MCP 接入

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-A 接入验证
- **描述：** Cat 服务必须通过公网 HTTPS 对外暴露 MCP 端点，支持标准 MCP 协议初始化、工具发现、工具调用和身份鉴权全流程。支持 Streamable HTTP 传输方式。所有 MCP 通信必须经过 TLS 加密，仅开放 443 端口，通过反向代理转发到 Cat 内部服务。
- **输入：** 标准 MCP 协议请求（初始化、工具列表、工具调用）
- **输出：** 标准 MCP 协议响应，包括协议版本协商、工具 JSON Schema 列表、工具调用结果
- **验收标准：**
  - 使用真实目标客户端（如 ChatGPT、OpenAI Responses API）完成初始化、工具发现、工具调用全流程
  - 鉴权通过时正常返回结果，鉴权失败时返回标准错误
  - 支持 Streamable HTTP 传输
  - R01 是首个门禁，必须在目标 ChatGPT 账户上实测读写，不以 MCP 服务端可运行替代客户端验收

### R02：工程注册与发现

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-B 只读基座
- **描述：** 管理员通过配置注册工程目录，Cat 仅返回当前授权主体有权访问的工程列表。支持在一个或多个 discovery_root 下扫描一级目录作为候选工程，经管理员批准后方可读写。工程 ID 不应依赖目录名，支持多个 discovery_root。
- **输入：** 工程查询参数（可选的 query、cursor、limit）
- **输出：** 授权工程列表（工程 ID、显示名、根相对名称、权限摘要、类型说明），不返回其他工程真实路径
- **验收标准：**
  - 仅返回当前主体有权访问的工程
  - 未授权工程完全不暴露（不泄露路径和存在信息）
  - 支持按名称查询和分页

### R03：层级目录浏览

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-B 只读基座
- **描述：** 按深度、过滤、分页、排序浏览工程目录树。路径参数可复用。最大 depth 为 8，depth=0 只返回目录自身元数据，depth=1 列直接子项。支持 include/exclude 过滤模式，默认忽略 node_modules、target、dist、build、.git 及 .gitignore 匹配项。使用有界快照/游标分页，cursor 与主体、工程、参数和快照绑定。
- **输入：** path（可选）、path_base（可选）、depth（可选，默认 1）、cursor（可选）、limit（可选）、include（可选）、exclude（可选）
- **输出：** 目录树条目列表（path、name、type、depth、size、mtime、可选 child_count），包含 has_more、next_cursor、truncated_reason 分页信息
- **验收标准：**
  - 支持 depth 0-8 级深度浏览
  - 分页不丢页、不重复，截断时明确标示
  - 默认过滤目录生效
  - cursor 过期返回 CURSOR_EXPIRED，调用方重启扫描

### R04：当前工程和工作目录

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-B 只读基座
- **描述：** 每个 AI 客户端通过 Workspace 会话持有独立的当前工程和虚拟 CWD。不同工作空间互不影响。CWD 是逻辑路径，支持 '.'、'..' 和子目录，但不得越出工程根。Cat 永远不切换系统进程工作目录。CWD 变更后旧相对写请求返回 CWD_CONFLICT。Workspace ID 为不可预测随机 ID，必须绑定登录主体。
- **输入：** 工具调用时传入 workspace_id、cwd_version
- **输出：** workspace_id、cwd、cwd_version、project_id、期限信息
- **验收标准：**
  - 两个 AI 客户端打开同一工程，CWD 各自独立不串扰
  - set_cwd 支持相对导航但禁止越出工程根
  - CWD 变更后旧版本写请求被拒绝
  - Workspace 空闲可配置超时（建议 2 小时）、最长 24 小时后自动关闭

### R05：单文件读取

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-B 只读基座
- **描述：** 支持按整文件或按行/字节分段读取，返回版本（原始字节 SHA-256）、编码、行号。分段读取支持 start_line、end_line、max_bytes 参数，行号 1-based 闭区间，正文不嵌入行号。长单行按 byte offset 续读，游标不切断 UTF-8 字符。支持 UTF-8、BOM、LF/CRLF 编码检测，未知编码返回明确错误或以二进制读取。小文件可整读。
- **输入：** path、start_line（可选）、end_line（可选）、max_bytes（可选）、expected_version（可选）
- **输出：** path、version（sha256:xx）、encoding、newline、start_line、end_line、total_lines、content、has_more、next_start_line
- **验收标准：**
  - 精确返回指定行范围的内容和行号
  - 版本由文件原始字节 SHA-256 计算
  - 编码检测正确（UTF-8/GBK/二进制等）
  - 分段读取中途文件变更返回 VERSION_CONFLICT
  - 不可拼接不同版本的内容

### R06：工程代码检索

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-B 只读基座
- **描述：** 支持按文件名、路径 glob、文件内容字面量与正则搜索。搜索输出匹配路径、行号、短上下文。正则搜索使用 RE2 或 ripgrep 固定 argv，不拼接 shell，不跟随符号链接，禁止高风险模式。限制扫描字节、时间及结果数。标准库 fallback 仅提供有界字面量查询，正则不可用时返回能力限制，不静默采用无超时 Python re。
- **输入：** path（可选）、query、mode（literal/regex）、include（可选）、exclude（可选）、context_lines（可选）、cursor（可选）、limit（可选）
- **输出：** 匹配列表（path、行号、短上下文行、版本）
- **验收标准：**
  - 文件名搜索支持 glob 模式
  - 内容搜索支持字面量和正则模式
  - 搜索有超时控制（默认 10 秒），超时返回明确截断
  - 不跟随符号链接
  - 结果数有上限限制

### R07：创建与整体写入

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-C 写能力
- **描述：** 支持创建新文件和覆盖替换已有文件。create 模式要求目标文件不存在，replace 模式必须指定旧版本（expected_version），不提供无条件覆盖或 force。所有写入操作需携带唯一 request_id 实现幂等：同一 request_id 和相同参数重试返回原回执，不同参数返回 IDEMPOTENCY_CONFLICT。空文件合法。
- **输入：** path、content、mode（create/replace）、expected_version（replace 必填）、request_id、cwd_version（可选）
- **输出：** 写入回执（ok、operation_id、新版本号）
- **验收标准：**
  - create 模式目标已存在时拒绝写入
  - replace 模式旧版本不匹配时拒绝写入
  - 同一 request_id 重试返回相同回执且不重复写
  - 不同参数复用 request_id 返回 IDEMPOTENCY_CONFLICT

### R08：精确单文件编辑

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-C 写能力
- **描述：** 支持对单个文件进行精确的局部编辑。edits 支持 replace_exact（默认必须唯一匹配）、replace_lines（原版本和预期旧文本）、insert_before/after（唯一锚点）。同一文件多个编辑在内存中预检全部匹配，拒绝重叠范围及模糊替换。支持 dry_run（返回预览不修改）。提交前复核同一版本。支持统一 diff 严格限制一个文件，拒绝 rename、路径跳转、二进制、多文件补丁、模糊 hunk。
- **输入：** path、expected_version、edits（编辑操作列表）、dry_run（可选）、request_id（可选）、cwd_version（可选）
- **输出：** dry_run 时返回有界 diff、目标原版本、预览摘要；实际执行返回 operation_id、新版本
- **验收标准：**
  - replace_exact 匹配必须唯一，重复文本返回 AMBIGUOUS_MATCH
  - 多处编辑重叠范围拒绝提交
  - dry_run 不修改文件内容
  - 版本不匹配返回 VERSION_CONFLICT

### R09：文件属性与目录操作

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-C 写能力
- **描述：** 支持查询文件属性（stat）、创建目录（mkdir）、重命名/移动（rename/move）、复制（copy）、软删除（trash）。stat 返回 type/size/mtime/version/encoding。mkdir 支持 parents 选项。move/copy 默认不覆盖，源和目的都需授权，跨工程移动 V1 禁止。trash 将文件移至工程专属回收区，递归目录删除禁用或需服务端独立审批。
- **输入：**
  - stat: path, path_base（可选）
  - create_directory: path, parents（可选）, request_id, cwd_version（可选）
  - move_path: source, destination, expected_version, request_id, cwd_version（可选）
  - copy_file: source, destination（预期版本）, request_id, cwd_version（可选）
  - trash_path: path, expected_version, recursive（可选）, request_id, cwd_version（可选）
- **输出：** 操作回执及新版本号
- **验收标准：**
  - stat 正确返回文件类型、大小、修改时间、版本、编码
  - mkdir 递归创建成功
  - move 不覆盖目标，目标存在则拒绝
  - copy 不覆盖目标
  - trash 成功移至回收区，不直接删除
  - 工程根不允许删除、移动或覆盖

### R10：并发控制与可恢复性

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-C 写能力
- **描述：** 以工程 ID + 规范路径为锁键，移动锁定源及目的。持锁后校验权限、路径、文件类型、expected_version。写入前保存旧字节到受限对象存储、SQLite 记录 PREPARED。原子写入（同目录独占临时文件、写入、校验、fsync、原子 rename、fsync 父目录）后记录 COMMITTED、新版本及审计。失败清理临时文件。重试同一 request_id 返回原回执。Cat 内部并发冲突只有一方成功，外部编辑器竞态拒绝并记录。
- **输入：** 写入操作中的 request_id、expected_version、cwd_version
- **输出：** 成功时返回 operation_id、新版本；冲突时返回 VERSION_CONFLICT
- **验收标准：**
  - 两个 Cat 客户端写同一文件，只有一方成功，另一方收到冲突
  - 进程在 PREPARED/rename/COMMITTED 间崩溃后可恢复或进入 IN_DOUBT
  - 同一 request_id 重试不二次写入
  - 外部编辑器修改后 Cat 检测到并拒绝

### R11：权限和审计

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P0
- **所属阶段：** P0-C 写能力
- **描述：** 每次调用验证主体、工程、路径、动作、配额。四级权限模型：主体 -> 工程 -> 路径前缀 -> 动作。权限动作分离：discover/read/write/delete/restore/audit。默认 deny。每次请求都进行鉴权，不依赖会话缓存。审计记录主体、工程、路径、动作、旧/新版本、字节数、结果、request_id、时间。审计单独存储、权限隔离，审计失败时写操作 fail closed。
- **输入：** 所有工具调用均需携带身份令牌和 workspace_id
- **输出：** 鉴权失败返回 UNAUTHENTICATED/FORBIDDEN；成功执行后记录审计
- **验收标准：**
  - 未授权令牌、过期、撤销、错误 audience 均拒绝
  - 无项目数据泄露（包括工程存在信息）
  - 审计日志可追溯每笔操作
  - 审计失败时写操作拒绝执行

### R12：工程概要与 Git 只读状态

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P1
- **所属阶段：** P1 工程增强
- **描述：** 支持查询工程概要信息，包括项目类型、关键文件清单（README、AGENTS、构建清单）、语言估计。支持只读查询 Git 状态（HEAD commit、dirty 状态）和 Git 差异。不执行 Git commit/push 操作。
- **输入：** workspace_id
- **输出：** 工程概要（manifest 名称、语言估计、Git HEAD、dirty 状态）、Git 状态/差异（只读）
- **验收标准：**
  - 工程概要正确反映工程结构和特征
  - Git 状态只读，不执行任何写操作
  - 非 Git 工程正常返回无 Git 信息

### R13：变更记录、对比和回滚

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P1
- **所属阶段：** P1 工程增强
- **描述：** 支持查询工程授权范围内的操作记录变更历史，支持按文件查看有界差异，支持单文件条件恢复至历史版本。恢复时检查当前版本或不存在状态，禁止覆盖后来的改动。单文件回滚保留外部后续修改能力。
- **输入：**
  - list_changes: cursor（可选）, limit（可选）
  - diff_file: path, revision_id（可选）, git_base（可选）
  - restore_file: path, revision_id, expected_version, request_id, cwd_version（可选）
- **输出：** 变更记录列表、文件差异、恢复回执或冲突
- **验收标准：**
  - 变更历史可分页查询
  - diff 正确显示文件变更内容
  - 恢复时目标已被后续修改则拒绝覆盖
  - 回收区清理由管理员计划执行

### R14：二进制文件

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P1
- **所属阶段：** P1 工程增强
- **描述：** 支持受限的分块上传和下载二进制文件。严格大小、块序号、偏移和 SHA-256 哈希校验，finish 时才原子提交。超大文本文件也可通过分块方式创建。支持工具：cat_begin_upload、cat_upload_chunk、cat_finish_upload、cat_read_chunk、cat_abort_upload，均绑定单文件和授权 workspace。
- **输入：** path、总大小、块序号、偏移、数据块、校验和
- **输出：** 上传回执、校验确认、下载数据块
- **验收标准：**
  - 分块上传在最后一块完成时原子提交
  - 每块校验 SHA-256
  - 中断后查询操作状态可恢复
  - 大小和块数受配额限制

### R15：符号级理解

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P2
- **所属阶段：** P2 扩展
- **描述：** 可选扩展，支持代码符号级导航和理解。工具包括 cat_list_symbols、cat_find_definition、cat_find_references，返回符号路径和范围，由 cat_read_file 读取真实内容。可选用 LSP（Language Server Protocol）或 Serena 兼容方案实现。不阻塞 V1 发布。
- **输入：** workspace_id、符号名称或路径
- **输出：** 符号定义位置、引用位置列表、符号概要
- **验收标准：**
  - 符号定义和引用返回正确的文件路径和行列范围
  - 禁用后不影响 V1 正常功能
  - 不将 GPL 协议代码纳入 MIT 项目

### R16：独立构建测试

<!-- 来源:产品经理 | 状态:待实现 -->

- **优先级：** P2
- **所属阶段：** P2 扩展
- **描述：** 可选扩展，提供独立授权的预定义构建和测试执行服务。使用预定义任务白名单、隔离执行环境、限时和日志分页。需另行授权，不暴露任意 Shell。不阻塞 V1 发布。
- **输入：** workspace_id、任务标识符
- **输出：** 执行状态、日志输出（分页）、退出码
- **验收标准：**
  - 仅执行白名单中预定义的任务
  - 执行环境隔离，不能访问宿主文件系统
  - 超过时间限制自动终止
  - 禁用后不影响 V1 正常功能

---

## 5. 非功能需求

### 5.1 安全需求

<!-- 来源:产品经理 | 状态:待实现 -->

| 编号 | 需求 | 详细描述 |
|------|------|----------|
| NF01 | 网络与传输安全 | 仅开放 443 端口（HTTPS），反向代理到 loopback/private container；TLS 加密、Host/Origin 校验、请求体上限、认证限速 |
| NF02 | 身份认证 | 使用标准 MCP OAuth 发现、PKCE、精确 redirect URI、短期 token、支持 token 撤销；优先成熟 IdP，不自行发明登录协议；不将 token 写入 URL、源码、审计 |
| NF03 | 四级权限模型 | 主体 -> 工程 -> 路径前缀 -> 动作；默认 deny；discover/read/write/delete/restore/audit 权限分离；OAuth scopes 与 ACL 取交集 |
| NF04 | OS 隔离 | 非 root 运行、只挂载指定工程、独立 UID/容器、只读系统根、禁止 Docker socket/宿主 home/SSH 凭据、drop capabilities、no-new-privileges、资源配额；项目 worker 默认无公网出站 |
| NF05 | 路径安全 | fd-relative / openat2 安全路径解析；禁止 symlink 穿越及写入；拒绝特殊设备、FIFO、socket、硬链接；拒绝 ../ 跳出根目录 |
| NF06 | 敏感文件保护 | .env、私钥、凭证、生产配置、Git 内部配置等 denylist；搜索/目录/读/写/备份均执行同一策略；匹配秘密的内容额外阻断或脱敏 |
| NF07 | Prompt 注入防护 | 项目文档/源码/注释视为不可信数据；文件内指令没有授权效力；工具只执行授权的结构化操作 |
| NF08 | 审计 | 记录主体、工程、路径、动作、旧/新版本、字节数、结果、request_id、时间；单独存储、权限隔离、可外送日志；审计失败时写操作 fail closed |
| NF09 | 资源配额 | 每主体/工程限制并发操作数、搜索超时、磁盘配额、临时文件和备份容量；超量拒绝并返回重试建议 |
| NF10 | 错误安全 | 不泄露主机绝对路径、内部堆栈、工程存在信息等敏感上下文 |

### 5.2 性能需求

<!-- 来源:产品经理 | 状态:待实现 -->

| 编号 | 需求 | 目标值 |
|------|------|--------|
| NF11 | 目录浏览性能 | 10 万文件测试工程，depth=1 目录页 P95 小于 500ms（服务端暖态、不含公网） |
| NF12 | 文件读取性能 | 64 KiB 文本读 P95 小于 300ms（服务端暖态、不含公网） |
| NF13 | 搜索超时 | 搜索 10 秒内返回结果或明确超时 |
| NF14 | 结果大小限制 | 单次结果默认 64 KiB，可调但不得超客户端限制 |
| NF15 | 并发限制 | 每主体最大并发操作数 4（可配置） |
| NF16 | 工作空间空闲超时 | 默认空闲 2 小时后自动关闭，最长 24 小时（可配置） |
| NF17 | 备份保留 | 备份默认保留 7 天，按配额清理（可配置） |
| NF18 | 目录深度上限 | 最大 depth=8 |
| NF19 | 分页条目上限 | 默认每页 200 条目（可配置） |
| NF20 | 最大内联写入 | 单次直接写入最大 256 KiB，超大文本可用分块上传 |

### 5.3 可靠性需求

<!-- 来源:产品经理 | 状态:待实现 -->

| 编号 | 需求 | 详细描述 |
|------|------|----------|
| NF21 | 写入原子性 | 原子 rename、fsync，崩溃后恢复或进入可管理的 IN_DOUBT 状态 |
| NF22 | 幂等性 | 唯一 request_id，重试同一请求返回原回执，不同参数复用 ID 拒绝 |
| NF23 | 版本一致性 | 写前校验 expected_version，不匹配拒绝；分段读取内容中途变更返回 VERSION_CONFLICT |
| NF24 | 恢复机制 | 重启后扫描 PREPARED 状态：按 before/after hash 判断已提交、未提交或 IN_DOUBT，不确定时冻结该路径等待管理员 |
| NF25 | 并发安全 | 文件锁约束 Cat 客户端；外部编辑器竞态记录共享模式限制，不支持自动 Git 合并 |

---

## 6. 外部接口需求

### 6.1 MCP 协议接口

<!-- 来源:产品经理 | 状态:待实现 -->

Cat 通过标准 MCP 协议对外暴露能力，使用 Streamable HTTP 传输方式，兼容 MCP 2025-era 客户端及后续版本。所有工具名称使用 `cat_` 前缀。

#### 6.1.1 核心工具接口（23 项 V1 工具）

**系统与工程（5 项）：**

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_get_capabilities | 无 | 协议/工具版本、限额 | read |
| cat_list_projects | query?, cursor?, limit? | 授权工程列表 | discover |
| cat_open_workspace | project_id, cwd? | workspace_id、cwd_version | read |
| cat_get_workspace | workspace_id | 当前上下文、期限 | read |
| cat_set_cwd | workspace_id, path, expected_cwd_version | 新 cwd/版本 | workspace |
| cat_close_workspace | workspace_id | 关闭会话 | workspace |

**读取与浏览（5 项）：**

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_project_info | workspace_id | manifest 名称、语言估计、Git 概况 | read |
| cat_list_directory | path?, path_base?, depth?, cursor?, limit?, include?, exclude? | 目录树条目及分页 | read |
| cat_stat | path, path_base? | type/size/mtime/version/encoding | read |
| cat_find_files | path?, glob, cursor?, limit? | 文件名、相对路径 | read |
| cat_search_text | path?, query, mode, include?, exclude?, context_lines?, cursor?, limit? | 匹配路径、行号、短上下文 | read |
| cat_read_file | path, start_line?, end_line?, max_bytes?, expected_version? | 单文件文本、行范围、版本 | read |

**写入与编辑（6 项）：**

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_write_file | path, content, mode, expected_version?, request_id, cwd_version? | 写入回执、新版本 | write |
| cat_edit_file | path, expected_version, edits, dry_run?, request_id?, cwd_version? | diff 或新版本 | write |
| cat_apply_patch | path, patch, expected_version, dry_run?, request_id?, cwd_version? | 单文件严格补丁 | write |

**目录与文件管理（5 项）：**

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_create_directory | path, parents?, request_id, cwd_version? | 创建回执 | write |
| cat_move_path | source, destination, expected_version, request_id, cwd_version? | 不覆盖目标的重命名/移动 | write |
| cat_copy_file | source, destination, expected_version, request_id, cwd_version? | 不覆盖的单文件复制 | write |
| cat_trash_path | path, expected_version, recursive?, request_id, cwd_version? | 软删除/回收记录 | delete |

**查询（3 项）：**

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_list_changes | cursor?, limit? | 操作变更记录 | audit |
| cat_get_operation | request_id | 已执行/失败/进行中/未知 | read |
| cat_diff_file | path, revision_id?, git_base? | 有界差异 | read |
| cat_restore_file | path, revision_id, expected_version, request_id, cwd_version? | 条件恢复或冲突 | restore |

#### 6.1.2 P1 增强接口（7 项）

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_git_status | workspace_id | Git HEAD、dirty 状态 | read |
| cat_git_diff | workspace_id, path | 只读 Git 差异 | read |
| cat_begin_upload | path, total_size | 上传会话 ID、首块确认 | write |
| cat_upload_chunk | session_id, offset, data, checksum | 块确认 | write |
| cat_finish_upload | session_id, checksum | 原子提交回执 | write |
| cat_read_chunk | path, offset, size | 二进制数据块 | read |
| cat_abort_upload | session_id | 中止并清理 | write |

#### 6.1.3 P2 扩展接口（3 项）

| 工具 | 核心输入 | 输出/作用 | 权限 |
|------|----------|-----------|------|
| cat_list_symbols | workspace_id, path? | 符号概要列表 | read |
| cat_find_definition | workspace_id, symbol, path? | 定义位置 | read |
| cat_find_references | workspace_id, symbol, path? | 引用位置列表 | read |

受控执行接口名称待独立设计，不属于本轮强制新增范围。

### 6.2 接口规范

- **命名：** 所有工具以 `cat_` 前缀开头
- **参数：** 除 cat_get_capabilities、cat_list_projects、cat_open_workspace 外，所有工具均需 workspace_id
- **只读标注：** 只读工具使用 readOnlyHint=true，写入工具 readOnlyHint=false
- **破坏性标注：** 覆盖、删除、恢复等使用 destructiveHint=true
- **幂等性：** 所有写操作需携带唯一 request_id 实现幂等
- **错误响应：** 统一结果格式 `{ok, request_id, workspace_id, data?, error?:{code, message, retryable, details}}`
- **版本语义：** 文件版本通过文件原始字节 SHA-256 计算
- **不启用能力：** 未实现的工具不得在 tools/list 中伪装为已可用

---

## 7. 阶段划分

<!-- 来源:产品经理 | 状态:待实现 -->

### 7.1 P0-A：接入验证（基础与接入）

**阶段目标：** 初始化 Python 公开包，固定参考版本和许可，建立配置模板和 CI；完成真实客户端 MCP 接入试验。

**通过条件：**
- Python 工程可运行，参考与依赖许可范围明晰
- 有真实客户端读写回执
- 确认可用账户路线（ChatGPT/OpenAI Responses API 或其他）
- 不支持写操作的客户端要显式记录

**涵盖任务：** 工程初始化、许可门禁、配置契约、协议和状态基础、身份验证、真实客户端试验。

### 7.2 P0-B：只读基座

**阶段目标：** 实现工程注册/发现、ACL、工作空间、CWD、目录分页、文件属性查询、分段文件读取、文件搜索；两个工程/两会话隔离。

**通过条件：**
- 两个项目、两个 workspace 隔离测试通过
- CWD 不串扰，权限正确
- 大工程（如 10 万文件）浏览与搜索通过
- 只读内部验收 G2 通过

**涵盖任务：** 持久状态、工程注册、ACL、安全路径、敏感策略、工作空间、目录浏览、文件读取、文件搜索、资源配额。

### 7.3 P0-C：写能力（可靠写入与核心验收）

**阶段目标：** 实现单文件创建、覆盖、精确编辑、patch、mkdir、move/copy/trash；版本控制、锁、审计、恢复。并发冲突、断电/重试、权限和路径攻击测试通过。

**通过条件：**
- 锁内版本/ACL/CWD 联合验证通过
- PREPARED 恢复测试通过
- 幂等、日志与备份保护、磁盘满和未知状态场景覆盖
- 审计与备份保护 G3 通过
- 核心端到端及安全验收 G4 通过

**涵盖任务：** 写入锁、原子提交、幂等、审计、写入工具、局部编辑、目录操作、移动复制、软删除、公网部署、核心联合验收。

### 7.4 P1：工程增强（增强及正式发布）

**阶段目标：** 实现变更查询、diff、恢复、Git 只读、二进制分块上传下载、运维安装。可恢复删除、客户端分页、部署升级回滚验证。

**通过条件：**
- 全部 P0/P1 工具联合回归通过
- 安装/升级/回滚/卸载验证
- 性能基准测试报告通过
- Linux amd64/arm64 实测
- 公开文档与隐私检查通过
- G5 正式发布门禁通过

**涵盖任务：** 工程概要、Git 只读状态、变更历史、文件差异对比、安全恢复、二进制分块、安装部署、性能基准、发布门禁。

### 7.5 P2：扩展（可选）

**阶段目标：** 可选扩展，实现代码符号导航和独立受控构建/测试执行服务。不阻塞 V1 发布。

**通过条件：**
- 各 P2 扩展独立评审、授权、兼容/隔离测试
- 禁用后 V1 正常运作
- G6 门禁通过

**涵盖任务：** 符号导航、独立受控执行。

---

> **文档说明：** 本文档从 Cat-总体需求与技术实现方案 v1.2 正向梳理，严格覆盖 R01–R16 原始需求范围，不做功能增减。所有需求标注为「来源:产品经理 | 状态:待实现」，表示从方案文档正向推导。实际实施优先级和阶段划分以 Cat-开发实施计划.md 为准，详细任务执行以 40 份标准任务档案为准。
