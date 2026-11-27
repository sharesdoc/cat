# Cat 开源源码参考基线

版本：v1.2；与《Cat-总体需求与技术实现方案》§3.4 和 doc/ 下 40 份标准需求档案配套。

## 1. 固定源码版本

| 项目 | 版本锚点（完整 commit） | 版本性质 | 复用边界 |
|---|---|---|---|
| MCP 官方 Python SDK | [2118f14f8a19bc158d8a1cf90af58d85d187f849](https://github.com/modelcontextprotocol/python-sdk/tree/2118f14f8a19bc158d8a1cf90af58d85d187f849) | v2.3.0 候选稳定版 | 直接依赖；MCPServer、TokenVerifier、StreamableHTTP、传输安全、SDK 测试 |
| MCP 官方参考文件系统 | [c799c8552b0f27b7a33a7b9233773da144dddcf5](https://github.com/modelcontextprotocol/servers/tree/c799c8552b0f27b7a33a7b9233773da144dddcf5) | main 固定提交 | 工具行为、根限制、目录预算、pinned directory、路径测试；不使用全局 chdir |
| Filesystem MCP | [df56b1932872f47bf56eb4329be3d781ab9050f4](https://github.com/j0hanz/filesystem-mcp/tree/df56b1932872f47bf56eb4329be3d781ab9050f4) | main 固定提交 | 精确编辑、patch、scan/pagination、rewrite、undo、concurrency、敏感过滤 |
| local-mcp | [2a895a74981d82be327eb38e78dda1fc4eee3dc6](https://github.com/tkgstrator/local-mcp/tree/2a895a74981d82be327eb38e78dda1fc4eee3dc6) | master 固定提交 | OAuth、SQLite 会话、分段读取、文件交互；不移植自建共享口令或任意 shell |
| octofs | [d004e37902fb980d19a53261416dc9a822ac0e8a](https://github.com/Muvon/octofs/tree/d004e37902fb980d19a53261416dc9a822ac0e8a) | master 固定提交 | workdir、文件锁、行锚点、history、结构和符号参考 |
| Python Filesystem MCP | [a4b5b34f07a5386138825d7843bf4a2c578c8d5c](https://github.com/njbrake/filesystem-mcp-server/tree/a4b5b34f07a5386138825d7843bf4a2c578c8d5c) | main 固定提交 | Python 包、Docker、工具注册和基础 CRUD；非完整安全层 |
| Remote Desktop Commander | [71d6444c59c36f2cba2f13e8f72785383a8528b5](https://github.com/desktop-commander/remote-desktop-commander/tree/71d6444c59c36f2cba2f13e8f72785383a8528b5) | 固定提交 | 仅 README 交互体验；远程托管源码非开源，不作为代码复用来源 |
| Supergateway | [ffdf9b1d63a8e6086ed919da7039207b3f0ed3f5](https://github.com/supercorp-ai/supergateway/tree/ffdf9b1d63a8e6086ed919da7039207b3f0ed3f5) | 固定提交 | Streamable HTTP 网关、会话行为，仅参考/联调 |
| Serena | [23c06ff715409b2e9dc9be7ca26b7b2fbf356b7c](https://github.com/oraios/serena/tree/23c06ff715409b2e9dc9be7ca26b7b2fbf356b7c) | 固定提交 | GPL 应用符号工具只参考接口；MIT SolidLSP 单独评估 |
| ripgrep | [3e85c6c148fdce38a99ceeb9d2780dc7b03b4a4f](https://github.com/BurntSushi/ripgrep/tree/3e85c6c148fdce38a99ceeb9d2780dc7b03b4a4f) | 固定提交 | 直接调用 CLI；搜索、JSON 输出、忽略及限额测试 |
| CPython | [40533a46d2dcef742cd56d6e4098da23c813a518](https://github.com/python/cpython/tree/40533a46d2dcef742cd56d6e4098da23c813a518) | 3.12 分支固定提交 | 直接使用标准库；os/tempfile/hashlib/difflib/sqlite3/subprocess 等接口与测试 |

## 2. 重点核查发现

- 官方 SDK v2 主分支的 src/mcp/server/fastmcp.py 明确是移除说明；v2.3.0 固定提交已核实 MCPServer、tool、streamable_http_app、TokenVerifier 和 TransportSecurityMiddleware 的源码存在。实际依赖锁定和联调由任务04、05、06完成。
- j0hanz 的 rewriteFile/lockedWrite、applyEdits/findEditMatches、patchContent、UndoJournal、paginate、readByMode 等为核心行为参考。可移植算法，但 Cat 不沿用模糊匹配、多文件批量、未受权写入和不持久撤销语义。
- local-mcp 的 Store::open、PersistentSessions、Root::resolve、read_file/edit_file、OAuth 发现流程可参考。其会话存储不是 Cat 业务工作空间，路径检查及 OAuth 实现不能直接替代 Cat 安全设计。
- 官方 filesystem 的 buildTree、createEntryBudget、validatePath、pinned-directory 和相关测试值得借鉴；原地覆盖及进程级目录操作不可直接当作 Cat 的原子安全后端。
- octofs 的 resolve_path、lock_key_for/acquire_file_lock、行锚点及 HistoryStore 提供编辑和工作目录参考；任意 shell/远程路径不进入 V1。
- Serena 当前 LICENSE 明确 SolidLSP 为 MIT，Serena application 为 GPL-3.0-or-later。仅评估兼容的独立组件，不能把应用源码直接并入 MIT Cat。
- Remote Desktop Commander README 明确远程托管服务实现不是开源；只参考公开交互。

## 3. 实施和验收要求

40 份档案的第4节是具体源码映射和改造计划，第6节是对应验收。实现者需将实际阅读的函数、采用方式、许可与差异写入任务实现备注和 THIRD_PARTY_NOTICES（按实际使用），不得仅记录项目名。统一公共安全及事务服务必须先实现，再接各文件工具。用户数据、内部系统信息、真实 endpoint 和凭据不得进入公开记录。

## 4. 核查边界

本次固定参考提交，核对相关目录树，并读取关键实现的符号及部分代码段、SDK 稳定版核心接口及 Serena 许可证、Remote Desktop Commander README。不是对所有参考文件的逐行审计，也没有运行上游或 Cat；未读取的测试/实现正文须由对应任务开发者在实施前补读，许可证逐文件核验后方可复制或移植。引用版本是研究基线，不自动等同实际运行时依赖锁定。若上游变化、不可获取或不满足安全语义，记录证据和替代方案，不虚称已复用。
