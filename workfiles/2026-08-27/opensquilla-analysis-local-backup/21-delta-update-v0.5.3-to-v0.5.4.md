# 21 · 增量体检：v0.5.3 → v0.5.4

> 体检日期：2026-08-26 | 基线：`4e48f9b56`（16 号文档基线，v0.5.3）→ HEAD：`a0bbe0235`
> 方法：恢复模式增量复检。merge-base 实测 = 基线本身（基线是 HEAD 直接祖先），增量范围干净无交叉。
> 本文档为 16 号之后的第二个增量勘误层；**先读本文再读 17/18/19 号**（锚点行号以 §5 漂移表修正为准）。

---

## 0. 结论速览

| 维度 | 判定 |
|------|------|
| 版本 | `pyproject.toml:3` → `"0.5.4"`（repo_verified）；tag v0.5.4=`3877f9668` 为 HEAD 祖先 |
| 增量规模 | **47 提交**（1 merge）；后端 src 变更 **86 文件 +13036/−2929 行**（repo_verified，git diff --stat） |
| 提交构成 | 上游 ~41 + 本地 6（QinLuza：2 个 engine waterline 特性、1 个 storage busy 修复、1 个配置注释修剪、1 个分析文档入库、1 个 merge） |
| S2 结构 | 业务模块目录 36→36 零增删（与 16 号"35"的差异为统计口径，见 §6 注） |
| 最高价值新机制 | ① context waterline 治理双件套（本地）② artifact 自主 HTML 编辑闭环（上游 #1359）③ 每会话路由策略（上游）④ runtime_packs 不可变目录固化 |
| 既有结论影响 | 18 号维持；17 号微漂移；19 号**强化且锚点漂移**（§5/§7） |
| 新增漂移信号 | D10：catalog.py 文档字符串引用不存在的 `runtime_packs.sources` 模块（P3，repo_verified） |

---

## 1. 增量范围界定

```
git merge-base 4e48f9b56 HEAD  =  4e48f9b56          ← 基线即分叉点，无交叉历史
git rev-list --count 4e48f9b56..HEAD  =  47（含 1 merge）
git merge-base --is-ancestor v0.5.4 HEAD  =  yes     ← v0.5.4(3877f9668) 已包含于 HEAD
```

时间跨度：2026-08-21 ~ 2026-08-26（6 天，日均 ~8 提交，与 16 号记录的"164 commits/月漂移"节奏一致）。

> **※ 参考点澄清（差量核对后补注）**：`4e48f9b56` 并非 v0.5.3 tag（=`38d1cf46f`，2026-08-13）——它是已同步大部分 v0.5.4 前上游内容的本地基线（v0.5.3 是其祖先，merge-base 亲证）。因此本文"47 提交增量"**窄于** tag 区间 v0.5.3..v0.5.4；gateway/provider 大文件在 tag 口径下的更大差量（如 rpc_sessions ±2633、ensemble ±3575）主要发生在 16 号分析时的树内，已被 16-20 号文档覆盖。另 HEAD 领先 v0.5.4 tag **26 提交**（rev-list 亲证）：6 个 QinLuza 本地提交 + 合并带入的 tag 后上游演进。

---

## 2. 提交分类

### 2.1 本地提交（QinLuza，6 个）

| 提交 | 类型 | 内容 | 触达 |
|------|------|------|------|
| `77a67d0a1` | feat(engine) | **长会话 advisory context waterline 告警** | runtime.py +85, gateway/config.py +5, 测试 +188 |
| `36b0c4169` | feat(engine) | **按水位线投影超大的历史工具结果** | agent.py +137, types.py +5, 测试 +255 |
| `24cf2b3d9` | fix(storage) | **交互式 SQLite busy 预算 2s→10s** | storage.py ±1 行 |
| `3a04fdbf9` | docs(config) | 修剪 `context_waterline_alert_ratio` 注释残句 | 配置注释 |
| `843cdb7ae` | docs(analysis) | 本分析档案入库（17-20 号 + workfiles 归档） | opensquilla-analysis/ |
| `a0bbe0235` | merge | 合并 upstream/main | — |

### 2.2 上游提交（~41 个，v0.5.3→v0.5.4）

主题分组（依据提交标题 + dirstat + 抽样亲证）：

