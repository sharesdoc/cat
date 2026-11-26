# C-前端设计文档

> 项目：Cat — 远程工程文件系统 MCP 服务
> 版本：v1.1（设计审查修订；尚未实现）
> 日期：2026-11-26

## 总述

Cat 的交互前端是 AI MCP 客户端（如 ChatGPT Web、OpenAI Responses API、Claude Code、Codex 等）。Cat 本身不提供 Web UI 或图形界面，而是以 MCP 工具（Tool）方式暴露文件系统能力，由 AI 客户端发现和调用。本文档描述 MCP 协议层的工具契约设计，是 Cat 的"前端"接口规范。

---

## 1. 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-11-26 | 初稿，基于总体方案 v1.2 §7 工具设计、§8 业务流程、§11 错误规范 |
| v1.1 | 2026-11-26 | 对齐 A/B、总体方案 v1.2、40 项任务及 D/E/F；补足契约、授权、幂等和分块边界 |

## 2. 交互架构

### 2.1 协议层

Cat 使用 MCP（Model Context Protocol）Streamable HTTP 协议与 AI 客户端通信，目标兼容 MCP 2025-era 及后续客户端，实际协议版本通过标准初始化协商，按 CAT-04/06 实测。官方 Python SDK v2.3.0 的 MCPServer 为候选，不将固定协议号或特定套餐能力视为已验证。协议交互链路：

```
AI MCP Client → HTTPS/TLS → 反向代理 → Cat MCP Gateway → 身份鉴权 → 业务处理
```

### 2.2 工具发现与初始化

1. 客户端通过 MCP 标准初始化握手与 Cat 建立连接，协商协议版本。
2. 客户端发起 `tools/list` 请求，Cat 返回全部可用工具清单（含名称、描述、输入 schema、readOnlyHint、destructiveHint 注解）。
3. 后续调用通过 `tools/call` 逐次发起，每次调用携带参数并接收结构化响应。

### 2.3 会话模式

Cat 引入业务级 workspace_id 作为上下文标识，与 MCP 协议层连接 ID 分离。工作空间状态持久化存储于 SQLite：

- **建立**：`cat_open_workspace` 选择工程项目，分配不可预测随机 workspace_id，绑定登录主体。
- **使用**：相对文件变更必须传 cwd_version，或显式 path_base=root；读调用可选 expected_version，续读必须携带首次版本。
- **过期**：空闲 2 小时（可配置），最长 24 小时（可配置），过期后需重新 open。
- **隔离**：不同 AI 客户端即使使用同一账号，也分别打开独立 workspace，避免 CWD 串扰。

### 2.4 鉴权模式

- OAuth 2.1 + PKCE，使用成熟身份提供方（IdP）。
- 按主体 → 工程 → 路径 → 动作四级鉴权。
- discover/read/write/delete/restore/audit/workspace 为独立动作，无隐含等级继承；有效权限 = 已验证 token scope ∩ 当前 Grant scope ∩ 管理员项目 access 上限，另检查路径前缀和 denylist。
- 仅本机隔离开发可启用受限 loopback 模式；不能经公网代理转发未认证入口。

## 3. MCP 工具契约清单

所有工具名称统一使用 `cat_` 前缀。此目录共 33 个已命名工具：19 个 P0、11 个 P1（合计 V1 30 个）、3 个可选 P2。CAT-40 受控构建/测试另行设计和授权，不在本目录增加执行接口。P1/P2 未实施、未启用或不具备安全条件的工具不得出现在可用 tools/list。每个工具标注：

- **权限级别**：discover / read / write / delete / restore / audit / workspace
- **readOnlyHint**：true 表示工具不修改服务端状态
- **destructiveHint**：true 表示工具可能破坏数据（覆盖、删除、恢复等）
- **阶段归属**：P0（核心）/ P1（工程增强）/ P2（扩展）

### 3.0 公共参数与校验规则（所有工具生效）

