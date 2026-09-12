# OpenSquilla：第一阶段 Runtime 纯删除方案

日期：2026-09-09。版本：v2（升级非阻断护栏）。第 1–10 节保留完整阶段的规划记录；本 PR 仅实施 D01，D02–D10 尚未实施。D01 的实际基准、归档及验证状态见第 11 节。

本文件取代 `/private/tmp/opensquilla-runtime-deletion-plan-20260909.md` 的 v1 方案，是后续删除 PR 的仓库内依据。源码基准 SHA 见下文；实施时将本文件和实际删除基准一并提交并在 PR 中固定链接。本次随 D01 PR 将规划纳入版本控制；历史规划中的全阶段验收不等于本 PR 的实施范围。

## 1. 本期范围

按用户确认，本期只退出已有机制及其专属控制面，不开发替代功能，不调整保留能力的默认值，不重写 Agent 架构，不新增 Profile、插件、恢复框架或统一策略层。

删除包括必要的 import、内部构造传播、分支、模板、测试和文档引用清理。升级兼容只允许复用现有配置迁移／诊断做最小退役适配；不借此建立新的兼容框架。代码净变化不必每一行都是减号，但不得产生新的 Agent 能力。

用户新增硬约束：旧实验字段、env、工具引用即使曾显式开启，也不能仅因本次退役导致升级启动报错。已退役功能不再执行，兼容入口负责接住旧输入并给出非阻断说明。其他无关配置错误和安全检查沿用现有规则。

审计基准：本地 `origin/main` 指向的 `f0981d61ceb8d43007030988487ca1eb20628a81`。本任务工作树 HEAD 是 `58b93c67dff18572f065c69a5d4d99584e1c4859`；实施必须从届时确认的 main 基线开始，不能把本文件中的行号直接套到旧工作树。实施前记录新的 immutable SHA 并复核本清单涉及的差异。

历史主要合入为 PR #569（`3784b2356`）和 PR #804（`115bee9df`）。两者混有正式工具、Provider、安全和兼容性修复，按机制删除，不整 PR revert。

## 2. 保留集先冻结

以下能力和当前默认值在本期保持原状，连同实际需要的依赖一起保留：

- Tokenjuice strict matcher、`FAILURE_PRESERVE`、既有规则及失败窗口。
- ToolResult projection、Store、动态 `retrieve_tool_result`、读取原文的权限和会话边界；保留 fresh-diagnostic／retrieval 等混合通路，避免本期顺带改错误展示策略。
- Provider request-proof、recent assistant/tool/error/unresolved 保护、never-worse、历史 projection marker 识别与执行阻断。
- Provider retry/fallback、reasoning-only prefill/continuation、context-block feedback；恢复过程保持当前请求的 thinking 设置不变。
- Identical-request breaker。本期不默认化 context feedback，先前设想的替代前提没有发生，因此暂留。
- `reasoning_only_act_now` 及其自动恢复路径。本期整项暂留：当前代码在 `not thinking_enabled` 时即使开关关闭也会执行一次恢复，不能按实验名字整删。
- Final-diff salvage、candidate 捕获、`lost/restored` 状态、`final_diff_salvage_veto`、patch instrumentation classifier。所有默认值不变。
- Source-diff preservation 主干和 mode。它负责标记候选 patch 被撤销，是 salvage veto 的实际依赖；本期只删除其中独立的 endgame freeze 扩展。
- 核心 `tools/policy/finalize.py`、mutation receipts、write tracking、sandbox、approval、workspace write policy、已配置 deny policy 的现有执法能力。
- 正常 Git capability discovery、status/diff/log、Git 不存在或不是仓库时的现有处理。
- Generic runtime event sink、router diagnostics、正常 tracing／bounded store 配置。
- Provider strict routing、deny envelope/cap 配置均保持原状，本期不类型化、不默认化、不顺便改上限。
- `contrib/codetask`、正式 `update_plan`、tool search、skills、Child/Meta、Scheduler 等产品能力。
- #804 中独立 bug 修复：超时部分输出、edit_file no-op 拒绝及失败提示、参数别名规范化、tool-name typo 提示、schema-invalid 示例、Provider 错误结果 head/tail 和 ToolContext 兼容性。
- 公开 dataclass 的历史位置槽，以及已发布实验关键字的必要 inert compatibility slots。它们只保留构造兼容，不参与运行决策；见 4.5。

## 3. 删除清单

表中“整文件行数”是 pinned Git blob 的物理行计数，包含空行、注释、专属测试和文档。混合文件局部删减另算，禁止按文件全长计数或重复计算。