| 主题簇 | 代表提交 | 分析权重 |
|--------|----------|----------|
| **artifact 自主 HTML 编辑闭环** | `5d5c420e8` #1359 及后续修复 | 高（§4-N2） |
| 每会话路由策略 / 路由面板快照 | `806d63b93` #1386、`fc2127ada` #1384、`f8bc9e132` #1389 | 高（§4-N3） |
| Runtime Pack 下载定型 | `5fdfa579f` #1340、`6c741e2bc` #1365、`f5a2c7c57` #1367 | 中高（§4-N4） |
| **不可达代码清理潮** | `f02662d54` #1382、`739aa49d2` #1385 | 中高（§4-N5） |
| 回合终止/活动时间线治理 | `02aa214e0` #1343、terminal_activity 扩容 | 中（§3） |
| 桌面启动/进程树/打包验证 | `72e7a9b76` #1355、`2ad0f1ea2` #1336、`d19dc14a3` #1330 等 | 低中（平台健壮性） |
| first-send 打包测试基建 ×3 | #1399/#1401/#1402 | 低（仅测试脚本，repo_verified：全部落在 `desktop/electron/scripts/test-packaged-first-send-renderer.mjs` + `tests/test_release_consistency.py`，非运行时源码） |
| CI 整固 ×4 | #1403/#1404/#1405 等 | 低 |
| 发布准备 | `5f205c44c` #1396 | — |

---

## 3. 后端源码增量地图（src/ 变更热点）

86 文件 +13036/−2929 行。按净变化排序的热点：

| 文件 | ±行数 | 定性 |
|------|-------|------|
| `artifact_session/mutation_attempts.py` | +1164 | 编辑尝试状态机扩容（→N2） |
| `engine/agent.py` | ±1239 | **巨型文件继续膨胀**（§6）；含 waterline 投影新方法（→N1） |
| `tools/builtin/document_browser.py` | +1198 | **全新工具**（→N2） |
| `gateway/terminal_activity.py` | +998 | 有界非敏感终端活动快照：v1 用量屏障 provider 相位证明 + v2 终末回合紧凑呈现轨迹，只存顺序与引用（头注 :1-7 亲读） |
| `sandbox/upgrade_migration.py` | +773 | 遗留沙箱状态幂等直迁（Windows ACL 等，#1330） |
| `tools/dispatch.py` | +729 | 工具分发路由扩容（→N2） |
| `gateway/desktop_artifact_bridge.py` | +653 | Electron 编辑四视图桥接（→N2） |
| `artifact_session/repository.py` | +1075 | 持久层 CAS/对账扩容（基线已存在，见 ※B1） |
| `gateway/artifact_mutation_recovery.py` | +453 | 变更恢复扩容 |
| `tools/builtin/artifact_editing.py` | +459 | 编辑工具扩容 |
| `gateway/rpc_sessions.py` | +398 | 会话 RPC 扩容（§6 异常信号） |
| `engine/history.py` | +345 | 历史装配调整 |
| `engine/runtime.py` | ±1082 | 管线组装区扩展（→§5 锚点漂移主因） |
| `provider/openai.py` | −303 | 大幅裁剪 |
| `memory/session_flush.py` | −276 | 死代码清理（→N5） |

> ※B1 差量核对修正（2026-08-26）：repository/service/artifact_preview/mutation_attempts 均为既有文件扩容而非新建——git cat-file 裁决基线 blob 分别为 173,437/51,201/35,352/15,520 B；本 delta 全新文件仅 document_browser.py（+1198 恰为其全文行数）。

删除清单（整文件）：见 §4-N5。

---

## 4. 新机制考察

### N1 【本地】context waterline 治理双件套（亲读，repo_verified）

#### N1-a 历史工具结果水位线投影