- 所有调用均从已验证 token/受限本机身份上下文取得主体，不接受客户端 user_id；除 capabilities/list_projects/open_workspace 外均要求 workspace_id。每次重验项目启用/generation、当前授权、期限及动作。get_operation 对过期/关闭的原 workspace 的只读查账例外见其条目，不恢复其文件权限。
- JSON Schema 禁止未知字段（additionalProperties:false），按类型、枚举、长度、范围和条件必填严格校验。各阶段真实限额由 capabilities 返回；本文的数字是设计默认值。
- 凡接收 path、source、destination 的文件工具统一支持可选 path_base="cwd"（默认）或"root"（set_cwd 除外）。省略 path 时默认对应 base 的目录；明确工程全局操作（例如 project_info、git_status）不受 CWD 影响。返回的路径始终为工程相对路径，以 root 再定位。根统一表示 "."，不接受主机绝对路径。
- 文件/目录变更及上传会话变更须有稳定 request_id；相对变更须有 cwd_version 或显式 path_base=root。source/destination 使用同一 base；set_cwd 的 path 专指相对当前 CWD，使用 expected_cwd_version。上传 chunk/finish/abort 通过 upload_id 继承 begin 已绑定的规范目标，不能随新 CWD 改变，不接收新的 path_base/cwd_version。dry_run 默认 false，预览可省略 request_id，不写目标或创建 mutation 回执，仍须授权、版本检查及审计。set_cwd 通过 expected_cwd_version CAS 更新；open/close 为工作空间生命周期，非文件事务，open 重试可能创建新会话，close 不重复产生文件变更。
- 文件版本为 sha256: 加 64 位小写十六进制，基于原始字节。恢复目标不存在时使用字面量 "absent"（不是文件哈希）。目录版本见 stat；目录不可直接当普通文件做哈希。所有输入版本在实际操作时复验，失败不得自动覆盖。
- 真实文件 mutation 的幂等键为 (subject, project_id, workspace_id, request_id)。输入摘要对工具名称和规范化结构参数（含 base、cwd_version、内容等）计算，默认值显式化；已受理请求记录首次解析的规范路径与工程 generation，不因后续 CWD 变化重新定位；每次重放前重验当前身份和授权，已提交只返回安全持久回执；不同参数拒绝。过期会话不得执行新文件变更。
- 只读工具的审计/租期维护属于内部记账；工具注解按对业务资源的实际影响描述。混合 create/replace 或 dry_run/commit 工具静态 destructiveHint=true、readOnlyHint=false；不得按参数伪装为只读。

### 3.1 工程与工作空间管理

---

#### cat_get_capabilities

- **描述**：查询 Cat 服务能力，包括协议版本、工具版本、限额配置等。
- **权限**：read（已验证身份的全局能力查询；不需要工程 Grant，不返回工程数据）
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**输入**：无

**输出**：以下为完整 envelope 的虚构示例；实际协商协议、运行版本、启用工具版本、正则/分块能力与限额以运行时返回为准。
```json
{
  "ok": true,
  "request_id": "r_example",
  "workspace_id": null,
  "data": {
    "protocol_version": "2025-03-26",
    "server_version": "0.1.0",
    "limits": {
      "max_directory_depth": 8,
      "max_page_entries": 200,
      "max_tool_response_bytes": 65536,
      "max_inline_write_bytes": 262144,
      "max_search_seconds": 10,
      "max_search_results": 100,
      "workspace_idle_seconds": 7200,
      "workspace_max_seconds": 86400
    }
  }
}
```

---

#### cat_list_projects

- **描述**：返回当前授权主体可访问的工程列表。支持分页和名称查询过滤。
- **权限**：discover
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| query | string | 否 | 工程名称过滤关键字 |
| cursor | string | 否 | 分页游标，首次不传 |
| limit | integer | 否 | 每页条数上限（默认 20，最大 200） |

**输出**：工程 ID、显示名、根相对名称、权限、摘要列表，带分页信息。不返回真实服务器路径。

---

#### cat_open_workspace

- **描述**：为当前主体在指定工程上打开一个新的工作空间，获取 workspace_id 和初始 cwd_version。
- **权限**：read（必须明确拥有 read，不由 write/delete 隐含授予）
- **readOnlyHint**：false
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| project_id | string | 是 | 工程标识 |
| cwd | string | 否 | 初始工作目录，默认工程根 |

**输出**：workspace_id、cwd、cwd_version、过期时间。

---

#### cat_get_workspace

- **描述**：查询当前工作空间状态，包括 CWD、版本、过期时间、绑定主体等上下文信息。
- **权限**：read（仅限当前 workspace 的绑定主体）
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |

**输出**：授权的 project_id、workspace_id、cwd、cwd_version、期限及状态；不返回令牌、主机路径或其他主体内部信息。

---

#### cat_set_cwd

- **描述**：切换当前工作空间的相对工作目录。支持 '.'、'..' 和子目录，但不得越出工程根。不切换系统进程工作目录。
- **权限**：workspace
- **readOnlyHint**：false
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 目标相对当前 CWD 的路径；切换工程根可逐级返回或重新 open_workspace |
| expected_cwd_version | integer | 是 | 预期当前 CWD 版本，用于并发控制 |

**输出**：新 cwd 和 cwd_version。

---

#### cat_close_workspace

- **描述**：关闭工作空间，释放服务端资源。
- **权限**：workspace
- **readOnlyHint**：false
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |

**输出**：关闭确认回执。

---

#### cat_project_info

- **描述**：获取工程概要信息，包括 manifest 文件（如 README、AGENTS、pyproject.toml 等）、语言估计、Git 概况（HEAD commit、dirty 状态）等。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |

**输出**：工程元数据、关键清单文件列表、语言估计、Git 概览。

---

### 3.2 目录浏览与搜索

---

#### cat_list_directory

- **描述**：按深度层级列出目录内容。支持分页、过滤、稳定排序。不返回文件正文。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 否 | 目录路径，默认 CWD |
| path_base | enum | 否 | "cwd"（默认）或 "root"（工程根） |
| depth | integer | 否 | 展开深度 0-8，默认 1 |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页条目上限（默认 50，最大 200） |
| include | string | 否 | glob 包含模式 |
| exclude | string | 否 | glob 排除模式 |

