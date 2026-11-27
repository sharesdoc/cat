# Cat C–F 设计文档复审记录

日期：2026-11-26  
审查基准：总体方案 v1.2、A/B、标准任务 CAT-01–40、Cat-开源源码参考基线及 Cat-开发实施计划；原始文档 commit 64aefae238f21d5a09f0def135749a09c241822d。  
范围：C、D、E、F 四份设计文档。按 dev 规划前置一致性纪律审查，依据明确设计修订请求单独修订设计；不执行 dev 生产代码迭代，不变更任务状态。

## 1. 主要问题及修订

| 编号 | 原始证据（原始版本章节） | 影响 | 处理 |
|---|---|---|---|
| 01 | E §3.2 scopes 写为 OAuth 与 ACL 并集、权限等级继承；F §7.1 用 LIKE 子串判断 | 可能越权 | C/D/E/F 统一 token∩Grant∩项目 access，独立动作、精确成员及路径组件判断；denylist 优先 |
| 02 | E §2、F §2/4 禁用物理外键；CAT-07 要求外键；F workspace/operation/revision ID 类型、基数和 E 不一致 | 孤儿记录及错关联 | SQLite foreign_keys=ON、复合 FK/PK、随机 TEXT ID、禁止级联历史删除 |
| 03 | F §3.4 全局 request_id UNIQUE，input_digest 可空，§7.3 仅 request_id 查询 | 跨主体碰撞/查账泄露、重复写 | 四元组作用域唯一、摘要非空、原路径绑定、原回执持久化；C/D 明确当前授权与断线处理 |
| 04 | C §3 缺通用 root 定位、长行 byte_offset、case_sensitive；混合注解与阶段不一致 | 工具不可直接实现或误审批 | C 统一公共条件、字段和精确编辑 schema；19 P0+11 P1+3 P2，cat_list_changes 为 P1 |
| 05 | C §3.6 restore 只能传哈希；D §5.4 不存在即恢复 | 可能覆盖后续变化或无法恢复删除文件 | 显式 absent 条件、同路径/工程/历史版本授权、备份哈希与过期检查 |
| 06 | D §5.1 write_file 隐式 mkdir，D §5.3 删除规则文字错误，§5.2 无版本和跨挂载检查 | 与 CAT-20/23–26 冲突 | 独立创建目录、no-replace、源目的校验、受限递归删除、隔离回收与目录版本 |
| 07 | D §6、E §3.4、F §5 状态命名混用；F 在 rename 之后才写 after_hash | 崩溃后缺少判定证据 | RESERVED/PREPARED/COMMITTED/FAILED/IN_DOUBT；PREPARED 持久化完整预计状态，按操作类型恢复 |
| 08 | D §6.1 先 COMMITTED 后审计；F §2 synchronous=NORMAL；F §7.3 仅更新状态无最终回执 | 断电/审计失败后错误成功或重复写 | FULL；终态、回执、修订、最终审计同一 DB 短事务，文件与 SQLite 非单一 ACID 事务；写后不明冻结 |
| 09 | D §4 断线将未知当可直接重试；E/F 未设计重放窗口、回执、未决清理 | 误重放及证据丢失 | 区分 UNKNOWN/IN_PROGRESS/IN_DOUBT/FAILED/COMMITTED，窗口外拒绝盲重试，未决对象禁止清理 |
| 10 | E/F cwd 默认 '/' 与 '' 不一致、租期仅 expires_at；D 过度简化授权 | 跨会话或无限续期 | 统一 "."，增加 absolute_expires_at、CAS、每次校验 generation 和 Grant；原 workspace 查账不恢复写入 |
| 11 | E/F Revision after_hash NOT NULL、单操作最多一快照；Audit 关联基数/结果枚举不一 | 删除、移动和历史记录表达错误 | 可空哈希、多路径记录、可用性、审计阶段及唯一 final 事件；元数据与对象过期分离 |
| 12 | F §3.7 可选数据库热配置没有原始需求；C 分块无完整回执、finish 无 request_id，E/F 无上传状态 | 引入额外功能且 R14 无持久设计 | 移除 config 表，保留 TOML；六张 P0 核心表+P1 upload_sessions；分块绑定、租期、清单、幂等与原子 finish |
| 13 | C/E/B 部分 JSON 示例不可解析；D §7 Mermaid 含破损边和 Note 语法；套餐能力陈述过度确定 | 契约示例/图无法可靠使用 | C/E 统一响应与错误码，重绘 D 授权/事务流程，接入能力以 CAT-06 实际回执验证 |
| 14 | D 备份默认不限；F 清理直接 DELETE revisions | 资源耗尽、失去审计或未决恢复依据 | 有限配额、持久引用、未决保留、过期对象与历史元数据分开 |