- **动机**（agent.py:5485-5498 docstring）：既有的绝对/聚合两道压缩只在请求级预算压力出现时才动作，而 `_remember_provider_visible_tool_results` 又会冻结已全文投递过的块——长会话因此单调膨胀，直到最大路由窗口也装不下请求。docstring 直接引用事故号 `h8m7rtg1 / 7bf7fefe`。
- **机制**（agent.py:5481 `_compact_history_tool_results_for_provider`）：
  - 触发条件：历史中已完成 assistant 回合数 > `tool_result_history_projection_keep_recent_turns`（types.py:806 新增，默认 **3**，0 关闭）；
  - 保护地板：`protected_floor = assistant_indexes[-keep_turns]`（:5514），近 N 回合完全不动；
  - 投影对象：成功、非 artifact、超过单结果 provider 上限的工具结果 → 头尾预览 + 恢复句柄（`_tool_result_projection_for_provider`，预览上限 `min(result_cap, 4000)` 字符 :5558）；
  - 豁免：待审批 payload（:5542）、含 artifact 结果（:5544）、已投影内容（:5540）；
  - **关键设计决策**（:5547-5552 注释原文语义）：read_file/git_diff 的"语义保护"是推理质量偏好而非生存保障——越过水位线后超大受保护块照样投影；
  - 不变量：原始转录历史永不变异（仅请求视图投影），可经投影句柄恢复；
  - 遥测：`config.metadata["tool_projection_applied"/"tool_projection_calls"]`（:5475-5478）。
- **调用点**：agent.py:20022（provider 请求装配路径）。

#### N1-b advisory 水位线告警

- 配置：`context_waterline_alert_ratio: float = Field(default=0.70, gt=0.0, le=1.0)`（gateway/config.py:2699）。
- 运行时：runtime.py:13156 比例读取 → :13168 `_emit_context_waterline_alert` → :12087 发射点。每会话一次性（去重集合 `_context_waterline_alerted_sessions` @4631；回落后重置允许再告警 @13194）。advisory 性质——只提示不拦截。

#### N1-c SQLite busy 预算扩展（配套）

- storage.py:379 `_INTERACTIVE_BUSY_BUDGET_SECONDS = 10.0`（原 2.0，±1 行亲证）；:378 `_SQLITE_BUSY_TIMEOUT_MS = 5000` 维持。
- 对 18 号的影响：并发治理章节的"交互式预算 2s"数值更新为 **10s**，其余机制（WAL/epoch/28 表/accept_turn）不变——storage.py 全 delta 仅此 1 行实质变更。

#### N2 【上游】artifact 自主 HTML 编辑闭环（#1359 及后续修复簇）

> 取证方式：双子代理广度侦察（11 文件全文实读 ≈2.4 万行）+ 主代理差量核对。下述"亲证"均为主代理本轮直接读取；其余为核对采信并标注来源。

**文件定性（※B1 差量核对修正）**：本 delta 中**仅 document_browser.py 为全新文件**（基线 cat-file 不存在，现 1198 行恰等于 diffstat 增量）；repository/service/artifact_preview/mutation_attempts 均为既有文件扩容（git cat-file 裁决：基线 blob 分别为 173,437/51,201/35,352/15,520 B）。

**架构分层**：

| 层 | 文件 | 职责 |
|----|------|------|
| 持久层 | artifact_session/repository.py（5,460 行） | 事务化 SQLite（aiosqlite+WAL+`BEGIN IMMEDIATE`），**13 张业务表**（CREATE TABLE 计数主代理亲证）；字节本体不入库，外部 ArtifactStore 只存 sha256 引用 |
| DDL/不变式 | artifact_session/schema.py | 幂等建表 + BEFORE UPDATE RAISE(ABORT) 不可变触发器（revisions/anchors/audit_events） |
| 门面 | artifact_session/service.py | 无状态参数校验编排层 |
| 回合控制 | mutation_attempts.py | `ArtifactCandidateLoopController` 内存状态机：open→candidate_staged→verification_passed/failed→committed/discarded |
| 工具面 | tools/builtin/document_browser.py | 五工具：`document_browser_inspect/act/screenshot/reload/document_finish`（owner_only、默认不暴露；**finish 是唯一回合计数工具**，decision∈{commit,discard}+expectedCandidateSha256+verificationToken） |
| 分发语义 | tools/dispatch.py | **无路由表**（名字→处理器在各 `@tool` 注册）；新增候选环工具集常量与效果投影族（`_candidate_loop_effect_result`:1764 等） |
| 桌面桥 | gateway/desktop_artifact_bridge.py + artifact_preview.py | preview 租约 + 候选物化 HTTP（offline realm 强制——候选 HTML 属不可信代码禁全网络）；bridge 永不接触 artifact_id/URL/字节，只有 opaque candidate_handle |

