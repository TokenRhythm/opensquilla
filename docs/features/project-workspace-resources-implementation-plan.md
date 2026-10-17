# 项目、规则与文件交付实施计划

状态：P1 已有代码与独立 review，首轮受影响集合 195 passed、3 skipped；完整 CI 另外发现旧数据库、CLI 默认目录及发布文档检查遗漏，正在补齐回归。P2 正在实施；P3–P6 尚未实施。P1/P2 没有 prompt 修改。本文第 5 节仍为后续阶段草案，实际 prompt 变更在对应阶段单列。

提交要求（2026-10-18）：继续按六阶段隔离实现与 review，但必须完成全部六阶段和最终联调验收后再创建 PR。已提前创建的 P1 PR #1930 已关闭；各 PR 标题、正文和评论使用英文。P2 正在本地实施。

基线：2026-10-17 核对的 `origin/main d3f926c17e361b27f0410e6ea52526c9a2bcbb3d`。reference 为本地固定快照，不等于最新版本或交互验收。原报告为分析工作树中的 `docs/design/project-workspace-resources.md`，未修改；其中的实施指令不作为额外授权。

## 1. 收敛结论与范围

目标：新建/打开项目含义明确；`/init` 根据证据生成或完善指南，指南进入实际模型上下文；资料写回真实读取的位置；历史交付和渠道发送准确反映版本及结果。

继续暂缓完整迁移、全局 USER、项目独立记忆、统一存储管理页、扩大清理、Git 管理和公开网站部署。保留 Agent 资料布局、memory.source 和现有覆盖配置。记忆仍按 Agent 使用、跨项目共享；历史混放文件不自动分离。

反复审查后，首版删除以下过重机制：

- **每次 provider 请求热刷新、动态目标整批工具阻断**：改为每轮聊天读取规则快照。执行中的外部变化下一轮生效；深层规则按任务读取，不保证首次工具动作前自动发现。
- **init 专用提交工具、完整原子发布、持久操作账本**：复用现有 source 文件工具和冲突检查，采用普通文件编辑的恢复边界。
- **项目创建的持久事务、新历史类型、新渠道兼容映射/状态机**：分别复用排他创建与项目登记、现有资源引用、现有 outbox。

保留核心体验，同时明确降低原稿中实时发现、固定目标事务、跨重启精确认领提交的强保证；不能只删实现而保留那些承诺。

| 原报告范围 | 本轮处理 |
|---|---|
| F01–F08 项目入口、保护、init、规则 | 实施；已有规则增量完善；自动加载限于每轮根到默认 CWD |
| F09–F12 资料、人格、记忆 | 原位置读写同源；不合并全局 USER |
| F13 私人资料权限 | 新入口接入现有可信授权和 caller 限制，不重构全部身份系统 |
| F14–F15 工作区/CWD | 保留已有绑定，只修交互工具遗漏的隐式回退 |
| F16–F23 文件、预览、交付 | 修现有入口、固定版本、完整材料和真实状态，不重写资源层 |
| F24–F25 main/side、关闭 | 共用现有生命周期，不扩大删除能力 |
| F26–F28 存储管理、迁移、扩大回收 | 暂缓；本轮新增临时 ZIP 的释放必须完成 |

## 2. 用户体验和首版边界

| 场景 | 完成后的行为 |
|---|---|
| 新建项目 | 填名字、选父目录，看到最终路径和执行主机；创建空目录并登记，不自动写资料模板 |
| 打开已有项目 | 登记现有目录，不复制、不补模板、不搬里面的记忆 |
| `/init` | 读取项目配置、文档和现有规则；创建或增量完善指南，报告实际路径及修改 |
| 使用规则 | 下一轮 main/side 读取根至默认 CWD 的规则；执行中外部编辑可能下一轮才生效 |
| 保存资料 | 项目 A 修改称呼/人格，项目 B 使用同一 Agent 时读到更新，不误建任务目录副本 |
| 工作文件 | 文件树直接预览当前文件，保留现有编辑能力 |
| 历史交付 | 发布 A 后改成 B，旧卡查看/下载 A；点“继续编辑当前版本”才打开 B |
| HTML 渠道 | 自包含网页可发单文件；有本地依赖时发固定材料 ZIP，需解压查看 |
| 发送状态 | 准备完成与已发送区分；失败说明原因；未知时待确认，不重复上传 |

规则是模型指导，不是新工具权限。首版不全树扫描，不分析任意 shell/脚本动态路径，不硬性拦截新子目录的首次操作。用户需要立即采用执行中修改的规则时，应在下一轮继续任务。

外部打开沿用既有能力，说明原文件/副本及实际执行主机。远程 WebUI 不能把服务端路径称为浏览器本机路径，历史内容不能静默切成当前版。

