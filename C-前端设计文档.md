# C-前端设计文档

> 项目：Cat — 远程工程文件系统 MCP 服务
> 版本：v1.0
> 日期：2026-11-26

## 总述

Cat 的交互前端是 AI MCP 客户端（如 ChatGPT Web、OpenAI Responses API、Claude Code、Codex 等）。Cat 本身不提供 Web UI 或图形界面，而是以 MCP 工具（Tool）方式暴露文件系统能力，由 AI 客户端发现和调用。本文档描述 MCP 协议层的工具契约设计，是 Cat 的"前端"接口规范。

---

## 1. 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-11-26 | 初稿，基于总体方案 v1.2 §7 工具设计、§8 业务流程、§11 错误规范 |

## 2. 交互架构

### 2.1 协议层

Cat 使用 MCP（Model Context Protocol）Streamable HTTP 协议与 AI 客户端通信，兼容 MCP 2025-era 客户端。协议交互链路：

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
- **使用**：后续写调用须传 cwd_version 实现并发控制；读调用可选传 expected_version 进行版本校验。
- **过期**：空闲 2 小时（可配置），最长 24 小时（可配置），过期后需重新 open。
- **隔离**：不同 AI 客户端即使使用同一账号，也分别打开独立 workspace，避免 CWD 串扰。

### 2.4 鉴权模式

- OAuth 2.1 + PKCE，使用成熟身份提供方（IdP）。
- 按主体 → 工程 → 路径 → 动作四级鉴权。
- 读/写/删除/审计等权限分离。
- 不支持时本地开发可用受限 loopback 模式。

## 3. MCP 工具契约清单

所有工具名称统一使用 `cat_` 前缀。工具接收结构化 JSON 参数，返回统一结果格式。每个工具标注：

- **权限级别**：discover / read / write / delete / restore / audit / workspace
- **readOnlyHint**：true 表示工具不修改服务端状态
- **destructiveHint**：true 表示工具可能破坏数据（覆盖、删除、恢复等）
- **阶段归属**：P0（核心）/ P1（工程增强）/ P2（扩展）

### 3.1 工程与工作空间管理

---

#### cat_get_capabilities

- **描述**：查询 Cat 服务能力，包括协议版本、工具版本、限额配置等。
- **权限**：read
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**输入**：无