**数据完整性关键不变式（主代理亲证）**：
- `artifact_mutation_attempts`：**UNIQUE(turn_id) 单写槽**（schema.py:162）＋四态 CHECK `'reserved'/'applied'/'failed'/'ambiguous'`（:145-146）＋状态-列联合 CHECK（reserved⇒全 NULL、applied⇒change_set+revision 必有、failed/ambiguous⇒failure_code 必有，:163-171）
- ChangeSet 六态 DRAFT→READY→APPLIED / REJECTED 终态，全部 state_revision CAS 守卫
- AMBIGUOUS 语义：只能对账、永不定论（retryPolicy="reconcile"）

**安全闸门五级链**（"用户审核后才提交"的实现）：
1. staging 不动 head——候选写 internal 私有桶（`visibility="internal"`，durable:false）
2. commit 入口三重凭据——**主代理亲读** document_browser.py:1103-1113：`_artifact_candidate_preview_bound` 必须为真 ＋ 进程内 verification token/sha256 双匹配
3. 提交前一刻强制重新 inspect——**主代理亲读** :1120-1123 调用 `_final_browser_health_check`；:1115-1119 注释原文 "The model-provided receipt is necessary but not sufficient"
4. 控制器终检——status=="verification_passed"+token+digest 三一致（mutation_attempts.py:721-728，子代理锚点）
5. 仓库 fail-closed——**主代理亲读** repository.py:3712-3731：DRAFT-only／base==head／无候选拒／无变化拒／摘要不符拒，五路 ArtifactConflictError

**定性判断（重要）**：后端**不存在**"等待用户点击批准"的服务端强制位（子代理 repo_inferred；主代理佐证：rpc_artifact_editing.py 全部 **21 个** JSON-RPC 方法逐一枚举亲证——`@_d.method` 列表 :1371-2803，仅 revert/restore/discard 类事后救济，无 approve/reject 类端点）。"审核"的实现语义＝候选必须绑定到用户正在看的 Electron 活动预览面 ＋ 上述验证链 ＋ 事后 `artifacts.changes.revert`/`artifacts.revisions.restore`。CHANGELOG "users review before committing" 的实现**弱于字面承诺**——审阅是结构保证的机会而非强制的门。前端是否另有确认弹窗属 desktop/electron 排除域，unverified。

**崩溃对账**：artifact_mutation_recovery.py 重启扫描全部 RESERVED/AMBIGUOUS 定案（失败码 `process_restarted_before_commit`/`restart_persistent_result_mismatch`）+ 孤儿草稿批量清扫（reject_orphaned_artifact_drafts）。

**HTML 锚点机制**（html_anchors.py）：lxml 规范 DOM（no_network）＋元素路径＋v1/v2 身份证明哈希（v2 容忍运行时附加类名但要求承诺集⊆期望集 fail-closed）→ source-backed 解析器回映射源码偏移 locator `{start_offset, source_sha256, offset_encoding:"unicode-code-point"}`；旧锚点对新 head 的重匹配为五级唯一性级联（强身份属性→normalized_text→稳定属性→开标签原文→结构轮廓），任一级命中 >1 即放弃为 ORPHANED（`_unique_candidate` :1065 存在性主代理 grep 亲证）。

#### N3 【上游】每会话路由策略与路由面板快照

> 取证方式：双子代理侦察 + 主代理差量核对。标注"亲证"者为本轮主代理直接 grep/read 命中。

**每会话策略存储与回落链**（锚点全套主代理 grep 亲证）：
- 存储：`sessions` 表新列 `model_routing_mode TEXT`(可 NULL)/`model_routing_revision INT`——session/models.py:141、storage.py:558、迁移 :2503；值域 {direct,router,ensemble}
- 新会话物化全局默认：manager.py:671-692 `_default_model_routing_mode`（provider 由 boot.py:3124 注入 `model_routing_snapshot(config)["mode"]`）
- **遗留 NULL 仅原子物化一次**：storage.resolve_model_routing_mode（:5673）在 BEGIN IMMEDIATE 写事务内 `UPDATE ... WHERE model_routing_mode IS NULL` 并 revision+1——此后全局变更不再漂移该会话（source:"legacy_initialized"）
- CAS 写入：set_model_routing_mode（:5762）expected_revision 冲突抛 SessionRoutingConflictError；幂等同值直返
- 适用白名单：仅 session_turn/web_turn/channel_turn；cron/subagent/后台强制全局（session_model_routing.py:112-148 区段）
- RPC 面：`sessions.routing.get/set` + `sessions.routing.changed` 广播（rpc_sessions.py resolver/setter getattr :11696/:11802 亲证）