## 3. Reference 比较

参考 prompt 的要求不等于文件系统强制保证。来源定位在第 8 节。

| 产品、固定提交 | 实际逻辑 | 采用与边界 |
|---|---|---|
| Codex `ab82af0b2cfc1e894de72f5971b79d451dc2b337` | init 专用提示走普通 turn；提示要求已有 AGENTS 则停止；规则有缓存 | 学普通 turn、有证据的指南；本方案选择支持增量完善 |
| ZCode `29628c9acdb81b703bbd4080c207a0e7ce5e276e` | builtin prompt 走普通执行链，要求先读后增量 Edit；规则取最近单份并复用快照 | 学 create/update、输入与提示分离；不认为生成文件就解决实时刷新 |
| Hermes `6e7b83a2f1c093e992a8d838ed77ea320fbd8e51` | 精确命令、观察到的约定、保留原文；分层规则、工具后补充 | 学质量标准与小修改；init 根固定绑定，不跟临时 shell cd |
| DeepSeek Harness `d347e703908d0406b7a7ef80e3a0e594d86b2215` | 未发现同类内建 init；已有规则在步骤/工具结果边界发现 | 只作上下文机制参考 |
| Pi `f13c00fb3a4d77ad3faa815057e31d614d42b3f1` | 默认无 init；分层资源、显式 reload | 学刷新边界明确，不因此要求引擎热刷新 |

每轮重读比永久会话缓存及时，又不改 provider 请求或并行工具调度。HTML 渠道/历史卡依据 OpenSquilla 自身的 artifact、preview、outbox，不声称这些 reference 有同款机制。

## 4. 六个 PR 与依赖

| PR | 内容 | 依赖与发布边界 |
|---|---|---|
| P1 | 项目与重叠资料源的非显式写入保护 | 先建立保护边界，不全局停维护 |
| P2 | 新建/打开、默认位置和 CWD | P1；保留既有路径授权 |
| P3 | 有效来源、窄 agent_file 写回、资料 prompt | P1；工具和 prompt 同批上线 |
| P4 | 每轮规则读取和普通 turn 的 init | P1；复用文件工具，不依赖新事务 |
| P5 | 历史交付查看与继续编辑当前 | 可独立推进；复用资源类型 |
| P6 | HTML 完整材料、outbox 兼容、状态和交付 prompt | 同一行为 PR，避免先换 ZIP 后修状态 |

原 P4/P5 合为新 P4；原 P6 强门控后置；原 P7 为新 P5；原 P8/P9 合为新 P6。P2/P3/P4 在 P1 后分别推进，P5/P6 可并行。PR 数不是架构目标：可先做不改行为的窄提取，但不形成长期双路径。每个相关 PR 同时接入 main/side。

### P1：保护项目，不扩大为全局维护重构

实际入口包含列表/读取 ensure、启动 bootstrap/模板升级、raw/turn 搬迁，以及独立 `maybe_migrate_legacy_memory("data")`。[S1][S2]

| 有效来源 | 非显式维护处理 |
|---|---|
| 应用管理的 Agent/state/专属 memory 源 | 保留 missing-only 初始化、精确指纹+备份+锁的模板升级及已配置 TTL |
| 用户项目/执行根及与其重叠的资料源 | 列表/读取纯读，不自动播种、升级、搬迁或删除过期源文件 |
| 归属无法确认的 legacy 源 | 跳过破坏性维护并记录具体原因，不按文件名认领 |
| 自定义但明确用于应用状态/专属 memory 的目录 | 不因为路径自定义就停 TTL，按真实角色和重叠关系处理 |

- Registry 与 RPC fallback 读取只解析路径；缺文件返回缺失。显式创建 Agent/初始化保留原目标上的初始化能力。
- 旧模板指纹不能授权改用户项目；也不要求所有应用模板补 provenance。避免项目保护导致全用户模板升级失效，不建所有权数据库。
- raw/turn 复用名称+内容标记原地分类；正常 archive 笔记保留。旧 data 迁移相对进程 CWD 移动 DB/WAL/SHM/目录，也必须纳入保护。
- 受保护源 TTL 同时覆盖启动、后台和 memory_save 内联 prune；仅该源暂停删除。显式保存仍可改用户指定目标，不附带删其他旧笔记。

实施补充：保护集合复用既有项目登记和会话绑定，包含已移除项目的原路径；移除列表条目不授予删除原件的权限。默认托管 workspace 保留模板维护；显式配置的共享执行根即使与默认路径相同也受保护，与既有执行目录分配逻辑一致。自定义独立 state/notes 目录继续维护。每次实际 TTL 删除前重新判断，覆盖 sweep 中途打开重叠项目的情况。
- scanner 排除后仍需核对旧 indexed paths、删除确认不属于 curated 的派生行，查询侧不返回已确认 raw/turn。只改可重建索引，不搬源、不按目录粗略排除、不新建索引体系。