| 编号 | 删除对象 | 删除边界与理由 | 已核定整文件行数 | 还需局部删除 | 预期影响 |
|---|---|---|---:|---|---|
| D01 | 实验控制面 | `scripts/experiments/*`、专属测试、`docs/experiments.md`；先确认 ledger 或 pinned 历史资产可取回原脚本 | 7,440 | 文档目录入口、CI 脚本／测试清单 | 产品入口不受影响；产品仓不再承担这些实验命令 |
| D02 | Finalize Evidence Gate 及外围 | 删除基础 gate、strict、variant challenge、patch-hygiene block、scratch verify mirror 的 tracker／挑战／重试。基础 gate 有正向信号但未稳定确认，按本次激进范围退出；不标成已证明无效 | 3,324（专属测试） | gate 模块约 1,470 行；Agent、prompt、mirror guidance、config wiring | 默认模型可见行为预期不变；旧启用配置失去结束前挑战，current-main SWE 可能变化 |
| D03 | Submit/review | 删除 builtin `submit`、隐式 review、diff checklist、anti-rubber-stamp 和确认状态机 | 1,097 | 注册、surface、Agent 拦截、参数传播 | 普通默认不暴露 submit；启用者不再获得提交检查。保留 shell process 的 stdin submit 操作 |
| D04 | Patch evidence 实验层 | 删除 PatchEvidenceLedger 与 Patch Evidence Protocol 的模型提示／记录消费 | 533 | prompt、Agent 记录／write-final、路径过滤中的 ledger 专属成员 | 减少实验记录与提示；mutation receipts、执行状态和通用事件不变 |
| D05 | Progress Watchdog／Post-write Convergence | 删除 progress 判定、warn/block、重复验证 steering、写入后强制收敛状态 | 1,236 | Agent 观察／提示／提前结束分支、env/config/Child 传播 | 默认模型内容预期不变；减少额外状态与检测。通用预算、timeout、tool-failure guard 保留 |
| D06 | Final-diff Contract／Runtime Capsule／Coding-loop Diagnostics | 删除 diff 分类告警、结束观察、capsule 注入、coding-loop observer 和 source-loop recovery steering | 1,854 | `runtime_recovery.py` 的 source-loop 专属部分；Agent 探测和注入；混合 diagnostics 测试 | 少做部分 Git 探测、指纹和事件；salvage 依赖的查询与候选捕获仍存在 |
| D07 | Deadline／Endgame 实验 | 删除 wrap-up、deadline thinking cutoff、reasoning stream 字符抢占、mid-budget no-diff、iteration extension、sticky cutoff、endgame fix、Git freeze 与 freeze instrumentation exemption | 0（本表按混合文件处理） | freeze 专属部分约 344 行，以及 Agent/stream/config/专属测试；保留 reasoning-only act-now 和 salvage veto | 默认模型可见行为预期不变；实验不再被 deadline 提示、抢占、延长或 Git freeze 干预；普通 deadline/cancel/retry 仍有效 |
| D08 | Placeholder／Repeat 实验提示 | 删除 placeholder escalation、repeated-call notice、可配置重复工具 nudge 与 extra-tools 列表 | 0 | dispatch、Agent、env、计数器及混合测试 | 不再额外注入提示；历史 marker 执行阻断、tool failure budget、identical-request breaker 保留 |
| D09 | 未采纳的请求／提示变体 | 删除 objective reminder、provider-history dedup、projection signal hints、text-only-tool steering，以及 tool description overrides | 672（description overrides 实现和专属测试） | Agent/registry/schema 的实验分支；各自专属或混合测试 | 默认模型可见行为预期不变；保留正常 request projection、工具定义、tool search、Provider malformed/empty 恢复 |
| D10 | 当前 CD_UNWRAP 字符串分支 | 删除前导 `cd/pushd` 的启发式剥离、env 和局部测试，不开发结构化 cwd 替代 | 0 | 约 90 行实现及 CD 专属测试；strict 与 FAILURE_PRESERVE 测试保留 | 默认模型可见行为预期不变；旧启用运行的包裹命令更多回退 generic，模型所见摘要及 token 可能变化 |
| 合计 | 已核定整文件集合 | 42 个文件 | 16,156 | 另有 gate 约 1,470 行和其他局部删除 | 是静态候选计数，尚不是实施后净删除值 |

本表 16,156 行拆分为：实现／脚本 7,141 行，专属测试 8,952 行，文档 63 行。不能说成 16,156 行 Runtime 实现。

D02 的基础 gate、D05/D06/D07 中曾出现在 GLM rich 配方的机制，以及 D10，均须在 PR 中标记“存在实验信号或组合证据，主动退出当前产品实现”。默认行为风险与实验分数风险分别说明。

影响披露统一为：**默认模型可见行为预期不变；默认内部观察、Git 查询和实验事件会有意减少。** `final_diff_contract_mode` 当前默认 `log`，D06 删除不是 byte-identical。配置 `runtime_events_path` 的用户将不再收到相关事件；coding-loop/source-loop 的实验观察数据会消失。保留的通用事件、Provider、工具结果及安全路径不受这项退役扩大影响。

本期不设置 25k 删除量验收。完成后以 `git diff --numstat` 对同一基准去重统计实现、测试、脚本、文档，以及必要兼容适配的新增行。

## 4. 混合文件的硬边界

### 4.1 Gate 剩余 helper 原地保留

`engine/finalize_evidence_gate.py` 内的 `is_repro_script_path` 仍被 Agent 的 `_workspace_edit_gate_external_scratch_repro_target` 调用，关系到普通 scratch 写入路径。