**RoutingDecision 本体零新增字段**：仍 tier/model/confidence/source 四字段。新增的是 `RouterTierSnapshot(v1)` 版本化候选池快照体系（route_plan.py:75/:95/:126/:325 主代理亲证）＋ `RoutePlan.router_tier_snapshot`（version 升 2）＋ `RouterDecisionEvent` 尾置兼容字段。快照条目携带 `execution_kind ∈ {single_model, ensemble}`；图像候选过滤规则：C3 融合激活时最高档剔出图像候选。

> **对 19 号 L1 的含义**：官方把执行形态放在快照条目的 execution_kind 而非 RoutingDecision 字段——与 L1 方案"RoutingDecision += execution_mode"的设计取向不同，落地时需对齐（快照面向可观测/回放，决策本体保持极简）。

**面板快照双通道**：历史回放走 transcript 内 `DoneEvent.route_plan` 结构化回执（rpc_chat 按"完整快照>结构丰富度"择优合并）；V017 `router_decisions` 表是记录/反馈通道（WebUI 不消费它画卡）。

**#1384 重连重复卡片**：根因＝旧代码 messageId 时间戳回退（重放即生成新 ID 去重失效）；修复＝`(sessionKey, stream_seq)` 确定性身份 ＋ pending→flush 同 ID ＋ 撞车即弃三层防重（纯 WebUI 侧修复）。

**C3 弹性固定回退与图像互斥**（主代理亲证两处原文）：
- 固定回退＝`[llm]` 直连 provider/model 对（非成员列表）；装配缺失即 `raise missing_fixed_fallback`；全败后唯一固定腿；legacy tier-local 选择模式不再拥有第二隐藏回退模型
- **Ensemble 显式拒图**：ensemble.py:115 `"Ensemble does not support image input yet."`（亲读命中）；独立 IMAGE_TIER 无视 TOML 顺序恒居首，产出 `RoutingDecision(source="image_route", confidence=1.0)`
- TokenRhythm B5 剖面：proposers=(deepseek-v4-pro, glm-5.2, kimi-k2.7-code, qwen3.7-max)、aggregator=glm-5.2；toml:40 `ensemble_enabled=true` 与 :42-47 image=kimi-k2.6(image_only) 主代理亲读

**管线外容量裁决 fail-closed**（主代理亲证）：
`finalize_squilla_router_capacity` 在管线之外运行（runtime.py:9422-9426 注释原文亲读，见 §5）；历史容量估算不完整即 block（估算异常亦 fail-closed，#1389）；裁决只许保持或升档（"must never turn a c2/c3 decision into a cheaper lower-complexity route"）；逐候选要求 `model_has_request_capacity` 证明，无证明档 → block → selector 绑定点 **raise LargeContextCapacityError**（selector_override.py:563 亲证；异常类定义 capacity_admission.py:19）——回合终止而非在未证明容量的路线上无限 fallback。

### N4 runtime_packs 不可变目录固化（亲读，repo_verified）

- catalog.py:1-7 头注声明"Immutable, application-owned catalog……deliberately contains asset names and digests, never arbitrary URLs"；:20 `_COMPONENT_IDS = ("python", "node", "gitBash")`（锚点自 ：16 微漂移）+ 各平台目标矩阵（:21-29）。
- 下载源硬编码双源：manager.py:59 `_OSS_BASE = "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/runtime-packs"`、:60 `_GITHUB_BASE = "https://github.com/opensquilla/runtime-packs/releases/download"`；:401/:618 强制 https 校验。
- CHANGELOG 声明下载支持 resume/cancel/source-fallback/integrity/removal/cache-discard（未逐项实测，定性采信 changelog + manager/resolver diff 存在性）。
- skills 侧同型加固：#1367 "verify digests through catalog paths"。
- **对 19 号的影响：强化**——运行时组件获取面进一步收敛为摘要校验的固定目录，"runtime_packs 是受控下载器而非插件系统"的判定更加稳固。

### N5 不可达代码清理潮（#1382/#1385，亲读定性）