**输出**：目录条目数组（path、name、type、depth、size、mtime、child_count），分页信息（has_more、next_cursor、truncated_reason）。

默认忽略 node_modules、target、dist、build、.git 及 .gitignore 匹配项。安全 denylist 永远优先。

---

#### cat_stat

- **描述**：获取单个文件或目录的详细元数据。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件/目录路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root" |

**输出**：type、size（字节）、mtime（UTC ISO 8601）、version、encoding、newline、MIME（无法判断时 null）。普通文件 version 为原始字节 SHA-256；目录用于 move/trash 时 version 为 `dir-sha256:` 加基于受控完整子树清单的确定性摘要（相对路径、类型、身份、普通文件原始字节哈希，稳定排序，含目录自身身份）。空目录可计算；有拒绝项、不安全对象、无法获得一致视图或超限时 version=null，不透露隐藏条目，拒绝需要目录版本的变更。符号链接仅显示类型，禁止跟随，不返回外部目标。

---

#### cat_find_files

- **描述**：按文件名的 glob 模式搜索工程内的文件路径。适用于寻找模块、配置等目标。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 否 | 搜索起始路径，默认 CWD；path_base=root 时为工程根 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| glob | string | 是 | 文件名 glob 模式（如 "*.java"、"**/README*"） |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页结果上限 |

**输出**：匹配文件路径列表，每项含 name、path（规范工程相对路径）、type、size、mtime，并返回 has_more/next_cursor/truncated_reason。

---

#### cat_search_text

- **描述**：在工程文件内容中搜索文本或正则匹配。支持路径过滤、上下文行、大小写设置。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 否 | 搜索起始路径，默认 CWD；path_base=root 时为工程根 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| query | string | 是 | 搜索关键字 |
| mode | enum | 是 | "literal"（字面量）或 "regex"（正则） |
| case_sensitive | boolean | 否 | 默认 true |
| include | string | 否 | 包含文件模式 |
| exclude | string | 否 | 排除文件模式 |
| context_lines | integer | 否 | 上下文行数（每侧） |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页结果上限 |

**输出**：匹配项列表（文件路径、行号、短文本上下文、版本），分页信息。

搜索限时（默认 10 秒）、限结果数（默认 100）。正则使用经验证的受限引擎或 ripgrep 固定 argv，禁止 shell/PCRE2/外部预处理器。搜索进程不得读取禁止文件；若无法保障则回退安全 fd 的有界字面量扫描，正则不可用返回 CAPABILITY_UNAVAILABLE。禁止路径一律不访问，允许文件内命中秘密才拦截或脱敏并标记。

---

### 3.3 文件读取

---

#### cat_read_file

- **描述**：读取单个文件的全部或指定行/字节范围。支持分段续读、版本校验。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root" |
| start_line | integer | 否 | 起始行号（1-based，闭区间） |
| end_line | integer | 否 | 结束行号 |
| max_bytes | integer | 否 | 最大读取字节数（默认 32768，建议上限 65536） |
| expected_version | string | 否 | 首次可省略；续读必须传首次版本，不匹配返回 VERSION_CONFLICT |
| byte_offset | integer | 否 | 原始字节偏移（0-based），用于长行续读；与行范围互斥，必须位于 UTF-8 字符边界 |

**输出**：path、version（SHA-256）、encoding、newline（LF/CRLF/mixed/none）、start_line、end_line、total_lines（无法有界统计时 null）、content、has_more、next_start_line、next_byte_offset、truncated_reason。长行续读使用 next_byte_offset；不切断 UTF-8 字符或混合不同版本。返回的 content 保留原始换行，BOM 以编码元数据标识；若内容脱敏则明确标记，不可按脱敏正文构造精确修改。

行号 1-based、闭区间，正文不嵌入行号。长单行按 byte offset 续读。支持 UTF-8、BOM、LF/CRLF；未知编码返回 UNSUPPORTED_ENCODING（P1 可按二进制读）。小文件可整读。分段读取中途内容改变返回 VERSION_CONFLICT 并要求重新读取。

---

### 3.4 文件写入与编辑

---

#### cat_write_file

- **描述**：创建新文件或覆盖已有文件。创建要求目标不存在，覆盖要求提供旧版本。不支持无条件 force 覆盖。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true（静态注解；支持覆盖）
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| content | string | 是 | 文件正文内容（文本模式） |
| mode | enum | 是 | "create"（创建，目标不得存在）或 "replace"（覆盖，需旧版本） |
| expected_version | string | 否 | mode=replace 时必填，预期旧版本哈希 |
| request_id | string | 是 | 幂等请求 ID，重试同 ID 和同参数返回原回执 |
| cwd_version | integer | 否 | 当前 CWD 版本，CWD 改变时拒绝 |

**输出**：写入回执，含 path、new_version（SHA-256）、operation_id。空文件合法，父目录必须已存在；不隐式 mkdir，replace 保留原编码、换行和权限，未明确允许的元数据变化拒绝。

---

#### cat_edit_file