影响：同一根既作为项目又开启 TTL 时，项目无非显式删除与原 TTL 冲突；仅暂停该源并说明原因。来源判断用既有项目/会话绑定和已解析位置，不扫描用户磁盘。

验收：临时项目包含自定义/空/旧模板规则、普通笔记、raw/turn、过期笔记、旧索引和旧 data 库。启动/读/重启/显式保存后，除指定目标外目录树/hash 不变；独立应用源升级与 TTL 仍有效。

### P2：最小创建流程与工作位置

当前 UI 的 create 最终走要求已有目录的 open。[S3]

- 保留 `workspaces.open` 的 owner、显式 trust、已有目录校验。
- 拟新增 `workspaces.create`：父目录+单段名字，校验权限、Windows 保留名/尾随点空格及父路径；排他 mkdir，再复用按 path_key 的项目登记。
- 同名已存在不覆盖、不接管。取消/失败至多 best-effort rmdir 本调用仍能证明拥有的空目录；非空/身份变化则保留。
- 不新增持久创建事务。丢响应先查登记；若只剩未登记目录，说明路径，让用户明确选择“打开已有”或改名，不自动认领/删除。
- 默认父目录按 Gateway/profile 保存偏好并显示主机。旧 Gateway 无 create 能力保留打开已有，不能偷偷把 create 变 open。
- 保留托管/共享/项目绑定，只修交互文件、shell、Python 默认根不一致和绑定失效后的回退；显式内部 scratch 作业保留。

现有绑定授权规范路径，不是永久 inode。短期回滚使用身份检查，不引入跨会话永久目录对象身份，保留目录恢复的现有契约。[S4]

验收：中文/空格、并发同名、拒绝、取消、失败后外部写入、丢响应、目录消失/链接改变、远程端；三个工具相对路径落点一致，显式 CWD 不污染下一调用。

### P3：真实来源与薄资料工具

来源必须分别保留 Agent bootstrap 根、MEMORY.md 所在根、notes_dir。现有 state/main 分支允许 `OPENSQUILLA_MEMORY_DIR` 单独覆盖笔记目录；保留 workspace 分支和 Agent 作用域的既有优先级，不能用 notes.parent 重新定义 MEMORY.md 位置。[S5]

- 从 resolver/MemoryManager 传递实际来源到 runtime/memory tools/sync，不各自猜路径、不变覆盖优先级。
- Dream 沿用开关和调度；普通来源传正确根，split notes 窄支持独立 notes_dir，覆盖 Gateway/CLI 两个 factory 调用者。未支持时明确阻止会写错源的 Dream，不静默写另一处。
- 拟新增一个 `agent_file(read|replace)`，白名单仅 USER.md/IDENTITY.md/SOUL.md；Agent/路径由服务端绑定。通用 Agent AGENTS 保留管理 UI/RPC；项目规则走 P4。
- 从 AgentRegistry/RPC 文件逻辑抽窄共享服务：纯读返回 revision；replace 带 expected revision、边界/链接检查、应用内同目标串行、写后核对，成功失效该 Agent bootstrap。缺文件使用 absent revision。
- UI/RPC 共用失效回调。若新增 revision 参数，旧调用保持兼容，新 UI 携带预期版本；不把旧调用突然变成必填。不宣称可阻止所有外部编辑器竞态。
- 不新建持久操作账本或通用原子发布框架。现 mutation receipts 是 ctx 内记录，Full Host/工作区外可能不记，不能用于跨重启恢复。[S6]
- memory_save 保留索引失败回滚源的契约，按最终状态报告，不改异步事务；agent_file 只刷新 bootstrap，不写 memory 索引。

权限/可见性必须同时接线：[S7]

- 接既有可信 owner 与 private-source 会话限制；无 ctx/未知身份拒绝；CHANNEL 需已有 verified channel-admin，普通 DM 不自动获权。
- SUBAGENT/CRON 在原 deny/allow surface 和 handler 都拒绝；子 Agent 可能带 is_owner=True，不能据此放行。
- 合法 Web/CLI owner、main/side、可信 channel 管理员须在现有 surface 显式可见；default-deny 配合 surfaced，同时保留用户显式 deny。
- 保留真实工具策略/run mode/审批；PlanAccess 已是 legacy metadata，不把它当硬权限，不新建 Plan 门控。

验收：两 Agent×两项目、workspace/state/split notes、只读/缺失源、UI/模型互改、冲突/取消/丢结果、索引回滚；各 caller 的可见与实际执行权限。混放源按原配置报告路径，不声称已迁移隔离。