**输出**：
```json
{
  "ok": true,
  "data": {
    "protocol_version": "2025-03-26",
    "server_version": "1.0.0",
    "limits": {
      "max_directory_depth": 8,
      "max_page_entries": 200,
      "max_tool_response_bytes": 65536,
      "max_inline_write_bytes": 262144,
      "max_search_seconds": 10,
      "max_search_results": 100,
      "workspace_idle_seconds": 7200
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
- **权限**：read（需有工程的 read 及以上权限）
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

**输出**：当前上下文全部信息（CWD、版本、过期时间等）。

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
| path | string | 是 | 目标相对路径 |
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

**输出**：type、size（字节）、mtime（ISO 8601）、version（SHA-256 哈希）、encoding（如 utf-8）、换行风格、MIME 类型。

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
| path | string | 否 | 搜索起始路径，默认工程根 |
| glob | string | 是 | 文件名 glob 模式（如 "*.java"、"**/README*"） |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页结果上限 |

**输出**：匹配文件路径列表，每项含 name、relative_path、type、size、mtime。

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
| path | string | 否 | 搜索起始路径，默认工程根 |
| query | string | 是 | 搜索关键字 |
| mode | enum | 是 | "literal"（字面量）或 "regex"（正则） |
| include | string | 否 | 包含文件模式 |
| exclude | string | 否 | 排除文件模式 |
| context_lines | integer | 否 | 上下文行数（每侧） |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页结果上限 |

**输出**：匹配项列表（文件路径、行号、短文本上下文、版本），分页信息。

搜索限时（默认 10 秒）、限结果数（默认 100）。正则使用 RE2 或 ripgrep 固定 argv，不拼接 shell。不跟随符号链接。敏感文件搜索被阻断或脱敏。

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
| expected_version | string | 否 | 预期文件版本哈希，不匹配返回 VERSION_CONFLICT |

**输出**：path、version（SHA-256）、encoding、newline（LF/CRLF）、start_line、end_line、total_lines、content、has_more、next_start_line。

行号 1-based、闭区间，正文不嵌入行号。长单行按 byte offset 续读。支持 UTF-8、BOM、LF/CRLF；未知编码返回明确错误。小文件可整读。分段读取中途内容改变返回 VERSION_CONFLICT 并要求重新读取。

---

### 3.4 文件写入与编辑

---

#### cat_write_file

- **描述**：创建新文件或覆盖已有文件。创建要求目标不存在，覆盖要求提供旧版本。不支持无条件 force 覆盖。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：true（mode=replace 时）
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 文件路径 |
| content | string | 是 | 文件正文内容（文本模式） |
| mode | enum | 是 | "create"（创建，目标不得存在）或 "replace"（覆盖，需旧版本） |
| expected_version | string | 否 | mode=replace 时必填，预期旧版本哈希 |
| request_id | string | 是 | 幂等请求 ID，重试同 ID 和同参数返回原回执 |
| cwd_version | integer | 否 | 当前 CWD 版本，CWD 改变时拒绝 |

**输出**：写入回执，含 path、new_version（SHA-256）、operation_id。空文件合法。

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
| expected_version | string | 是 | 预期旧版本哈希 |
| edits | array | 是 | 编辑操作数组 |
| dry_run | boolean | 否 | true 时只返回 diff 预览，不修改文件 |
| request_id | string | 否 | dry_run 为 false 时必填，幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

edits 数组元素支持的 kind：

| kind | 说明 | 约束 |
|---|---|---|
| replace_exact | 精确文本替换 | 默认必须唯一匹配；expected_matches 指定预期匹配数 |
| replace_lines | 按行范围替换 | 需旧版本行范围确认 |
| insert_before | 在唯一锚点之前插入 | 锚点须唯一匹配 |
| insert_after | 在唯一锚点之后插入 | 锚点须唯一匹配 |

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

- **描述**：创建单个或层级目录。目标已存在时返回 ALREADY_EXISTS 错误。
- **权限**：write
- **readOnlyHint**：false
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 目录路径 |
| parents | boolean | 否 | 是否递归创建父目录（类似 mkdir -p） |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：创建回执（路径、操作 ID）。

---

#### cat_move_path

- **描述**：重命名或移动文件/目录。默认不覆盖已存在的目标。跨工程/跨文件系统移动 V1 禁止。
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
| expected_version | string | 是 | 源文件预期版本 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：移动回执（新路径、新版本、操作 ID）。

---

#### cat_copy_file

- **描述**：复制单个文件。默认不覆盖已存在的目标。
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
| expected_version | string | 是 | 源文件预期版本 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：复制回执（目标路径、新版本、操作 ID）。

---

#### cat_trash_path

- **描述**：将文件或目录移至工程专属回收区（软删除）。递归目录删除默认禁用或需服务端独立审批。不可删除工程根。
- **权限**：delete
- **readOnlyHint**：false
- **destructiveHint**：true
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| path | string | 是 | 目标路径 |
| expected_version | string | 是 | 目标预期版本 |
| recursive | boolean | 否 | 是否递归删除目录 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：删除回执（路径、操作 ID）。

---

### 3.6 变更审计与恢复

---

#### cat_list_changes

- **描述**：查询当前工程授权范围内的变更操作记录。支持分页。
- **权限**：audit
- **readOnlyHint**：true
- **destructiveHint**：false
- **阶段**：P0

**核心输入**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| workspace_id | string | 是 | 工作空间标识 |
| cursor | string | 否 | 分页游标 |
| limit | integer | 否 | 每页条数上限 |

**输出**：操作记录列表（操作 ID、路径、动作类型、时间戳、先后版本、结果），分页信息。

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

**输出**：操作状态（已执行/失败/进行中/未知）、操作详情。

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
| revision_id | string | 是 | 目标修订版本标识 |
| expected_version | string | 是 | 当前版本哈希，用于冲突检测 |
| request_id | string | 是 | 幂等请求 ID |
| cwd_version | integer | 否 | 当前 CWD 版本 |

**输出**：恢复回执（路径、新版本哈希、操作 ID）；若当前版本与 expected_version 不符，返回 VERSION_CONFLICT。

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
| mode | enum | 是 | "create" 或 "replace" |
| expected_version | string | 否 | replace 时必填 |
| total_size | integer | 是 | 文件总字节数 |
| file_hash | string | 是 | 完整文件 SHA-256 预计算值 |
| request_id | string | 是 | 幂等请求 ID |

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
| offset | integer | 是 | 起始偏移字节 |
| size | integer | 是 | 读取字节数 |
| expected_version | string | 否 | 预期版本哈希 |

**输出**：data（Base64 编码）、offset、size、chunk_hash、total_size、version。

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

**输出**：当前分支、HEAD commit SHA、dirty 状态、modified/untracked/staged 文件列表。

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
| base | string | 否 | 对比基线（如 HEAD、HEAD~1），默认 HEAD |

**输出**：统一 diff 文本。

---

### 3.9 符号级理解（P2，可选）

符号操作依赖语言服务（LSP/Serena），非 V1 强制依赖。

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

**输出**：引用位置列表（文件路径、行号、上下文文本）。

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
- **CWD 版本**：写调用必须传 cwd_version，CWD 改变则旧相对写请求返回 CWD_CONFLICT。
- **幂等性**：所有 mutation 使用唯一 request_id。重试同 ID 和同参数返回原回执；不同参数返回 IDEMPOTENCY_CONFLICT。

### 4.3 并发写边界

文件锁约束 Cat 客户端之间的写冲突，不能保证外部本地编辑进程遵守锁。普通 POSIX rename 无通用 compare-and-swap。Cat 不负责自动 Git 合并。出现冲突时建议流程：重新读取 → 重新设计变更 → 重新提交，不自动覆盖本地编辑。

### 4.4 网络中断恢复

网络中断后不盲目重放请求：
1. 调用 `cat_get_operation(request_id)` 查询前一次请求的状态。
2. 根据返回状态决策：已执行则不再重试；失败则重试；进行中则等待；未知（IN_DOUBT）则通知管理员介入。

## 5. 错误响应规范

### 5.1 统一结果格式

所有工具调用返回统一结构化响应：

```json
{
  "ok": true/false,
  "request_id": "r_xxx",
  "workspace_id": "ws_xxx",
  "data": { },
  "error": {
    "code": "ERROR_CODE",
    "message": "人类可读的错误描述",
    "retryable": true/false,
    "details": {}
  }
}
```

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
- 安全敏感文件（.env、私钥、凭证、生产配置、Git 内部配置等）在任何工具中均被阻断或脱敏。

### 6.4 搜索规则

| 特性 | 说明 |
|---|---|
| mode | "literal"（字面量）或 "regex"（正则） |
| 路径过滤 | include/exclude glob 模式 |
| 上下文行 | context_lines 参数控制匹配行前后的附加行数 |
| 大小写 | 默认大小写敏感，可通过参数控制 |
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

适用于具备自定义 MCP 写工具权限的 ChatGPT 套餐（Business、Enterprise/Edu；Pro 仅只读；Plus 不应假定有完整读写）。

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

1. 工具发现：`tools/list` 返回完整工具清单。
2. 工程发现：`cat_list_projects` 返回授权的工程。
3. 工作空间：`cat_open_workspace` 成功后，`cat_get_workspace` 返回正确上下文。
4. 文件读取：`cat_read_file` 返回文件内容和哈希。
5. 创建文件：`cat_write_file(mode=create)` 创建新文件。
6. 修改文件：`cat_edit_file(dry_run=true)` 预览，再以 `request_id` 提交。
7. 版本冲突：修改后不传新版本，验证返回 VERSION_CONFLICT。
8. 搜索：`cat_search_text` 返回匹配结果。
9. 删除：`cat_trash_path` 将文件移入回收区。