- **描述**：对单个文件执行精确的多处编辑（replace_exact、replace_lines、insert_before/after）。所有编辑在内存中预检全部匹配后才提交。支持 dry_run 预览。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| expected_version | string | 是 | 预期旧版本哈希 |
| edits | array | 是 | 编辑操作数组 |
| dry_run | boolean | 否 | true 时只返回 diff 预览，不修改文件 |
| request_id | string | 否 | dry_run 为 false 时必填，幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

edits 数组元素支持的 kind：

| kind | 说明 | 约束 |
|---|---|---|
| replace_exact | 精确文本替换 | 默认必须唯一匹配；expected_matches 指定预期匹配数 |
| replace_lines | 按行范围替换 | start_line/end_line 为原始版本 1-based 闭区间，必须提供 expected_old_text 与 new_text |
| insert_before | 在唯一锚点之前插入 | 锚点须唯一匹配 |
| insert_after | 在唯一锚点之后插入 | 锚点须唯一匹配 |

每个元素按 kind 使用严格 Schema：replace_exact={kind,old_text,new_text,expected_matches?}（old_text 非空，默认匹配数1；显式 N>1 须恰好 N 个非重叠匹配并全部替换）；replace_lines={kind,start_line,end_line,expected_old_text,new_text}；insert_before/after={kind,anchor,new_text}（anchor 非空且唯一）。所有位置以同一原版本解析，区间或插入点冲突拒绝，禁止逐次偏移造成误匹配；new_text 可为空以支持删除。

**输出**：[dry_run] 有界 diff、目标原版本、预览摘要；[提交] 新版本、操作回执。

所有编辑共享同一个 expected_version，重叠范围拒绝。dry_run 不修改工程，提交时复核同一版本。

---

#### cat_apply_patch

- **描述**：对单个文件应用严格单文件统一 diff 补丁。拒绝 rename、路径跳转、二进制、多文件补丁、模糊 hunk。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| patch | string | 是 | 统一 diff 格式补丁文本 |
| expected_version | string | 是 | 预期旧版本哈希 |
| dry_run | boolean | 否 | true 时只校验不修改 |
| request_id | string | 否 | dry_run 为 false 时必填 |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：同 edit_file，所有 hunk 匹配才提交，半失败则文件内容完全不变。

---

### 3.5 目录与文件操作

---

#### cat_create_directory

- **描述**：创建单个或层级目录。目标已存在时返回 ALREADY_EXISTS；parents=true 仅允许复用已存在的安全父目录。多级创建可能部分成功，必须记录已新增路径及恢复状态，不承诺跨目录原子性。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 目录路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| parents | boolean | 否 | 是否递归创建父目录（类似 mkdir -p） |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：创建回执（路径、operation_id、created_paths）；部分失败须通过回执/状态准确报告已创建项，不误删原目录。

---

#### cat_move_path

- **描述**：重命名或移动文件/目录。源与目的写授权、相关祖先及目录子树均需校验；不覆盖目标，禁止移动工程根或移入自身子树。跨工程/跨文件系统移动 V1 禁止。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| source | string | 是 | 源路径 |
| destination | string | 是 | 目标路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| expected_version | string | 是 | 源文件/目录预期版本（stat 获取） |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：移动回执（新路径、新版本、操作 ID）。

---

#### cat_copy_file

- **描述**：复制单个文件。源须 read、目的须 write，父目录存在且目标不存在；同工程内复制，不提供递归复制。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| source | string | 是 | 源文件路径 |
| destination | string | 是 | 目标文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| expected_version | string | 是 | 源文件预期版本 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：复制回执（目标路径、新版本、操作 ID）。

---

#### cat_trash_path

- **描述**：将文件或目录移至工程外专属回收区（软删除）。非空目录默认拒绝；recursive=true 需配置启用、真实独立审批、全子树授权、完整清单和限额。不可删除工程根；无法安全原子移动时拒绝。
- **权限**：delete
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 目标路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| expected_version | string | 是 | 目标预期版本 |
| recursive | boolean | 否 | 是否递归删除目录 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：删除回执（路径、operation_id、单文件可恢复的 revision_id；目录删除只提供管理员恢复材料，不承诺目录级 MCP 恢复）。

---

### 3.6 变更审计与恢复

---

#### cat_list_changes

- **描述**：查询当前工程授权范围内的变更操作记录。支持分页。
- **权限**：audit
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P1（CAT-31）

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页条数上限 |
| path | string | 否 | 过滤当前授权范围内的规范路径/子树；不传为授权工程范围 |
| path_base | enum | 否 | "cwd"（默认）或 "root" |

**输出**：操作记录列表（操作 ID、路径、动作类型、时间戳、先后版本、结果、可用 revision_id 及其可恢复标志），分页信息。不返回备份物理引用。可选 path 过滤，遵循 path_base。

---

#### cat_get_operation

- **描述**：查询单次操作的当前状态。网络中断恢复时优先调用此接口确认操作结果，避免盲目重放。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| request_id | string | 是 | 操作请求 ID |