### P4：每轮规则快照和普通 init

规则读取：

- 在现有 PromptAssemblyStage 每轮读取绑定根到默认 CWD，放动态上下文；不建 watcher/热缓存，不改 provider-step 和并行 dispatch。[S8]
- 每目录 AGENTS.md 优先，兼容 agent.md；双文件只选标准，只有兼容就沿用。同一物理文件去重，不越绑定根读祖先；更深约定只适用其子树。
- 每文件/总预算有上限，来源/范围/截断/失败明确；超预算按任务读取原文件，不宣称全部已读，不建立新的写入硬门控。
- 本轮快照稳定，重试/压缩使用同一内容，下一轮重读。main/side 各读共享文件当前状态，父聊天快照不改写。

init 接入：

- 复用已有 CommandCategory.TURN/NEXT_TURN 和普通 dispatchSend/turn runner；WebUI 仅补所需 catalog 投影，不另建命令类型或执行器。[S9]
- 在原 send envelope 拟加窄 `turnCommand={name:"project.init",args:...}`，使服务端统一展开提示并识别显式命令。只含名/参数，不传路径/revision/候选/事务。
- 字段随既有 pending/WAL/队列/fingerprint/receipt；literal `//init`、引用、附件、历史不触发。服务器用有效会话绑定根，失效则报错不回退。
- canonical 显示原 init，InputStage 一次性加载模板。CLI/TUI/渠道按 adapter 能力完成情况展示，旧 Gateway 明确不支持。CLI `opensquilla init` 配置向导不变。
- 无规则则创建，有则完整读取、最小补全；空文件可填充；双候选只改选中者。无项目证据时说明不足，不写空模板/套话。
- 用已有 read_source/create_source/edit_source；三者默认 deny，init 在原 surfaced_tools 显式展示，仍经过 allow/deny/profile/路径权限。以最终授权工具表启用写能力，不按 SAFE/Full Host/CodeMode 名称猜可用性。[S10]
- **现 source 行范围不支持零行文件**：补通用 source 工具的小型“空文件读取 revision/零行插入”契约及对应测试，使已播种的空 AGENTS 能被填充。不要为这个缺口另造 apply 工具。[S10]
- 普通排他创建、expected_revision、freshness、权限、取消/错误处理继续有效。无必要工具时说明不可写；不绕到宽权限工具替代。任务目标约束不冒充固定目标沙箱。
- 丢结果后重读内容/revision；相同字节只证明当前文件包含该内容，不认领原操作、不盲目覆盖。保留不确定状态；不承诺普通工具没有的原子完整发布、外部 CAS 或持久操作恢复。新规则下一轮生效。

验收：精确命令有来源、已有内容保持、空/兼容/双候选、冲突/拒绝/取消/丢结果；转义、排队、重连；source 工具真实可见性和空文件全链；provider 消息有规则，本轮稳定/下轮更新，main/side 同组。

### P5：复用资源身份区分历史与当前

保留后端 `workbench.resources.open(edit-current)`，修 ChatView/provider 的默认历史查看路径。[S11]

- 优先使用已有 WorkbenchResourceRef 的 deliverable/document、规范 type:id 及 workbenchResourceType/workbenchResourceId，不预定新 historical enum。legacy resourceIdentity 可能只是普通 artifact identity，不能将所有非 document 字符串当作已解析、授权的 deliverable。
- 历史固定原 artifact ID；HTML 用已有 lease/bundle，Office 用 previews.create。覆盖 load Document、head 替换/事件、下载、外部打开和编辑按钮。
- 不把所有历史 HTML 套 preparedPreview，它会强制 offline；保留现有端能力和安全限制。
- 继续编辑明确走 edit-current；无映射时只有用户选择才创建工作副本，不按文件名猜、不因查看隐式导入。
- 历史材料已缺失/过期明确失败，不拿当前文件或当前源码冒充。旧 Gateway 使用既有安全 fallback，无预览能力保留可用的原 artifact 下载。
- 文件树仍直接预览工作文件；child 授权、parent 展示、epoch/Gateway/关闭 guards 保持。

验收：发布 A→修改 B，历史预览/下载保持 A，外部单文件副本保持 A 的字节，head 更新不改变它；继续编辑才 B；缺材料不冒充 B。多文件 HTML 的完整外部使用走现有 ZIP 导出，单文件副本不承诺带齐 CSS/图片，不为此新增解包目录、外部浏览器服务或缓存。HTML/Office、main/side、远程/无能力端实际点击，保留 edit-current 恢复测试。

### P6：HTML 包与真实状态同时落地

复用 bundle/ZIP/outbox，不新增导出体系、兼容映射或状态机。[S12][S13]