保留现有 `re` import、`_SCRIPT_EXTENSION_RE` 和该函数在原文件，删除其余 gate 代码。本期不重命名模块，不搬函数，不写替代算法。

保留 `test_agent_llm_budget.py` 中 configured scratch、prefix collision、symlink escape 等回归用例，以及 `test_apply_patch_gates.py`、现有 write policy 测试。

### 4.2 Salvage 的 Git 与撤销依赖保留

保留 `tools/source_diff_candidates.py` 和 mutation receipts 内的捕获调用。保留 `tools/source_diff_preservation.py` 的主干、共享解析器和 `mark_source_diff_candidates_lost` 调用；它是源码中该标记的唯一生产者。

删除 freeze 专属部分时，不删除共享 Git parser、run_git、patch classifier、ToolContext 或 workspace mutation helpers。

保留 Agent 的 `_workspace_diff_paths_for_final_diff_contract` 和 `_workspace_internal_diagnostic_paths`。名字含 contract 不构成删除理由：salvage 仍使用它们。只删除已退出 ledger 对应的那个过滤项。

本期保留 `final_diff_salvage_veto=False` 的现有默认值，不把保护规则永久开启；不能以删除开关的方式偷偷改变行为。

### 4.3 Provider recovery 按路径保留

`runtime_recovery.py` 的 replay capability、reasoning prefill、DashScope continuation、共用 decision 类型保留。只删除与 D06 source-loop 观察绑定的状态、文案和 helper。

`reasoning_only_act_now` 暂留整项。当前 Agent 的两处恢复判断使用 `not thinking_enabled or config.reasoning_only_act_now`，开关关闭时也有自动恢复；本期不拆出新策略。

Post-tool empty recovery 暂留，以免把 Provider malformed/empty 恢复与模型进度提示一并清掉。D09 只退出独立的 text-only-tool steering。

### 4.4 测试不按文件名粗删

- `test_toolcomp_matcher_levers.py` 同时验证 strict、CD 和 FAILURE_PRESERVE，只删 CD 部分。
- `test_reasoning_retry_and_deadline_thinking_levers.py`、`test_endgame_directive_and_cap_levers.py`、`test_final_diff_salvage_and_endgame_freeze_levers.py`、`test_tool_surface_levers.py` 按存活契约剥离，不能整删。
- `test_runtime_diagnostics.py` 虽以实验诊断命名，含普通 Chat 无 Git 时能完成等测试；保留这些用例和必要 fixture，在原文件内只删除观察器／source-loop 专属测试。因此该 767 行文件未列入整文件计数。
- `test_git_patch_hygiene.py` 验证默认工具输出中的 warning，和 D02 的 hard-block 不同，完整保留。
- `test_request_proof_levers.py`、write-deny tests、mutation receipts tests、provider goldens 均不因名字含 lever 而删除。
- 保留 `router_runtime_diagnostics.py` 和其测试；它们服务路由、health、doctor，非 D06 对象。

### 4.5 ToolContext 和 AgentConfig 的构造兼容

下面五个 ToolContext 字段必须保留原名称、原位置、原类型和原默认值，仅将注释标为 deprecated/unused：

| 字段 | 原默认值 | 本期处理 |
|---|---|---|
| `endgame_git_freeze_active` | `False` | 保留槽位，删除 freeze 业务消费者和赋值 |
| `tool_description_overrides` | `None` | 保留槽位，不读取旧 override 文件、不重写工具描述 |
| `tool_description_overrides_source` | `None` | 保留槽位，不再传播为运行配置 |
| `endgame_git_freeze_instrumentation_exempt` | `False` | 保留槽位，删除 freeze exemption 扩展 |
| `scratch_verify_mirror_active` | `False` | 保留槽位，删除 mirror 业务和 guidance |

现有 `tests/test_tools/test_tool_upgrade_compatibility.py` 固定了位置序列，必须纳入必跑清单并保持断言。不能修改预期序号来掩盖 sandbox、collaboration、plan、artifact 字段前移。

`AgentConfig` 也通过 `opensquilla.engine` 公开导出。对于本次退役的已发布字段，保留原位置的 inert 字段声明，以免旧关键字构造触发 TypeError 或位置构造错绑；删除读取这些字段的业务分支和内部显式传播。不要为清理几十行声明新增通用兼容构造器或 kwargs 吞参数层。其他有公开构造契约的 dataclass 同样检查。

这些槽位不列入本期删除行数，不进入新的策略或模型提示，也不要求老用户手动清理才能运行。后续架构阶段再审查其长期接口归属。

## 5. 配置、工具与事件的非阻断退役

本节取代 v1 的“旧机制显式开启时报错”。用户要求版本升级不中断，因此关闭和开启的已知旧输入都能加载；功能退出通过自动归一化和一次性非阻断诊断说明。

现有事实：Gateway 顶层 schema 严格校验；PromptConfig 部分旧字段可能被静默忽略；`config_migration.py` 已有 disk-load 迁移、removed_fields、warnings 数据、备份和原子写入，但尚未覆盖本清单所有名字。env 没有统一退休检查器。使用现有边界做有限适配，不新增配置框架。

### 5.1 各表面的处置