**输出**：status=COMMITTED/FAILED/IN_PROGRESS/IN_DOUBT/UNKNOWN、operation_id（若存在）、原有安全回执或明确错误、retryable。IN_PROGRESS 对应 RESERVED/PREPARED。查询必须使用原请求 workspace_id；仅为查询可接受过期/关闭的原 workspace（同主体、同工程 generation、当前 read 及原操作路径/动作仍授权），不延长租期或允许新写入。未知与无权不可泄露其他主体记录；IN_DOUBT 不得盲重试。UNKNOWN 仅当原 scope 仍有效且服务确认未受理、未过保留窗口时允许相同 ID/参数重放；超过窗口或无法证明时先管理员核验。

---

#### cat_diff_file

- **描述**：获取单个文件的指定版本差异。支持对比历史修订版本或 Git 基线。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| revision_id | string | 否 | 历史修订版本标识，不传则对比当前与上一版本 |
| git_base | string | 否 | Git 对比基准（如 HEAD~1），不传则对比 Cat 历史 |

**输出**：有界差异（统一 diff 格式）、前后版本哈希。

---

#### cat_restore_file

- **描述**：将文件恢复到指定历史修订版本。检查当前版本或不存在状态，禁止覆盖后来发生的用户修改。
- **权限**：restore
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| revision_id | string | 是 | 目标修订版本标识 |
| expected_version | string | 是 | 当前版本哈希；恢复已删除文件时明确传 "absent"，目标必须仍不存在 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：恢复回执（路径、新版本哈希、operation_id）；若当前版本与 expected_version 不符，返回 VERSION_CONFLICT。revision_id 必须对应同一工程及该规范路径的受授权普通文件旧字节快照，校验备份哈希、有效期和根 generation；无法取得内容返回 HISTORY_UNAVAILABLE，不猜测历史内容。

---

### 3.7 二进制文件（P1）

二进制文件操作工具集。所有操作绑定单文件和授权 workspace，严格限制大小、块序号、偏移和哈希校验，仅在 finish 时原子提交。超大文本文件也可通过此分块方式创建。

---

#### cat_begin_upload

- **描述**：初始化二进制文件上传，返回 upload_id 和块大小建议。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| mode | enum | 是 | "create" 或 "replace" |
| expected_version | string | 否 | replace 时必填 |
| total_size | integer | 是 | 文件总字节数 |
| file_hash | string | 是 | 完整文件 SHA-256 预计算值 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 相对路径必填；显式 root 可省略 |

**输出**：upload_id、chunk_size_max、next_chunk_index=0、next_offset=0、expires_at。begin 固定目标规范路径、版本条件、主体、工程 generation 与 workspace；不写目标。

---

#### cat_upload_chunk

- **描述**：上传一个数据块。按序传输，服务端校验块序号和偏移连续性。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| upload_id | string | 是 | begin_upload 返回的上传 ID |
| chunk_index | integer | 是 | 块序号（0-based） |
| offset | integer | 是 | 块在文件中的偏移字节 |
| data | base64 | 是 | 块数据（Base64 编码） |
| chunk_hash | string | 是 | 块的 SHA-256 哈希 |
| request_id | string | 是 | 稳定幂等请求 ID |

**输出**：accepted_index、next_chunk_index、next_offset、expires_at。同序号/偏移/哈希的已接受块返回原确认；不同内容、乱序、偏移错误或超限拒绝。只写隔离暂存对象，不写目标。

---

#### cat_finish_upload

- **描述**：完成上传，按总文件哈希校验所有块，原子写入文件系统。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| upload_id | string | 是 | 上传 ID |
| request_id | string | 是 | 最终文件提交的稳定幂等 ID |

**输出**：operation_id、path、new_version。重验当前 ACL、固定路径/工程 generation、原始目标条件和上传状态，核验总字节、总哈希；仅通过统一提交器变更目标。finish 已提交重试返回原回执；无法确认时按 IN_DOUBT 处理。

---

#### cat_read_chunk

- **描述**：读取二进制文件的指定字节范围。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| offset | integer | 是 | 起始偏移字节 |
| size | integer | 是 | 读取字节数 |
| expected_version | string | 否 | 预期版本哈希 |

**输出**：data（Base64 编码）、offset、size、chunk_hash、total_size、version、has_more、next_offset。续读必须提供首次版本；size 与 Base64 膨胀及 envelope 一同受实际响应限额约束。

---

#### cat_abort_upload

- **描述**：取消进行中的上传，清理已接收的临时数据。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| upload_id | string | 是 | 待取消的上传 ID |
| request_id | string | 是 | 稳定幂等请求 ID |

**输出**：upload_id、status、已清理标志。不得中断正在提交的目标文件事务；已完成上传不执行逆向恢复；IN_DOUBT 保留暂存与日志。

---

### 3.8 Git 只读（P1）

Git 操作仅限只读查询，不执行 commit、push 或其他变更操作。

---

#### cat_git_status

- **描述**：查询当前工程的 Git 状态（dirty、untracked 文件、已修改文件等）。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 受实际响应限额约束 |