- 窄提取现有 manifest 校验/ZIP 构建供 Gateway/channel 共用，保留并发/cleanup；不反向依赖 Gateway HTTP，不建持久 ZIP 缓存。
- 自包含 HTML 可单文件；有本地依赖时按固定 manifest 打包。**实际 path.name、ZIP 字节、request filename/MIME/size 一致**；多个 adapter 用 path.name，不能路径仍叫 html。
- 缺失/越界/不安全依赖在上传前失败；大小/扩展名沿用 adapter 已知限制和平台实际拒绝，不虚构统一的全渠道预检。DingTalk 沿用 ZIP 支持避免二次套包；不支持普通文件的 QQ 保持原 fallback。
- 动态/远程依赖不承诺完全离线；不回退残缺 HTML 冒充完整，不递归打包项目。
- 自动补登记只改变现有本轮新建/跟踪/最终提及的 HTML 候选为 auto bundle；documents.publish 的 revision/attempt/commit/promote/恢复不变。
- 继续使用旧 sender 根据原 artifact 元数据能算出的 delivery_id，payload 记真 ZIP。直接复用 begin_send_once/replay/mismatch。
- 同 key 材料不同会安全拒发，不等于成功重放。旧 sent/unknown 不因升级重传；旧 failed+retryable 也可能无法 claim，需待核对/可用下载。用户新的明确发送请求可按既有新 inbound identity 投递，不暗换 ID 重试旧请求。
- 临时材料覆盖 awaited 上传消费期；复用压缩 worker shield/settle/cleanup。取消时还需等待已有 to_thread 读取收敛（如 WeCom），不能 coroutine 结束就提前删文件。第三方 adapter 返回后继续消费须自行持有副本，不为假设的后台队列建 spool/GC。
- 状态使用既有任务/fallback 呈现，不额外发一串渠道消息、添加事件系统或模型往返。权限拒绝不能通过 URL fallback 绕过；原 HTML 下载不能说成完整 ZIP 已交付。

| 实际事实 | 呈现/动作 |
|---|---|
| 材料准备好，无成功回执 | 成果已准备，不能称已发送 |
| 发送调用中 | 发送中，不并发重传 |
| 成功回执且材料匹配 | 已发送，复用回执 |
| 准备失败/确定发送失败 | 原因明确，仅原状态机允许且身份匹配才重试 |
| unknown/未确认/身份不匹配 | 待确认或材料不匹配，不上传、不伪造成功 |

验收：ZIP 成员/SHA、源后来变化不影响固定包、无额外文件；真实 path.name/metadata；旧新方向的 sent/unknown/failed-retryable；延迟压缩/读取/上传时取消仍持有材料；DingTalk/Feishu/legacy send_file 等有实际差异的 adapter。真实渠道仅用授权目的地，不穷举所有渠道替代风险判断。

## 5. Prompt 改动：逐字草案与注入位置

尚未写入代码；工具与 prompt 同批上线。原“规则变化导致整批未执行”Q3 已取消。以下四组取代旧五组，超出这些文本的改动需在实施 PR 明示。

### Q1：init 单次任务模板

拟新增 `src/opensquilla/identity/templates/project_init.md.j2`，共享 InputStage 仅对已验证 project.init 加载；变量来自会话根/候选选择与用户参数。

```text
The user invoked /init to create or improve project instructions.
Bound workspace: {{ workspace_root }}
Selected instruction file: {{ selected_path }}

Inspect the existing instructions, manifests, README/docs, directory layout,
and build/test/lint configuration before editing. Include only project-specific
facts supported by those sources: useful directories, exact commands, observed
conventions, architectural boundaries, and concrete pitfalls.
Use read-only exploration. Do not install dependencies, initialize Git, run
project setup/build/test commands, or create profile/persona/memory files.

If instructions exist, read them completely. Preserve the user's wording,
structure, and rules, making small edits for missing or demonstrably stale facts.
If absent, create a concise guide. Aim for fewer than 100 lines for new content;
do not delete existing user content to meet that target.
If evidence is insufficient, explain what is missing instead of writing an
empty file or generic template.

Use read_source before editing; create_source for a missing file; edit_source
with the read revision for existing content. Recheck the selected instruction
candidates before creating. Do not create a second instructions file or change
the target after a shell cd.
On conflict or an uncertain result, reread before further changes.
Report only the file path and changes supported by tool results or readback.
Updated instructions will load automatically at the next chat turn.

Additional instructions from this /init request:
{{ user_notes }}
```

上述三个工具须已由现有 surface 合法提供；缺能力不注入“能够保存”的指令。遵循已有路径授权，prompt 不是额外授权或新沙箱。

### Q2：每轮项目规则块

拟新增 `src/opensquilla/identity/templates/project_rules.md.j2`，由 `engine/runtime.py::_assemble_prompt` 动态部分经现有 PromptAssemblyStage 每轮加载，不增加 provider-step 回调。