| 退役表面 | 处理 | 明确禁止 |
|---|---|---|
| 旧 TOML／持久化配置 | 在现有预验证迁移中移除已知退役 key，记录变更；复用已有备份与只读加载模式；旧开启值给一次非阻断说明 | 不因旧值为 on/true 抛退役异常；不放宽全部未知字段检查 |
| 旧 env | 识别已知退役名字后不再驱动运行；开启值聚合提示一次；不重新读取其指向的旧文件或依赖 | 不让不存在的 override 文件、旧路径或旧枚举值阻止启动；不修改用户 shell/env 文件 |
| AgentConfig／ToolContext 等已发布参数 | 保留有限 inert 声明以接受旧构造；删除业务消费者与显式传播 | 不造成 TypeError、位置错绑；不引入动态 kwargs 兼容框架 |
| builtin 工具名 `submit` | 删除真实注册、schema 和 review 拦截；规范化已知旧配置里的 builtin 引用并提示 | 不提供假成功或 no-op submit，不误删 shell process 的 submit action |
| `allowed_tools`／`allow`／`also_allow`／旧 Profile | 移除已退役 builtin 的精确引用；其余工具、Profile、selectors 与权限语义保留 | 不能把清理后的空 allowlist 转为 None/full，也不能把整个 policy block 丢掉 |
| 历史 submit tool-use/result | 保留历史配对和内容；恢复会话正常加载；再次调用走现有 registry-miss 工具结果 | 不删除半条 transcript、不把旧 submit 改写为别的工具执行 |
| runtime events | 删除实验生产者与仓内专属消费者；逐项登记退出的 event 名称 | 不保留昂贵观察器只为兼容旧事件；不伪造空事件补流 |
| Child／Meta／codetask／recovery | 删除退役字段的显式传递、构造赋值及 env 透传；保留有用的恢复和安全数据 | 不误删整个环境隔离列表或恢复逻辑；不把父任务旧 env 继续传给新任务激活功能 |

工具列表必须保持权限收窄：若原来显式只允许 `submit`，退役后应该正常启动、提示“已退役工具导致可用集合为空”，并保持不允许任何工具；不能擅自给它 exec_command 等其他工具。`also_allow` 是追加权限，删除其中 submit 不应改变原 Profile 的正常权限。

仅针对已退役 builtin 的配置引用处理，不能全局替换所有名为 submit 的字符串或阻止合法第三方同名工具。

### 5.2 诊断必须非阻断

- 使用现有 logger／配置迁移结果聚合一次，不逐轮、不逐 Child 提示，不插入模型上下文、不弹阻断对话框。
- 只记录字段／工具名和退役处置，不打印环境变量值、工具描述正文等可能包含敏感内容的值。
- 不新增 `warnings.warn` 路径；验收 `PYTHONWARNINGS=error` 时不会把兼容提示变成异常。
- 日志目标不可写、只读配置等情况按现有可用路径降级，不因为写退役提示而导致启动失败。
- 新版本 UI／配置输出不继续提供退役能力的开启入口；必要构造槽位可以保留，但不重新生成业务 wiring。

### 5.3 退役登记表与测试

PR 2 每个删除提交必须附上精确登记项：原 TOML 路径、env 名称、公开参数、ToolContext 兼容槽、工具／Profile 引用、runtime event 名称、Child/Meta 构造点、codetask/recovery 透传点以及对应处置。不能只给一个 env 列表。

当前已确认需列入的点包括五个 ToolContext 槽位、builtin `submit`、`final_diff_contract.observed`，以及 codetask/recovery 中的 `OPENSQUILLA_PATCH_EVIDENCE_LEDGER_PATH`。其余事件以删除提交中的真实生产者逐项登记；清单不要求保留生产能力。

至少覆盖：

1. 旧字段/env 未设置、显式关闭：正常启动，不恢复旧机制。
2. 旧字段/env 显式开启：正常启动，一次非阻断说明；旧机制没有执行。
3. 旧 TOML 含 submit，或 `allowed_tools={"submit"}`、`also_allow=["submit"]`：正常启动，明确说明退役，权限不扩大。
4. 旧位置构造与旧关键字构造：不报 TypeError、不静默错绑。
5. 恢复带历史 submit 的会话：历史合法；工具不可用结果不导致 Runtime 崩溃。
6. `runtime_events_path` 已配置：正常启动和完成，旧实验事件不再产生，通用事件仍可用。
7. Child/Meta/codetask/recovery：不残留启用传播，正常任务仍能运行。
8. `PYTHONWARNINGS=error`、只读配置、诊断日志不可写：退役适配不新增启动错误。
9. 其他未知 key、真实权限拒绝和无关错误：保持原有校验，不把“升级兼容”变成吞掉所有错误。

兼容是接住旧输入的有限成本，不等于继续维护旧实验实现、测试矩阵或事件序列。其实现仍须在删除 PR 中单独列出，确保没有带入新的 Agent 功能。

## 6. 两个删除 PR

### PR 1：产品仓实验控制面退出

范围：D01。