**输出**：is_git、当前分支、HEAD、授权范围内 dirty 状态、modified/untracked/staged 文件列表（受限分页，cursor/limit 可选）。非 Git 返回 is_git=false；Git 不可用返回明确能力错误。禁止外部 diff/pager/textconv、可选锁副作用和未授权 worktree 访问。

---

#### cat_git_diff

- **描述**：获取当前工作树与 Git 基线的差异。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P1

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 否 | 限定某个文件或目录的 diff |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| base | string | 否 | 对比基线（如 HEAD、HEAD~1），默认 HEAD |

**输出**：有界统一 diff 文本、基线、truncated_reason（必要时）；path 未传时为授权工程范围的有界 Git 差异，不批量返回完整文件正文。Git 基线作为参数验证，不解释成命令或选项。

---

### 3.9 符号级理解（P2，可选）

符号操作依赖可选语言服务（标准 LSP 或许可证核验后的 MIT SolidLSP），Serena GPL 应用仅参考行为；非 V1 强制依赖。返回授权路径与范围，不运行工程指令、不批量返回正文。

---

#### cat_list_symbols

- **描述**：列出文件或工程中的符号概要（类、函数、变量等）。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P2

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 否 | 限定路径，不传则扫描工程 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |
| kind | string | 否 | 符号类型过滤（class、function、variable 等） |

**输出**：符号路径、名称、类型、所在行范围。

---

#### cat_find_definition

- **描述**：查找符号的定义位置。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P2

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| symbol | string | 是 | 符号名称 |
| path | string | 否 | 搜索范围路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |

**输出**：定义文件路径和行范围。

---

#### cat_find_references

- **描述**：查找符号的所有引用位置。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P2

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| symbol | string | 是 | 符号名称 |
| path | string | 否 | 搜索范围路径 |
| path_base | enum | 否 | "cwd"（默认）或 "root"；双路径共同适用 |

**输出**：授权引用位置列表（文件路径、行号和范围），有界返回；正文另用 cat_read_file。

---

## 4. 读写交互流程

### 4.1 标准单文件修改流程

```
AI                          Cat MCP                     文件系统
 |                            |                            |
 |--- read_file(path) ------>|                            |
 |                            |--- 有界读取 authorized --->|
 |                            |<--- bytes -----------------|
 |<-- text + version V1 -----|                            |
 |                            |                            |
 |--- edit_file(dry_run,V1)->|                            |
 |                            |--- (预检全部编辑匹配) ------|
 |<-- diff preview ----------|                            |
 |                            |                            |
 |--- edit_file(request_id,  |                            |
 |              V1) -------->|                            |
 |                            |--- 锁 + 重验 V1 ----------|
 |                            |                            |
 |      ┌── V1 未变化 ──────┤                            |
 |      |                    |--- 备份 + 原子替换 --------->|
 |      |                    |<-- 写入确认 ----------------|
 |      |<-- operation_id +  |                            |
 |      |    version V2 -----|                            |
 |      |                    |                            |
 |      └── 外部已修改 ─────┤                            |
 |                       <--| VERSION_CONFLICT ----------|
 |                            |                            |
 |--- read_file(expected_    |                            |
 |        version=V2) ------>|                            |
 |<-- content or conflict ---|                            |
```

### 4.2 版本覆盖控制

- **读取时**：可选传 expected_version，中途内容改变返回 VERSION_CONFLICT 并要求重新读取。不可拼接不同版本的内容。
- **写入时**：create 模式要求目标不存在；replace 模式要求提供旧版本哈希。无无条件 force 覆盖。
- **编辑时**：所有编辑共享同一个 expected_version，提交时复核同一版本。dry_run 不修改工程。
- **CWD 版本**：相对写调用必须传 cwd_version 或显式 path_base=root；CWD 改变则旧相对写请求返回 CWD_CONFLICT。
- **幂等性**：所有 mutation 使用唯一 request_id。重试同 ID 和同参数返回原回执；不同参数返回 IDEMPOTENCY_CONFLICT。

### 4.3 并发写边界

文件锁约束 Cat 客户端之间的写冲突，不能保证外部本地编辑进程遵守锁。普通 POSIX rename 无通用 compare-and-swap。Cat 不负责自动 Git 合并。出现冲突时建议流程：重新读取 → 重新设计变更 → 重新提交，不自动覆盖本地编辑。

### 4.4 网络中断恢复

网络中断后不盲目重放请求：
1. 调用 `cat_get_operation(workspace_id, request_id)` 查询原作用域。COMMITTED 直接采用原回执；IN_PROGRESS 继续查询；FAILED 为已明确终结，不能以相同 ID 重新执行，若确需重新规划则重新读/授权后使用新 ID。
2. UNKNOWN 与 IN_DOUBT 不同：只有服务器明确未受理且在安全重放窗口内，才用相同作用域、ID 和参数重放；IN_DOUBT 或窗口外状态须管理员核验，禁止盲目重试。

## 5. 错误响应规范

### 5.1 统一结果格式

所有工具调用返回统一结构化响应：