```text
Project instructions for this chat turn:
Workspace: {{ workspace_root }}
Apply each file within its listed scope. More specific conventions apply within
that directory. They do not override explicit current user instructions, system
requirements, or tool permissions.
This is a turn-start snapshot. Automatic loading covers the workspace root through
the default working directory. Read relevant deeper-directory instructions when
working there. File changes will be loaded automatically at the next chat turn.

{% for rule in rules %}
Source: {{ rule.path }}
Scope: {{ rule.scope }}
Read status: {{ rule.read_status }}
{{ rule.content }}
{% endfor %}
```

截断/失败标出具体文件和读取提示，不宣称全读，不附硬性批次阻断文案。

### Q3：资料保存指导（替换旧 Q4）

替换 `src/opensquilla/identity/templates/system_prompt.j2:208–229`，去掉 active workspace 和普通文件工具保存 USER/MEMORY 的错误默认，保留相邻 Memory Recall。

```jinja
{% set has_memory_save = tools and "memory_save" in tools -%}
{% set has_agent_file = tools and "agent_file" in tools -%}
{% if prompt_mode == "full" -%}
## Profile and Memory Updates

- Profile and memory files belong to configured Agent sources, which may differ
  from the task workspace and from one another.
{% if has_agent_file -%}
- Use agent_file for USER.md (stable user profile), IDENTITY.md (assistant
  identity), and SOUL.md (persona). Read first, preserve unrelated content,
  and supply the returned revision when replacing. The runtime selects the source.
{% endif -%}
{% if has_memory_save -%}
- Use memory_save for durable non-profile facts in MEMORY.md and daily/named
  notes under memory/. These are logical paths in the configured Agent sources.
{% if "memory_get" in tools -%}
- Read existing content with memory_get before replacing it.
{% else -%}
- Do not replace existing memory content unless you have read it completely.
{% endif -%}
- Do not use memory_save for USER.md, IDENTITY.md, or SOUL.md.
{% endif -%}
- If the relevant persistence tool is unavailable, explain the limitation;
  do not create a task-local profile or memory file as a substitute.
- An ordinary file requested at an explicit path remains a file task. Keep
  task deliverables separate from configured profile and memory sources.
- Source files are authoritative; the index is derived. Report a saved update
  only when its final state is confirmed. Respect rollback results and distinguish
  an unresolved outcome from a successful save.
- Do not persist fleeting emotions, in-progress state, or session-only facts.

{% endif -%}
```

| 同步的工具说明 | 拟议文本 |
|---|---|
| 拟 `tools/builtin/agent_files.py` 的 agent_file | `Read or replace USER.md, IDENTITY.md, or SOUL.md for the current Agent. The runtime selects the source. Read first; replacement requires the returned revision. Preserve unrelated content. Existing caller authorization and tool policy apply.` |
| `tools/builtin/memory_tools.py:698` | `Write durable non-profile memory to the configured Agent sources, which may differ from the task workspace. Profile and persona files use agent_file when available, not memory_save.` |
| 同文件 memory_get path（759） | `Logical path in the configured Agent memory sources: MEMORY.md or memory/**/*.md.` |
| 同文件 memory_delete path（825） | `Supported logical file path in the configured Agent memory sources to delete.` |

不改变 memory_save schema/replace/append/回滚。无保存工具但有普通文件工具时，“不创建无效副本”仍需渲染。

### Q4：交付说明（替换旧 Q5）

| 位置 | 拟议文本 |
|---|---|
| `identity/templates/system_prompt.j2:101` | `When the user requests a file deliverable, prepare the final workspace file and its required local materials with publish_artifact.` |
| 同文件 107 | `If publish_artifact reports that the same unchanged file or bundle is already registered, do not register it again. Publication prepares the deliverable; do not claim native channel delivery without a confirmed delivery receipt.` |
| `tools/builtin/artifacts.py:219` description | `Prepare a fixed snapshot or bundle of an existing workspace file for delivery on the current surface. This does not deploy a website or confirm a channel send receipt.` |
| 同文件 `_publish_note`（153–177） | channel 场景增加 `Channel delivery success is determined by its delivery receipt.`；`on this machine` 改成 `on the execution host` |
| `tools/builtin/workspace_preview.py:26` | `Correct HTML input errors and call open_workspace_preview again after edits.` |

保留普通 HTML 直接 preview、明确交付才 publish、URL/权限/bundle 参数。历史卡行为不增加模型 prompt，不扩 Goal 指导。

## 6. 横向影响与验证