实施前记录 ledger commit、engine commit、run manifests、command、prompt、tools、dataset/image hashes 及原脚本可获取位置；缺失的资产先按原样归档，不重写 runner、不自动启动付费实验。历史 Git commit 可读不等于已验证所有外部数据和镜像仍可用，复现资产状态如实记录。

删除 21 个文件、7,440 行及关联 docs/CI 引用。交付包含文件映射和取回说明。

验收：产品 CLI、正常导入和配置不依赖 `scripts/experiments`；文档入口无悬挂链接；CI 没有继续调用已删脚本／测试；保留独立 live provider harness。

### PR 2：Runtime 实验机制退出

范围：D02–D10，使用数个可分别回滚的提交，顺序如下：

1. 删除 gate、submit、patch evidence、mirror/hygiene 实验闭环，留下共享 helper。
2. 删除 watchdog、post-write、capsule、final-diff contract、coding diagnostics 与 source-loop steering。
3. 删除 deadline/freeze/ARM-EG 的可独立部分和 placeholder/repeat/reminder 等提示分支。
4. 删除 description override、dedup、signal hints、text-only steering 和 CD unwrap 局部分支。
5. 同步清除每批的 import、构造和 Child 传播、模板、专属测试、CI 清单；配置退役处理与对应删除一起提交，不能先合入会导致旧配置无法启动的中间状态。

本期不给现有 Agent 新增默认行为，也不创建替代状态机。回归时定位并回退对应删除提交，不通过新提示／新策略补分。

## 7. 多入口影响与验收

| 入口／场景 | 预期变化 | 必须验证 |
|---|---|---|
| CLI chat / agent / codetask | 默认成功回合模型内容及正常工具面预期不变 | AgentConfig 构造、事件完成／失败、工具权限及 codetask 路径 |
| WebUI / Gateway / Desktop 后端 | 通过共享 Runtime 获得相同删除；不改 UI 架构 | RPC 配置、prompt assembly、stream terminal、history/tool pair |
| standalone TUI | 预期不新增行为或提示 | 原生 runtime adapter、turn stream、工具结果与结束事件 |
| Channel | 不新增工具权限或 override | owner/non-owner、allow/deny、工具召回 session scope、异步完成 |
| Child Agent | 删除对应实验字段复制 | 保留 Provider、salvage、安全和预算继承；避免漏字段导致构造报错 |
| Meta Agent | 不继承新默认；保留原恢复和工具策略 | 默认 AgentConfig、meta surface、子任务暂停／完成、权限 |
| Cron / Heartbeat | 默认不再运行已退出的隐式实验观察；仍受正常预算和取消控制 | 新 turn bootstrap、provenance、最终结果与 delivery |
| Git coding | 减少部分观察器、扫描、指纹和挑战 | salvage 候选与 lost/veto 路径仍有效；Git 基础、用户写入和工具退出状态不变 |
| 无 Git、Git 不可用 | 不应因清理新增错误 | 普通 Chat 能结束，文件工具仍正常，现有 Git unknown/not-repo 语义保持 |
| 显式实验用户 | 已删机制不再提供，旧 rich recipe 不再等价 | 升级启动不中断，一次非阻断退役说明；实验报告不得标成原 recipe；历史 runner 与新 main 区分 |
| KV cache / token | 普通默认稳定 prefix 预期不变；实验启用场景不再等价 | 用既有 request goldens／捕获请求比对；不得直接承诺 cache hit 或分数提高 |

特别说明：保留 salvage 的现有依赖意味着 candidate/preservation 的默认 log 路径及部分 Git 查询继续存在。不能承诺删除后所有候选 patch、后台 Git 操作和实验字段全部消失。

## 8. 验证顺序

本次只规划，以下测试尚未运行。

### 8.1 环境 preflight

遵守用户 AGENTS.md：先确认物理 cwd、Git root/基准、解释器、源码 import path、依赖与实际 build artifacts，再运行代表用例。当前 `.codex/worktrees` 路径不适合全套正式验收；`/tmp` 同样不是自动合格替代目录。正式全套应在获得授权的普通项目路径或与 CI 相符的环境执行。

当前构建链是 WebUI Vite 输出 `opensquilla-webui/dist`，验证／stage 脚本再写入 `src/opensquilla/gateway/static/dist`；实施时重新核对配置与实际文件，不能只复制 tracked source 后假定资产存在。

必须先读并运行当前等价文件：

```sh
PYTHONPATH="$PWD/src:$PWD" uv run --frozen --no-sync pytest -q \
  tests/test_sandbox/test_trusted_sandbox_execution.py \
  tests/test_tools/test_approval_unification.py \
  tests/test_live_multi_provider_matrix.py \
  tests/test_live_provider_profile_smoke.py
```

macOS 按最终验收相同权限补充当前相关 Seatbelt/network probe。付费 live Provider 和 SWE batch 不因编写本方案而自动启动。

### 8.2 每批最小验证

- `git diff --check`，对存活变更文件执行项目现有 lint。
- 搜索被删符号的 import、注册、构造、模板、env 和 CI 引用；允许保留清单内的兼容入口和共享 helper。
- 保留能力的测试断言不删弱；删除失效实验用例，调整构造参数及明确因删除而过期的断言。
- 在未修改 baseline 上运行相同代表用例再比对，不能未经对照将失败称为既存问题。