整文件删除（−合计约 1345 行）：

| 删除文件 | −行数 | 原职责 |
|----------|-------|--------|
| `cli/repl/clarify_form.py` | 381 | REPL 澄清表单 |
| `tools/candidate_patch_checkpoint.py` | 264 | 候选补丁检查点 |
| `provider/request_proof.py` | 220 | 请求证明（HTML annotation proofs 重写为 `92bea1378` #1392 取代，prompt_annotations.py +152） |
| `session/openai_responses_state.py` | 93 | OpenAI responses 状态遗留 |
| `engine/turn_control.py` | 145 | 回合控制实验代码 |
| `engine/compaction_control.py` | 90 | 压缩控制实验代码 |
| `memory/flush_status.py` | 84 | flush 回执分类辅助 |
| `engine/session_lock.py` | 48 | 会话锁实验代码 |
| `skills/meta/progress_throttle.py` | 50 | 进度节流 meta 技能 |
| `provider/minimax_compat.py` | 50 | MiniMax 兼容层 |

- session_flush.py 同步裁剪 −276 行：删除 `_segment_receipt_payload`/`_plan_with_relative_path` 及尾部 231 行死代码块（diff hunk 亲读）。
- **对 18 号的影响核查**：三级降级主体完好——`_build_flush_segments`（session_flush.py:1032）、`SessionFlushService`（:2016）均在（grep 亲证）；既往分析文档对被删模块零引用（opensquilla-analysis 全文 grep 无命中）。

### N6 supports_tools 来源语义反转（亲读，repo_verified）

model_catalog.py:607 区段 docstring 由：

> 未知模型乐观合成 `supports_tools=True` 以保留通用聊天行为；**source-backed Artifact mutation 不可使用该乐观回退**（否则等于授予持久写工具）

改为：

> 未知模型保持 tools=True 使 agent 回合维持其授权工具面；该 helper 只回答来源(provenance)不回答授权；**调用方不得把未验证值变成 tools 拒绝——只有显式 `supports_tools=False` 才拒绝**

性质：工具准入从"特定场景收紧"反转为"显式 False 才拒"。影响面：依赖 provenance 判断做能力否决的调用方（rpc_models.py:54 "Keep TokenRhythm capability tri-state and explicit False priority" 与之呼应）。对 12/17 号的"能力门控"叙述属于语义级修订，建议后续深挖轮复核全部 provenance 消费方。

### N7 TokenRhythm 默认档位表更新（亲读，repo_verified）

`provider/presets/tokenrhythm.toml:13-37`：

| 档位 | 模型 |
|------|------|
| C0 | `deepseek-v4-flash-0731` |
| C1（default_model） | `deepseek-v4-pro-0813` |
| C2 | `kimi-k2.7-code` |
| C3 | `glm-5.2`（描述："shared B5 fusion"） |

与 CHANGELOG 声明逐字一致。注意 changelog 明示"现有自定义内联档位不迁移"。对 11/12 号的路由档位示例数字构成勘误。

---

## 5. 锚点漂移复核表（17/18/19 号文档行号修正）

全部为本代理今日亲读实测（repo_verified）：

| 锚点（所属文档） | 旧值 | 新值（v0.5.4） | 状态 |
|------------------|------|----------------|------|
| 管线命令式组装起点（19号 L2 目标） | runtime.py:9128-9157 | **runtime.py:9367-9395** | 漂移 +239；结构不变；**`insert(-4, meta_command_launch)` 魔数仍在 @9395** |
| 路由 step 特殊包裹 asyncio.run（19号） | runtime.py:8816-8838 | **runtime.py:9057** 一带 | 漂移；独立线程+asyncio.run 机制不变 |
| TurnStep 定义（19号） | pipeline.py:19 | pipeline.py:**20** | 微漂移；形态不变 |
| 四规范能力 ID 硬编码（17号） | rpc_onboarding.py:288-293 | memory_embedding@**292**（区段仍在） | 微漂移 |
| `_COMPONENT_IDS`（19号排除项） | catalog.py:16 | catalog.py:**20** | 微漂移 |
| storage.py 巨石指标（18号/A9） | 646349 B / 15264 行 / 275 方法 | git blob 631084→631085 B（+1 B=busy 值）；磁盘 15264 物理行 | 维持（唯一实质变更即 busy 值） |
| 三级降级主体（18号） | session_flush 内 | `_build_flush_segments`:1032、`SessionFlushService`:2016 | 维持 |