| 影响面 | 必须处理 | 控制复杂度 |
|---|---|---|
| 旧源/混放 | 非显式保护、显式写真实来源、旧 raw 索引 | 不搬源、不全局停维护、不建所有权库 |
| 自定义路径 | MEMORY 与 notes 分离，Dream 两入口 | 传真实路径，不猜共同父目录 |
| 权限 | caller、surface、子 Agent/cron 不能借 owner 获权 | 只接新工具，不重构身份系统 |
| 命令 | literal、WAL/fingerprint、旧端能力 | 一个窄字段，复用普通 turn |
| Prompt/压缩 | 本轮快照/计量一致，下轮更新、去重/预算 | 无热刷新或批次门控 |
| 文件修改 | 空文件、先读、冲突、取消/结果不明 | 小修通用 source，不冒充持久事务 |
| main/side | child 权限、父展示、首次快照、折叠/关闭、迟到响应 | 共用原执行/生命周期 |
| 移除项目 | 保留暂停关联 cron、阻止新执行、目录保留 | 移除登记与删历史不混淆 |
| 历史资源 | 所有路径固定 ID，材料缺失明确失败 | 用已有 deliverable/document |
| 渠道 | 真 ZIP 元数据/文件名、限额、临时材料寿命 | 复用 outbox/cleanup，不建通用预检或 spool |
| 升降级 | 同 key 不重发；旧 Gateway 明确能力 | 不建新旧键映射，不删文件“回退功能” |

每 PR 运行直接受影响测试和必要契约检查，不每次全量回归：

- P1/P3：registry/bootstrap、memory ownership、write notifications、snapshot invalidation；补维护正反例、split notes、caller surface、写入/索引失败。
- P2：workspaces RPC、execution workspace 和 UI；补创建失败/丢响应/恢复，保留 cron 移除行为。
- P4：turn command/InputStage/PromptAssemblyStage/source tools；实际 provider 消息、转义/排队、空文件、下轮刷新、主侧一致及指南质量。
- P5：ChatView artifact lightbox、provider/fileActions、workbench RPC；A/B 固定、缺材料、既有 current-open。
- P6：自动候选、channel contextual/idempotency、ZIP manifest、documents.publish 恢复；慢压缩/读取/发送取消、旧新记录和 failed-retryable。

最后用隔离 profile/临时项目做真实 main/side 流程：打开不改文件→init→下轮用规则→资料跨项目有效→直接预览→A 发布/B 修改/旧卡 A→授权渠道收完整材料。Desktop/外部应用补运行验证。源码检查、模拟探针、浏览器、真实渠道、安装包分别报告，不互相替代。

不预设未经测量的毫秒/token 指标。记录每轮规则读取数量/字节、prompt 增量、ZIP 时间/峰值临时材料；确认无全树扫描、无规则不增加模型往返、无 ZIP 交付不打包。能力按功能启用，不新增迁移开关或全局新旧模式。

## 7. 复审记录与剩余实施门槛

以下是设计审查，不把未来测试写成已经通过。

| 轮次 | 方法 | 实质结果 |
|---|---|---|
| 第一轮 | 三路独立反证，允许推翻原建议；回查 reference/主线 | 删除热刷新、整批阻断、init 专用提交、创建持久事务；纠正非持久 receipt |
| 第二轮 | 检查简化会否带来功能/权限回归 | 保留独立源维护；补 split notes/Dream、subagent/cron/surface、source 空文件；ZIP/状态合并 |
| 第三轮 | 修订稿再交叉检查调用链、prompt、范围和验收 | 三路均未发现阻断性设计矛盾；修正 source 契约路径，收窄多文件 HTML 外部打开保证，明确 legacy identity 不可直接当授权资源 |

小型模拟证据：现有 outbox begin/retry/replay 函数+内存 SQLite，对原 HTML/新 ZIP、sent/unknown 双向同 key 均拒绝额外发送，返回 nonretryable identity mismatch；相同材料 failed 可 claim，HTML→ZIP failed 不可 claim。仅日志依赖用未调用的 stub；没有 provider/真实 ZIP/旧二进制/重启。它证明该函数组合安全拒发，不证明完整产品发送或成功回执重放。

实施前需冻结三个窄接点，不扩架构：

1. split notes 经 Gateway/CLI Dream 传递；选择窄 notes_dir 参数，不改变旧源。
2. 各端命令身份及最终 source 工具 surface；补通用空文件契约，不另造工具系统。
3. adapter 的 ZIP 支持和消费完成/取消契约；用现有结果类型补语义，不新增发送系统。

没有发现需要恢复迁移/统一存储才能完成核心功能的理由。剩余风险在实现接线、平台文件行为、具体 provider，需测试与实际运行证据；不承诺“所有风险已消除”。

## 8. 源码证据索引

基线行号；缩写后端路径相对仓库内 `src/opensquilla/`，前端路径相对仓库根。