### 8.3 核心回归

```sh
PYTHONPATH="$PWD/src:$PWD" uv run --frozen --no-sync pytest -q \
  tests/test_engine/turn_runner \
  tests/test_engine/test_agent_llm_budget.py \
  tests/test_engine/test_agent_retry_budget.py \
  tests/test_engine/test_agent_terminal_outcomes.py \
  tests/test_engine/test_agent_transactional_tool_publication.py \
  tests/test_engine/test_provider_context_block_feedback.py \
  tests/test_engine/test_tokenjuice_tool_result_projection.py \
  tests/test_engine/test_tool_result_store.py \
  tests/test_engine/test_runtime_tool_result_retrieval_surfacing.py \
  tests/test_tools/test_tool_result_retrieval.py \
  tests/test_tools/test_gitless_write_tracking.py \
  tests/test_tools/test_source_diff_candidates.py \
  tests/test_tools/test_source_diff_preservation.py \
  tests/test_tools/test_mutation_receipts.py \
  tests/test_tools/test_tool_upgrade_compatibility.py \
  tests/test_tools/test_git_patch_hygiene.py \
  tests/test_toolcomp_matcher_safety.py \
  tests/test_toolcomp_matcher_levers.py \
  tests/test_request_proof_levers.py \
  tests/test_provider/test_request_goldens.py \
  tests/test_provider/test_stream_goldens.py
```

另跑保留了 salvage/Provider 用例的混合测试文件。命令中的文件以实施基准存在为准，不给已删除实验文件添加无意义空壳测试。

### 8.4 配置与入口回归

```sh
PYTHONPATH="$PWD/src:$PWD" uv run --frozen --no-sync pytest -q \
  tests/test_migration/test_legacy_config_fixtures.py \
  tests/test_gateway/test_config_version.py \
  tests/test_gateway/test_config_persistence_boundary.py \
  tests/test_contracts/test_config_public_dict.py \
  tests/test_contracts/test_config_effective_wire.py \
  tests/test_cli/test_agent_cmd.py \
  tests/test_cli/test_agent_event_stream.py \
  tests/test_cli/test_chat_cmd.py \
  tests/test_contrib/test_codetask/test_codetask_agent_config.py \
  tests/unit/cli/tui/test_native_chat_runtime.py \
  tests/unit/cli/tui/test_runtime_adapters.py \
  tests/test_gateway/test_channel_turn_ingress.py \
  tests/test_engine/test_subagent_run_mode_inheritance.py \
  tests/test_engine/test_subagent_execution_target.py \
  tests/test_engine/test_runtime_meta_invoke_surfacing.py \
  tests/test_engine/turn_runner/test_cron_provenance.py \
  tests/test_scheduler/test_heartbeat_service.py
```

随后按当前项目 CI 要求执行完整离线检查及 Linux/macOS/Windows 相关矩阵。不得在一套验证环境并发重复全套；换环境或失败后，先做针对性诊断及 preflight 再重跑。保留准确失败 ID、诊断日志和真实退出码。

### 8.5 SWE 验证口径

历史最高分通过原 pinned engine/config/assets 保留。代码删除 PR 不承诺在新 main 原样支持旧 rich recipe，也不把历史 59/63 作为当前 main 的自动验收分数。

新 main 的 GLM/Qwen 若安排评测，先固定剩余可用配置及预算，记录删除前后的配置差异。在同一比较口径上分析；不得为补分加入新 steering 或改默认。是否扩大到付费全量实验按当时授权与成本安排执行。

## 9. 精确整文件候选清单

下列路径均相对产品仓根目录。行数来自 `git show f0981d61ceb8d43007030988487ca1eb20628a81:<path>` 的物理行数。共 42 文件、16,156 行。实施时再做引用检查，出现新保留消费者则从清单撤下并更正计数。