## 2. 与计划及开源参考的对应

C 对齐 CAT-04/05/06/12–16/19–26/29–33/39；D 对齐 CAT-09/10/17–19/20–27/33/34/36；E/F 对齐 CAT-03/07/08/09/12/17–19/27/31–33。既定 40 项任务、源码固定版本、阶段与计划门禁未改变。文档均为设计，不宣称源码已移植或实现。后续开发仍须按任务第4/6节指定源码、许可和测试执行。

## 3. 设计细化决策与残留边界

- 本次统一新增内部 RESERVED 状态、操作清单及复合键；明确 SQLite FULL、固定 UTC 毫秒时间、随机 TEXT ID、明确全工程前缀、P1 上传会话及目录版本摘要。这些是既定需求的实现细化，不新增对外工具；需由对应 CAT 任务进一步冻结并测试。
- A/B 为此前提炼文档，本次按用户指定范围未直接修改。其简表存在需同步澄清的表述：A R11 写所有调用均需 workspace_id（与 A §6 的三个例外冲突）；A R14 的“最后一块完成时原子提交”应明确仅 finish 提交；A R09 copy_file 参数简表误把 destination 与预期版本混写；A 的权限动作简表省略 workspace；B 示例 access 仅 read/write，实际使用需明确 discover/workspace；B §8 的伪 JSON 仅示意而非可直接验证样本。C–F 已按总体方案与任务档案明确这些细节，建议下一次同步 A/B 表述，不凭简表实现冲突行为。
- 安全路径和外部编辑器竞态、真实账号与 IdP、目录清单与保留窗口限额、迁移和故障注入仍由对应任务完成；文档审查不替代独立审查或运行测试。
- dev 的完整主/子代理编排、代码实现、M/X、独立 tst 迭代不在本轮设计修订范围，当前未提供独立子代理执行回执。全部标准任务仍为原状态。

## 4. 静态核查

已核对 C 的33个具名工具与原总体方案，P0/P1/P2 分别19/11/3；F 七张业务表中6张 P0、1张 P1；D/E/F 使用同一 Operation 状态及恢复口径。提交后从 GitHub 回读：33 个工具名称与总体方案 §7 完全一致；三个 JSON 示例全部解析成功；C 错误码均在 E 覆盖；四份文档代码围栏成对，Operation 状态口径一致。真实 Cat 软件及安全、客户端联调尚未执行。


## 5. SQLite DDL 隔离验证

对设计修订提交 `983860a2cf7972a6fa98619a653413cff8b1f56b` 中 F §3.1、§3.2 的 DDL 提取后拼接，长度 6624 字符，FNV-1a 校验值 `eea8fe3d`；与隔离测试环境中的 SQL 文本一致。使用 Python 标准库 sqlite3（SQLite 3.46.1）在临时数据库中执行，未访问实际项目数据。七表创建成功，WAL、foreign_keys=ON、synchronous=FULL 生效；验证以下约束符合预期：孤儿 Grant 拒绝；最长租期限制；跨作用域同 request_id 可并存、同作用域重复拒绝、跨主体 workspace 拒绝；缺恢复清单的 PREPARED 和缺回执的 COMMITTED 拒绝；删除记录允许空 after_hash，多路径 revision 可存；重复 final 审计及缺备份的 AVAILABLE 修订拒绝；上传偏移超限、replace 缺版本拒绝；有引用的工程不能物理删除；foreign_key_check 无违规。

此为实际 DDL 及选定数据库约束的冒烟验证，不等同 CAT-07 迁移实现、业务状态转移验证、真实断电/文件系统恢复、ACL 安全或独立 tst 验收；不推进任何标准任务状态。