| 编号 | 定位 | 支持结论 |
|---|---|---|
| S1 | `agents/registry.py:97,224`；`gateway/rpc_agents.py:46`；`gateway/boot.py:1068`；`gateway/workspace_template_upgrade.py:125,175` | 读取 ensure、启动与精确指纹/备份/锁升级 |
| S2 | `memory/manager.py:73,160,483,516`；`agents/scope.py:170`；`memory/sync_manager.py:108,343`；`memory/store.py:1258`；`memory/retrieval.py:235`；`tools/builtin/memory_tools.py:444,585` | 搬迁/TTL 多入口、旧索引 |
| S3 | `opensquilla-webui/src/App.vue:1506,1534`；`gateway/rpc_workspaces.py:109,185`；`project_workspaces.py:65` | create 实为 open、trust/owner、移除关联 cron |
| S4 | `execution_workspaces.py:1,33,88`；`gateway/execution_workspaces.py:21`；`tools/builtin/code_exec.py:1363` | 路径绑定与短期回滚、默认目录 |
| S5 | `agents/scope.py:98,140`；`memory/manager.py:516,603`；`engine/runtime.py:9729`；`memory/dream_factory.py:125`；`memory/dream/runner.py:185`；`cli/main.py:243` | 真实源、分离 notes、Dream 两入口 |
| S6 | `agents/registry.py:126,255`；`gateway/rpc_agents.py:154`；`tools/mutation_receipts.py:54,87`；`tools/builtin/memory_tools.py:599` | 当前写回、非持久回执、索引失败回滚 |
| S7 | `tools/types.py:58,74,407`；`tools/visibility.py:350`；`tools/policy/checks.py:138`；`engine/agent.py:17151`；`gateway/channel_dispatch.py:2160` | PlanAccess 非硬门槛，caller/owner/surface |
| S8 | `engine/turn_runner/prompt_assembler_stage.py:453`；`engine/agent.py:6183,7197,12383`；`engine/runtime.py:9693` | 每 turn 装配，强实时会影响投影/并行 |
| S9 | `engine/commands.py:90,530`；`gateway/rpc_commands.py:63`；`opensquilla-webui/src/composables/chat/useChatSend.ts:2335,2604` | 已有 TURN、投影差异、slash 转义 |
| S10 | `tools/builtin/filesystem.py:1876,2427,2451,2547,3021`；`sandbox/filesystem_worker.py:740,772`；`tools/source_edit_contract.py:167` | source 可见性、排他/revision、空文件缺口 |
| S11 | `opensquilla-webui/src/views/ChatView.vue:5906`；`opensquilla-webui/src/types/workbenchResources.ts:5`；`opensquilla-webui/src/workbench/workbenchResourceItems.ts:21,116`；`opensquilla-webui/src/workbench/artifactItems.ts:122`；`opensquilla-webui/src/components/workbench/artifactWorkbenchProvider.ts:230,263,420,886,2179,2198`；`gateway/workbench_resource_runtime.py:1240,2227` | 既有引用类型、legacy identity、head 替换、单文件外部副本、固定预览/current-open |
| S12 | `tools/builtin/artifacts.py:365,499`；`gateway/html_archive.py:38,131,198`；`engine/artifact_delivery.py:229,421`；`channels/artifact_delivery.py:143,180,202` | bundle/ZIP/cleanup、候选和渠道 entry file |
| S13 | `channels/delivery_store.py:867,881,1233,1283,1339`；`channels/types.py:132`；`channels/wecom.py:1041` | delivery_id/mismatch/retry、线程读取取消 |
| R1 | `C:/code/reference/codex/codex-rs/tui/assets/prompt_for_init_command.md:1`；同仓库 `codex-rs/core/src/agents_md_manager.rs:87,118` | init 提示与缓存边界 |
| R2 | `C:/code/reference/ZCode/apps/zcode-cli/packages/bootstrap/src/builtin-prompt-command.ts:74`；同 packages `adapters/src/context/index.ts:204`、`core/src/runtime/methods/context.ts:31` | 增量编辑、最近规则和快照 |
| R3 | `C:/code/reference/hermes-agent/hermes_cli/init_command.py:8,30,58`；同仓库 `agent/prompt_builder.py:1717`、`agent/tool_executor.py:1110` | 指南质量、保留原文、工具后发现 |
| R4 | `C:/code/reference/deepseek-harness/packages/context/agent-instructions/src/index.ts:313,341` | 步骤/工具结果边界发现 |
| R5 | `C:/code/reference/pi-agent/packages/coding-agent/src/core/slash-commands.ts:19`；同 core `resource-loader.ts:232,646`、`agent-session.ts:3685` | 无默认 init，分层/显式 reload |