```text
63   docs/experiments.md
361  scripts/experiments/analyze_dashscope_payload_parity.py
198  scripts/experiments/analyze_dashscope_payload_risk.py
1123 scripts/experiments/analyze_tool_compression.py
129  scripts/experiments/check_docker_image_lock.py
251  scripts/experiments/check_treatment_delivery.py
416  scripts/experiments/exp_common.py
684  scripts/experiments/exp_finalize.py
292  scripts/experiments/exp_init.py
261  scripts/experiments/exp_quarantine.py
193  scripts/experiments/exp_run.py
211  scripts/experiments/exp_status.py
331  scripts/experiments/replay_finalize_gate.py
165  tests/test_scripts/test_analyze_dashscope_payload_parity.py
105  tests/test_scripts/test_analyze_dashscope_payload_risk.py
675  tests/test_scripts/test_analyze_tool_compression.py
101  tests/test_scripts/test_check_docker_image_lock.py
196  tests/test_scripts/test_check_treatment_delivery.py
957  tests/test_scripts/test_exp_ledger.py
416  tests/test_scripts/test_exp_quarantine.py
312  tests/test_scripts/test_replay_finalize_gate.py
265  src/opensquilla/engine/submit_review.py
44   src/opensquilla/tools/builtin/submit_tool.py
253  tests/test_engine/test_submit_review.py
535  tests/test_engine/test_agent_submit_review.py
1171 tests/test_engine/test_finalize_evidence_gate.py
781  tests/test_engine/test_agent_finalize_evidence_gate.py
606  tests/test_engine/test_agent_patch_hygiene_block.py
766  tests/test_engine/test_agent_verify_mirror_and_variant_challenge.py
139  src/opensquilla/engine/post_write_convergence.py
181  tests/test_engine/test_post_write_convergence.py
179  src/opensquilla/engine/runtime_state_capsule.py
208  tests/test_engine/test_runtime_state_capsule.py
123  src/opensquilla/tools/description_overrides.py
549  tests/test_tools/test_description_overrides.py
359  src/opensquilla/engine/patch_evidence_ledger.py
174  tests/test_engine/test_patch_evidence_ledger.py
506  src/opensquilla/engine/progress_watchdog.py
410  tests/test_engine/test_progress_watchdog.py
463  src/opensquilla/engine/final_diff_contract.py
391  tests/test_engine/test_final_diff_contract.py
613  src/opensquilla/engine/runtime_diagnostics.py
```

## 10. 证据与最终验收

Ledger 审计快照：`1942bb33c0041a0e910b8f229089ff2d6f216dd3`。