19号新增强化证据（同一区域亲读）：
- 组装区新增 `planning_turn`（collaboration_mode=="plan"，:9362-9366）与 `restricted_tool_boundary`（PromptAnnotation 回合，:9378-9393）两条条件分支——命令式组装复杂度进一步上升，L2"装配声明化"论据增强；
- 管线之后新增管线外裁决 `finalize_squilla_router_capacity`（:9426），注释明言 "Capacity admission is safety-critical... outside the generic fail-open pipeline wrapper"——附件路由容量改为 fail-closed（对应 #1389）。

---

## 6. 异常信号与漂移信号更新

### 巨型文件榜复测（物理行口径统一后）

| # | 文件 | 大小 | 行数 | 变化 |
|---|------|------|------|------|
| 1 | engine/agent.py | **1,217,990 B** | 25,133 | ↑（基线 1.17MB；本delta +1239 行） |
| 2 | session/storage.py | 646,349 B（git blob 631,085） | 15,264 | git blob +1 B——唯一实质变更即 busy 值 2.0→10.0 |
| 3 | engine/runtime.py | 618,341 B | 13,998 | ↑ |
| 4 | gateway/rpc_sessions.py | **516,713 B** | — | **[CANDIDATE·新入榜]** 本delta +398 行 |
| 5 | gateway/task_runtime.py | 302,224 B | — | ↑ |
| 6 | provider/ensemble.py | 285,865 B | — | ±95 |
| 新 | artifact_session/repository.py | **226,555 B**（基线 173,437 B） | 5,460 | **[CANDIDATE·扩容至巨石级]**（→N2）※B1 修正：非新建 |
| 新 | gateway/terminal_activity.py | — | 1,163 | [可选] 单一职责清晰，暂不列风险 |
| 新 | sandbox/upgrade_migration.py | — | 1,069 | [可选] 平台迁移逻辑 |

> 口径注：16 号记"35 业务模块"，本次实测目录 36=36（含 plugins/dist/compat 等边缘目录，不含 __pycache__）。差异为统计边界而非结构变化，S2 映射无需重跑。

### 漂移信号

| ID | 位置 | 内容 | 级别 | 置信度 |
|----|------|------|------|--------|
| **D10** | runtime_packs/catalog.py:4 | 文档字符串称下载源固定于 `opensquilla.runtime_packs.sources` 模块——**该模块不存在**（目录仅 catalog/models/manager/resolver/__init__ 五文件，glob 亲证）；实际硬编码在 manager.py:59-60。意图真实（不可变源），文档指错位置 | P3 | repo_verified |
| D11 | model_catalog.py:607 | supports_tools 准入语义反转未见 CHANGELOG 条目（行为语义变化藏于代码注释改写） | P3 | repo_verified（语义变化亲读；"无 changelog 条目"基于 0.5.4 节全文检索） |

---

## 7. 对既有结论的影响评估

| 文档 | 判定 | 说明 |
|------|------|------|
| 18 号（会话存储与记忆） | **维持** | storage.py 全 delta 仅 busy 值 1 行；内存侧死代码清理不触及三级降级/发件箱主体；§5 锚点全维持。唯一数值修订：交互式 busy 预算 2s→**10s** |
| 17 号（配置的能力） | **微漂移** | 能力 ID 锚点 +4 行；D1/D4 漂移结论不受影响；N6 语义反转涉及能力/工具准入面，建议下轮深挖复核 |
| 19 号（插件化可行性） | **强化 + 锚点更新** | insert(-4) 魔数与命令式组装依旧且更复杂（新增 planning/restricted 分支）；runtime_packs 固化目录强化"非插件系统"判定；`plugins/` 目录实为 37 字节空占位 `__init__.py`（亲读），不影响结论；L1 改造锚点按 §5 更新至 runtime.py:9367。另注意（N3）：官方将执行形态放至 `RouterTierSnapshot.execution_kind` 快照而非 RoutingDecision 本体字段，L1 的 "RoutingDecision += execution_mode" 设计落地时需与该走向对齐 |
| 11/12 号（路由/辅助模型） | **数字勘误** | TokenRhythm 档位表按 N7 更新；C3 ensemble 弹性回退与独立图像路由见 §4-N3 |
| 16 号 | 被本文接替为最新勘误层 | 版本/巨型文件/模块数以本文为准 |