成功示例（非文件调用 workspace_id 为 null，文件调用的 data 包含授权上下文及规范相对路径）：

```json
{"ok":true,"request_id":"r_example","workspace_id":"ws_example","data":{"project_id":"demo-app","cwd":".","cwd_version":1,"path":"src/example.py"}}
```

失败示例：

```json
{"ok":false,"request_id":"r_example","workspace_id":"ws_example","error":{"code":"VERSION_CONFLICT","message":"文件版本已变化","retryable":true,"details":{"requires_new_read":true}}}
```

data 与 error 按成功/失败互斥（未使用项省略）；未提供 request_id 的只读请求由服务生成关联 ID，不成为文件变更幂等键。retryable=true 指按明确恢复步骤可重新发起，并不允许盲重放已终结写请求。拒绝信息不泄露未知工程、其他主体或敏感路径。

- MCP 工具业务错误使用 `isError=true` 和结构化错误内容。
- 认证/协议层错误使用标准 HTTP 状态码和 MCP 协议错误语义。
- 所有响应避免泄露主机绝对路径和内部堆栈信息。

### 5.2 错误码表

| 错误码 | 含义 | 可重试 | 建议处理 |
|---|---|---|---|
| UNAUTHENTICATED | 未认证或令牌无效 | 否 | 重新认证 |
| FORBIDDEN | 无权限执行该操作 | 否 | 申请合法权限，不尝试替代路径 |
| WORKSPACE_EXPIRED | 工作空间已过期 | 是 | 重新调用 cat_open_workspace |
| CWD_CONFLICT | CWD 版本不匹配 | 是 | 调用 cat_get_workspace 确认当前 CWD，重设后重试 |
| PATH_OUTSIDE_ROOT | 路径越出工程根 | 否 | 确认路径在工程范围内 |
| UNSAFE_PATH | 路径被认为不安全（symlink、特殊文件等） | 否 | 使用安全路径重试，避免符号链接 |
| NOT_FOUND | 文件或目录不存在 | 否 | 确认路径或调整操作模式 |
| ALREADY_EXISTS | 目标已存在（如 create 模式） | 否 | 改用 replace 模式或更换目标路径 |
| VERSION_CONFLICT | 版本哈希不匹配或文件被外部修改 | 是 | 重新读取文件，重新生成编辑 |
| AMBIGUOUS_MATCH | 替换/插入锚点匹配不唯一 | 否 | 收窄匹配文本或使用行号定位 |
| PATCH_MISMATCH | unified diff hunk 不匹配 | 否 | 检查补丁上下文是否精确匹配当前文件 |
| CURSOR_EXPIRED | 分页游标已过期 | 是 | 从新的分页快照重新开始扫描 |
| TOO_LARGE | 请求超出大小限制 | 是 | 分块处理或分批读取 |
| RATE_LIMITED | 请求频率/并发/资源超限 | 是 | 按 retry_after 提示等待后重试 |
| APPROVAL_REQUIRED | 需要授权方审批 | 否 | 等待人工审批流程 |
| IDEMPOTENCY_CONFLICT | 同一 request_id 被用于不同请求参数 | 否 | 使用新的 request_id |
| IN_DOUBT | 操作状态不确定（进程崩溃等） | 否 | 查询操作状态并联系管理员，禁止盲目重试 |
| INVALID_ARGUMENT | 参数、模式或条件必填错误 | 否 | 修正请求 |
| CAPABILITY_UNAVAILABLE | 当前能力或安全执行条件不可用 | 否 | 查看 capabilities，不绕过安全层 |
| UNSUPPORTED_ENCODING | 非支持文本编码或二进制 | 否 | 使用经授权的 P1 分块读取 |
| HISTORY_UNAVAILABLE | 快照过期、缺失或校验失败 | 否 | 停止恢复并核验 |
| TIMEOUT | 有界执行超时 | 视操作状态 | 读可按限制重试，写先查操作 |
| STORAGE_UNAVAILABLE | 持久存储/配额/审计失败 | 视操作状态 | 不可确认时进入 IN_DOUBT |
| INTERNAL_ERROR | 无法安全分类的内部失败 | 否 | 仅返回脱敏关联 ID，写先查操作 |

## 6. 目录分页与搜索契约

### 6.1 分页参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| depth | integer | 1 | 目录展开深度。0=仅目录自身元数据，1=直接子项，最大 8 |
| cursor | string | 无（首次不传） | 分页游标，与主体、工程、参数和快照绑定 |
| limit | integer | 50（列表）/ 20（工程列表） | 每页条目上限，最大 200 |
| has_more | boolean | — | 响应字段，指示是否还有更多数据 |
| next_cursor | string | — | 响应字段，下一页游标值，null 表示无更多数据 |
| truncated_reason | string | — | 响应字段，截断原因说明 |

### 6.2 目录条目

每个条目包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| path | string | 规范相对路径 |
| name | string | 条目名称 |
| type | enum | "file" / "directory" / "symlink" |
| depth | integer | 在本次请求中的深度层级 |
| size | integer | 文件字节数（目录为 0） |
| mtime | string | 最后修改时间（ISO 8601） |
| child_count | integer/null | 子项数量（目录；未知时为 null） |