- [总交接与默认迁移规则](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/MASTER-BENCHMARK-AND-DEFAULT-MIGRATION-HANDOFF.md)。
- [GLM G59 四轴组合](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/opensquilla-swe-adoptable/runs/20260711-lite80-glm51-armr1lb-toolcompfix-pubmain-w10/decision.md)、[同配置确认 56](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/opensquilla-swe-adoptable/runs/20260712-lite80-glm51-armr1lbc-toolcompconfirm-pubmain-w10/decision.md)。组合证据不能逐项解释为独立增益。
- [GLM gate 56→58、成本未过](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/opensquilla-swe-adoptable/runs/20260710-lite80-glm51-run2-armpg-finalizegate-pubmain-w10/decision.md)、[确认轮 56](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/opensquilla-swe-adoptable/runs/20260711-lite80-glm51-run2-armpg-finalizegate-pubmain-w10/decision.md)。有正向线索，未稳定确认。
- [Salvage 直接归因案例](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/opensquilla-swe-adoptable/runs/20260715-lite80-qwen36-rebaseline-tsstack-aa2-w10/decision.md)。证明存在救回案例，不能推导总体净收益。
- [ARM-EG 低功效结果](https://github.com/Open-Squilla/swe-experiment-ledger/blob/1942bb33c0041a0e910b8f229089ff2d6f216dd3/opensquilla-swe-adoptable/runs/20260714-lite80-glm51-armeg-endgame-pubmain-w10/decision.md)。不能把 60/80 自动解释为整包能力获证。

本期完成条件：D01–D10 的指定机制退出；保留依赖及默认模型可见行为通过回归；已知旧实验输入不会因退役导致升级报错；ToolContext/AgentConfig 构造兼容成立；默认内部观察、Git 查询和实验事件的减少明确披露；无遗漏的运行调用／CI 引用；无新 Agent 能力、无架构替代方案、无以补分为目的的新默认。交付实际净删除统计、完整退役表、兼容槽清单、验证结果及准确的未验证项。

## 11. 首批 D01 实施记录（2026-09-09）

### 范围与不可变来源

- 产品分支：`codex/remove-experiment-control-plane`。
- 实际删除基准：`bbd0c429e106babeeec70a6c6d493b563696d576`。D01 的 21 个文件与原审计基准 `f0981d61` 完全一致；采用最新 main 的 Windows 分片记录，不覆盖其间的其他修改。
- Ledger 归档提交：[`4875258fa5dde48498da61e142482cdc63870a88`](https://github.com/Open-Squilla/swe-experiment-ledger/commit/4875258fa5dde48498da61e142482cdc63870a88)。 初始归档为 `d3b2173185d25430a6ba66ce1631af9aaeff291f`；后续提交只纠正 engine 来源仓库 URL 为已验证的 `TokenRhythm/opensquilla`，不改任何归档原文件或测试结果。
- [冻结工具与运行说明](https://github.com/Open-Squilla/swe-experiment-ledger/tree/4875258fa5dde48498da61e142482cdc63870a88/archives/opensquilla-experiment-control-plane/bbd0c429e106babeeec70a6c6d493b563696d576)；[逐文件 manifest](https://github.com/Open-Squilla/swe-experiment-ledger/blob/4875258fa5dde48498da61e142482cdc63870a88/archives/opensquilla-experiment-control-plane/bbd0c429e106babeeec70a6c6d493b563696d576/manifest.json) 记录原路径、mode、blob ID、SHA-256、行数、依赖和实际测试结果。
- 删除前已核对初始归档 `d3b2173185d25430a6ba66ce1631af9aaeff291f` 的远端分支 SHA，并通过 contents API 取回 manifest blob `15ca31d8014ca639aad1206c109198d766fa7170`；来源 URL 纠正后，又从固定提交 `4875258fa5dde48498da61e142482cdc63870a88` 取回并核对新版 manifest blob `08ccad7b5674b6049d4603e8339bff1c731423e9`。

D01 只移出 12 个脚本（4,450 行）、8 个专属测试（2,927 行）和原文档（63 行），整文件合计 7,440 行。八个测试为三个 `test_analyze_*`、两个 `test_check_*`，以及 `test_exp_ledger.py`、`test_exp_quarantine.py`、`test_replay_finalize_gate.py`；精确名单以 manifest 为准。连同 26 行引用清理，产品分支共删除 7,466 行；新增本方案文档 415 行，净减少 7,051 行，未新增运行时代码。

其余修改限定为文档目录、gate 实现和 gate 测试的过时 module docstring、Windows assignments/durations 的八个对应记录。不会改运行逻辑、默认配置、工具名、参数槽位、env 消费者、安全策略或其他测试断言。Windows 的既有 overrides、guardrails 和 source_runs 历史证据保持不变。

### 验证环境与结果

在独立临时 checkout 中执行经过路径审查的 D01 定向测试，未运行本地全套。原任务工作树及其未提交方案文档保持不变。冻结 engine 和产品 checkout 分离；使用冻结 engine 的 Python 环境并显式设置产品 `PYTHONPATH`，已核对实际 import 路径。

- Python 3.12.13、pytest 9.0.3、pytest-xdist 3.8.0、Node 22.23.1。
- 依赖来自冻结 engine 的 `uv sync --frozen --extra dev --extra recommended --extra mcp`；`uv.lock` SHA-256 为 `aeedeb3b5598d390aa5620999cf6431f907980f819102116ad3c2791754a0b3c`。
- 归档原文件：21/21 内容、mode、Git blob ID、SHA-256 校验通过。
- 归档测试：原始源码 68 passed；归档 preflight 55 passed；归档完整测试 68 passed；11 个 CLI `--help` 通过。
- 产品基线 preflight：27 passed。首次定向基线为 572 passed、3 failed、2 skipped；三个失败均为沙箱拒绝 `ps`，准确失败记录已保留。
- 提升进程查询权限后，原失败的两个完整文件先通过 35 项 preflight，再运行完整定向基线：575 passed、2 skipped（107.93 秒）。未改测试、mock、skip 或产品代码。
- 删除后验证：preflight 152 passed；完整定向测试 507 passed、2 skipped（111.85 秒）。JUnit 对比确认只少了迁往 ledger 的 68 个用例，所有存活用例结果与基线一致。独立审查确认两个 Python 文件除 module docstring 外 AST 完全一致、其他 Runtime 源码未变、Windows JSON 仅删除八个对应记录；lint、diff check、shard report 和活动引用检查均通过。

两项既有跳过为 `test_live_xdist_worker_uses_isolated_runtime_roots` 的外层 xdist 条件，以及 `test_dockerignore_filters_real_build_context` 的 `OPENSQUILLA_DOCKERIGNORE_E2E` gate。它们不计为已验证；正常 PR CI 门禁保持原样。WebUI 脚本测试使用临时自造资产，不需要真实 WebUI build。没有启动真实 SWE、Provider 请求或历史实验命令。

定向基线与删除后使用相同命令、源码定位方式和权限：

```sh
D01_ENGINE=/absolute/path/to/pinned/engine
D01_PRODUCT=/absolute/path/to/product/checkout
cd "$D01_PRODUCT"
PYTHONPATH="$D01_PRODUCT/src:$D01_PRODUCT" "$D01_ENGINE/.venv/bin/python" -m pytest \
  -q --tb=short -r a \
  tests/test_ci/test_plan_ci.py \
  tests/test_ci/test_windows_test_shards.py \
  tests/test_ci/test_windows_duration_governance.py \
  tests/test_packaging/test_pyproject_invariants.py \
  tests/test_ci/test_dockerignore_context.py \
  tests/test_scripts tests/test_engine/test_finalize_evidence_gate.py
```

同时执行 `git diff --check`、存活 Python 修改的 lint、Windows shard report 和引用清理审计。合并前必须检查实际 PR CI；本地定向通过不代替 CI。

### 首批影响与回滚

所有正常入口（CLI/chat/codetask、WebUI/Gateway/Desktop、TUI、Channel、Child/Meta、Cron/Heartbeat）的运行逻辑、配置及默认行为不变。已有实验 env 即使开启也继续有效，因为 D01 没有删除消费者。Tokenjuice、Provider、Store/retrieval、compaction、salvage、Git 支持和安全策略不动。

产品 wheel/Docker 原本不携带这 21 个文件，sdist 源码集合会有意减少；module docstring 变化不应描述为分发文件字节完全相同。本批 Runtime 实现删除量为 0，也不减少运行时 Git 查询、模型 token 或内部事件。

实验维护者改用冻结 ledger 归档及对应 engine。归档不是历史 Champion engine，不证明外部镜像、数据集或 evaluator 仍可取回。回滚产品删除提交即可恢复脚本、测试与引用；归档保留，无配置或数据迁移需要撤销。