---

## 8. 待办与可补做项

1. **[必挖候选] rpc_sessions.py 516KB**（新晋 #4 巨型文件）：回复「深挖 rpc_sessions」可从 checkpoint 续跑 S5。
2. **[必挖候选] artifact 编辑闭环全景**：N2 占位区填充后如需更深（安全闸门/状态机全图），回复「深挖 artifact 编辑闭环」。
3. **[建议] N6 supports_tools 语义反转的全部 provenance 消费方排查**：回复「深挖 supports_tools 准入链」。
4. D10/D11 修复属维修科范畴，未经人工确认不落地；如需修复回复「落地 D10」。
5. 19 号 L1 路由内核化改造仍待方案确认（Stage 9 前置条件未满足，状态不变）。

---

## 9. 追补（08-26 晚）— 对 20 号前置复核与 MVP 设计的增量影响

> 背景：20 号前置复核（11:14）完成于 v0.5.3 工作区；本节为 v0.5.4 下对其锚点与 MVP 设计结论的定向补正。原独立草稿已并入本节（去重）。

### 9.1 对 20 号锚点的行号修正（v0.5.4 实测）

| 20 号所述 | v0.5.3 行号 | v0.5.4 现行号 |
|---|---|---|
| `_run_pipeline` 定义 | runtime.py:8734 | **:8972** |
| pipeline_steps 组装 | :9128-9157 | **:9367 起** |
| 融合总闸 | :9438-9442 | **:9702-9706** |
| 收口 `return turn, provider` | :9648 单点 | 早退分支多处 + 终收口 **:9761** |
| RoutePlan 类定义 | route_plan.py:62 | **:111**（前部新增 RouterTierSnapshot 族） |
| bypass hack 两处 | squilla_router.py:1239/:1809 | **不变**（该文件本轮零变更） |

20 号核心语义结论（晚钩子推荐、内核两路径零 diff、扩展两类分型）经结构比对**全部维持**。

### 9.2 MVP 设计的两个实质冲击（对 19/20 号设计参数）

1. **上游已有会话级执行策略选择器**：`sessions.routing.get/set` RPC（rpc_sessions.py:11740-11784），mode 枚举 **`{direct, router, ensemble}`**、CAS expectedRevision、会话级持久字段（session/models.py:138 起 routing strategy）。#1312 在旧基线前已合入（CHANGELOG 0.5.4 系按发布窗口补记）。**冲击**：MVP 原拟 `turn.metadata["execution_mode"]` 若照做即与 `routing_strategy` 构成两套平行概念——触发自家反模式红线。修订：扩展注册表键位必须与 `routing_strategy` 对齐（枚举加值=动上游校验行 1 处锚点；或保持三值、c3 下分子模式=零上游改动，融合协议双轨制天然支持后者）。
2. **载体天平移动**：RouterTierSnapshot.execution_kind 已把执行方式观测面固化在 RoutePlan 层（见 §7-N3），metadata 方案进一步降级为内部实现细节。

### 9.3 修订后留给主人的两个裁决参数（替代 20 号 §F8）

1. 扩展注册表键位：(a) 扩展 `routing_strategy` 枚举（1 处上游锚点）vs (b) 三值不动 + c3 子模式分层（零上游改动，推荐起点）；
2. 钩子位置维持晚钩子推荐（:9761 终收口前），除非主人需要早钩子接管绑定语义。

### 9.4 数据勘误

内置工具数第四档实测：**89**（v0.5.4 @tool 计数；序列 74/88/84/89——AGENTOS 总索引 D1 已同步）。

---

*取证方式：主代理亲读（waterline/busy/catalog/model_catalog/session_flush/presets/锚点全套/安全闸门核心环/RPC 方法全集）+ 双子代理差量核对三分类（A 一致确认/B 冲突亲证裁决/C 复核入档，零静默丢弃；B1 文件定性误标已修正、B2 tag 哈希子代理误报已裁决、B3 区间口径双数字并存澄清）。Hindsight 记忆库本会话 401 不可用，checkpoint 为唯一跨会话事实源。*