按规范化路径稳定排序。使用有界快照/游标确保分页一致性。

### 6.3 默认排除与安全规则

- 默认忽略 node_modules、target、dist、build、.git 及 .gitignore 匹配项。
- 允许显式访问非敏感被忽略源文件，但安全 denylist 永远优先。
- 安全敏感路径（.env、私钥、凭证、生产配置、Git 内部配置等）在任何工具中均拒绝访问；允许文件中的秘密内容另行阻断或脱敏并明确标记。

### 6.4 搜索规则

| 特性 | 说明 |
|---|---|
| mode | "literal"（字面量）或 "regex"（正则） |
| 路径过滤 | include/exclude glob 模式 |
| 上下文行 | context_lines 参数控制匹配行前后的附加行数 |
| 大小写 | 默认大小写敏感，通过 case_sensitive 控制 |
| 超时 | 默认 10 秒超时 |
| 结果上限 | 默认 100 条 |
| 安全 | 正则使用 RE2 或 ripgrep 固定 argv，不拼接 shell。禁止 PCRE2 等高风险模式。不跟随符号链接。搜索命中的秘密内容额外阻断或脱敏 |

## 7. 客户端接入指南

### 7.1 接入前提

Cat 通过标准 MCP Streamable HTTP 协议暴露能力。使用前需要：

1. 一个可公网访问的 Cat 服务端点（HTTPS）。
2. 有效的 OAuth 授权凭据。
3. 至少一个已注册的工程。
4. AI 客户端支持自定义 MCP 服务器。

### 7.2 接入路线

**A 路线（ChatGPT Web 原生）**：

适用于经 CAT-06 实测具备自定义 MCP 写工具权限的账户及工作空间。总体方案列出的套餐限制是调研基线，不是实际账户能力保证；以目标账户当时的官方规则及真实授权、读写回执为准。

步骤：
1. 搭建 Cat 服务并配置公网 DNS（HTTPS）、反向代理、TLS 证书。
2. 配置 OAuth 身份提供方（IdP），设置 callback/redirect URI。
3. 在 ChatGPT Web 的 Plugins / 自定义 MCP 设置中输入端点 URL（`https://cat.example.com/mcp`）。
4. 按 OAuth 流程授权。
5. 验证工具列表和调用：cat_list_projects → cat_open_workspace → cat_read_file → cat_write_file → cat_edit_file。
6. 按客户端要求确认写操作，不将写工具伪装成只读来规避。

**B 路线（API / 其他客户端）**：

适用于不支持自定义写入的 ChatGPT 账户，或使用其他 AI 客户端（Codex、Claude Code 等）。

- 通过 OpenAI Responses API 或其他支持完整 MCP 的客户端，配置授权令牌连接同一服务。
- 工具执行审批由客户端显式控制。
- 此路线不意味着当前网页对话能直接调用该 MCP；如需当前网页原生使用，账户必须具备对应权限。

### 7.3 端点配置说明

| 配置项 | 说明 |
|---|---|
| 端点 URL | `https://<host>/mcp`（示例，实际地址以部署为准） |
| 协议 | MCP Streamable HTTP |
| 认证 | OAuth 2.1 + PKCE，使用成熟 IdP |
| 传输 | HTTPS/TLS，反向代理到 loopback（127.0.0.1:8765） |
| 初始化 | 标准 MCP 初始化握手及版本协商 |
| 限流 | 每个主体并发上限（默认 4），超限返回 RATE_LIMITED |

### 7.4 OAuth 配置要求

- 使用标准 MCP OAuth 发现流程。
- 支持 protected-resource metadata、PKCE、精确 redirect URI。
- 短期 token + 撤销机制。
- 优先使用成熟 IdP（身份提供方），不自研登录协议。
- 不将 token 写入 URL、源码或审计日志。

### 7.5 验证检查清单

完成部署后按以下顺序验证：

1. 工具发现：`tools/list` 只返回实际启用且可用的工具清单，逐项核对 schema 和静态注解。
2. 工程发现：`cat_list_projects` 返回授权的工程。
3. 工作空间：`cat_open_workspace` 成功后，`cat_get_workspace` 返回正确上下文。
4. 文件读取：`cat_read_file` 返回文件内容和哈希。
5. 创建文件：`cat_write_file(mode=create)` 创建新文件。
6. 修改文件：`cat_edit_file(dry_run=true)` 预览，再以 `request_id` 提交。
7. 版本冲突：修改后不传新版本，验证返回 VERSION_CONFLICT。
8. 搜索：`cat_search_text` 返回匹配结果。
9. 删除：`cat_trash_path` 将文件移入回收区。
10. 用隔离合成工程验证错误令牌、跨主体 ID、CWD/root 定位、版本冲突、断线查询、审批拒绝（不产生变更）、回读哈希；P1 增加历史恢复、分块和 Git 只读测试。CAT-06 仅最小测试工具；完整工具由 CAT-36/G4、CAT-38/G5 验证，本文不是实测报告。
