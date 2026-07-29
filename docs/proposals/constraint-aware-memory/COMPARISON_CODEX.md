# Codex Memory System vs Constraint-Aware Memory: 对比学习

> **Date**: 2026-07-29
> **Purpose**: 从 Codex (OpenAI) 的记忆系统实现中提取设计洞见，对照我们的 constraint-aware-memory 提案，识别可借鉴的模式和差异化方向。
> **Source**: `C:\Users\KunYu\workspace\harness\codex` (codex-rs)

---

## 1. 架构总览对比

### Codex 记忆系统

```
┌─────────────────────────────────────────────────────────────────────┐
│ 使用层: memory_summary.md → 注入 system prompt (always loaded)      │
│         MEMORY.md → grep/keyword 检索                               │
│         skills/ → 可复用过程                                         │
│         rollout_summaries/ → 单次会话蒸馏                            │
├─────────────────────────────────────────────────────────────────────┤
│ 读取层: codex-memories-read                                         │
│   memory injection (developer instructions)                         │
│   citation parsing (<citation_entries>, <rollout_ids>)              │
│   usage telemetry (MemoriesUsageKind: 5 种使用类型)                  │
├─────────────────────────────────────────────────────────────────────┤
│ 写入层: codex-memories-write (两阶段管线)                            │
│   Phase 1: per-rollout extraction (并行, concurrency=8)             │
│     → raw_memory + rollout_summary + rollout_slug                   │
│   Phase 2: global consolidation (串行, 单锁)                        │
│     → git-baseline diff → consolidation sub-agent → 文件更新         │
├─────────────────────────────────────────────────────────────────────┤
│ 存储层:                                                             │
│   thread-store (JSONL rollout + SQLite metadata)                    │
│   message-history (~/.codex/history.jsonl, append-only)             │
│   agent-graph-store (parent/child thread topology)                  │
│   state DB (Phase 1 job claims, watermarks, leases)                 │
├─────────────────────────────────────────────────────────────────────┤
│ 注入层: context-fragments (ContextualUserFragment trait)            │
│   30+ fragment types, marker-based recognition                      │
│   bounded size (MAX 10K tokens per item, 1K per additional context) │
│   no history rewrite, incremental append only                       │
└─────────────────────────────────────────────────────────────────────┘
```

### OpenSquilla 当前 + 我们的提案

```
┌─────────────────────────────────────────────────────────────────────┐
│ 使用层: memory_search tool → 注入 system prompt                     │
│         session_search tool → transcript 全文检索                    │
├─────────────────────────────────────────────────────────────────────┤
│ 检索层: MemoryRetriever                                             │
│   hybrid search (vector 0.7 + FTS5 0.3)                            │
│   temporal decay + MMR diversity rerank                             │
│   [L2 提案] constraint-aware routing + boost                        │
├─────────────────────────────────────────────────────────────────────┤
│ 索引层: LongTermMemoryStore                                         │
│   SQLite + FTS5 + sqlite-vec                                        │
│   [L0 已实现] compacted_transcript_entries FTS                      │
│   [L1 提案] constraint_type 标注                                    │
├─────────────────────────────────────────────────────────────────────┤
│ 整合层: Dream (cron-scheduled)                                      │
│   evidence-gated promotion                                          │
│   [L1 提案] constraint_stability 评分维度                            │
├─────────────────────────────────────────────────────────────────────┤
│ 会话层: SessionManager                                              │
│   transcript_entries (active) → compacted_transcript_entries (archived) │
│   compaction (context window 管理)                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 核心设计哲学对比

| 维度 | Codex | OpenSquilla (当前) | 我们的提案 |
|------|-------|-------------------|-----------|
| **记忆本质** | 行为改变工具 ("change future agent behavior") | 文档检索系统 | 推理资源管理系统 |
| **写入触发** | 会话结束后异步 (startup pipeline) | 实时 flush + cron Dream | 同 OpenSquilla |
| **写入方法** | LLM 抽取 (Phase 1) + LLM 整合 (Phase 2 sub-agent) | 规则 + embedding | LLM 分类 (L1) + 规则路由 (L2) |
| **质量控制** | "No-op is allowed and preferred" 信号门 | evidence-gated promotion | 约束类型稳定性 + 充分性检查 (L3) |
| **存储模型** | 文件系统 (git-tracked) + SQLite state | SQLite + FTS5 + vec | 同 OpenSquilla + FTS 扩展 |
| **注入模型** | 分层: summary (always) → MEMORY.md (grep) → skills (on-demand) | 检索 top-k 注入 | 同 + constraint boost |
| **历史保留** | JSONL append-only, 永不删除 | compaction 后 DELETE + archive | L0: archive 可搜索 |
| **隐私** | secret redaction (Phase 1 prompt 内置) | 无显式机制 | 模型认同 (非系统封锁) |

---

## 3. 关键设计模式提取

### 3.1 Phase 1 信号门 (No-Op Gate)

Codex Phase 1 prompt 中有一个极其重要的设计：

```
Before returning output, ask:
"Will a future agent plausibly act better because of what I write here?"
If NO → return all-empty fields.
```

**洞见**：不是"所有信息都值得记住"，而是"只有能改变未来行为的信息才值得记住"。

**对我们的启示**：
- L1 约束分类时，不是所有 chunk 都需要标注——低信号 chunk 可以跳过分类（节省 LLM 调用）
- Dream promotion 时，`constraint_stability` 维度本质上就是在问"这个约束在未来还会被需要吗？"
- 我们的 `anti_pattern` 类型直接对应 Codex 的 "failure shields"

### 3.2 两阶段写入管线 (Extract → Consolidate)

```
Phase 1 (并行, per-rollout):
  raw conversation → LLM → {raw_memory, rollout_summary, rollout_slug}
  
Phase 2 (串行, global):
  N × raw_memory → git diff → consolidation sub-agent → MEMORY.md + skills/
```

**洞见**：抽取和整合是两个不同的认知任务，不应该混在一起。
- 抽取：从噪声中提取信号（per-session，可并行）
- 整合：将多个信号融合为一致的知识结构（global，必须串行）

**对我们的启示**：
- 当前 OpenSquilla 的 flush 是"抽取+写入"一步完成，没有全局整合阶段
- Dream 部分承担了整合角色，但它是基于评分的 promotion，不是基于 diff 的 consolidation
- **可借鉴**：在 Dream 中引入 "workspace diff" 概念——不只评分单条记忆，而是看"自上次整合以来，记忆空间整体发生了什么变化"

### 3.3 ContextualUserFragment (有界注入)

Codex 的上下文注入有严格约束：

```rust
// AGENTS.md 规则:
// 1. No history rewrite - incremental only
// 2. Avoid frequent changes (cache misses)
// 3. No unbounded items - hard cap
// 4. No items > 10K tokens
// 5. >1K tokens items need P0 review
// 6. All fragments implement ContextualUserFragment trait
```

每个 fragment 有：
- `role()` — user / developer
- `markers()` — 可识别的起止标记
- `body()` — 有界内容
- `matches_text()` — 后续可识别"这是注入的，不是用户说的"

**洞见**：注入的内容必须是**可识别的、有界的、不可变的**。模型需要知道"这段不是我说的，是系统注入的"。

**对我们的启示**：
- L2 constraint-aware routing 的结果注入时，应该带有明确的元数据标记（"以下结果经过约束路由优化"）
- L3 充分性检查的提示注入，应该是一个独立的 fragment 类型，而不是混在检索结果里
- 当前 OpenSquilla 的 memory_search 结果注入缺乏这种结构化标记

### 3.4 Usage Telemetry (使用反馈闭环)

Codex 的 `memories-read` crate 追踪 5 种使用类型：

```rust
enum MemoriesUsageKind {
    MemoryMd,        // 手册被引用
    MemorySummary,   // 摘要被加载
    RawMemories,     // 原始记忆被查阅
    RolloutSummaries,// 会话摘要被查阅
    Skills,          // 技能被使用
}
```

并且通过 shell 命令解析来检测"模型是否主动读取了记忆文件"。

**洞见**：记忆的价值不是写入时决定的，而是**被使用时确认的**。使用频率 → Phase 2 选择权重。

**对我们的启示**：
- 当前 OpenSquilla 没有 memory chunk 的使用追踪
- L1 的 `constraint_type` 标注 + 使用追踪 → 可以回答"哪种约束类型最常被需要？"
- 这直接影响 L2 的 boost 权重校准：如果 `procedure` 类型被使用频率远高于 `fact`，那 procedure 的 boost 应该更高
- **可借鉴**：在 `search_transcript` 和 `memory_search` 的调用路径中加入轻量 usage 计数

### 3.5 Git-Baseline Diff (变更感知整合)

Phase 2 不是"重新生成所有记忆"，而是：
1. 维护一个 git baseline（上次成功整合的快照）
2. 同步新的 raw_memories + rollout_summaries 到工作区
3. 计算 git diff（新增/修改/删除了什么）
4. 将 diff 交给 consolidation agent
5. Agent 只处理**变化部分**

**洞见**：整合应该是增量的、变更驱动的，而不是全量重建。

**对我们的启示**：
- Dream 当前是全量扫描 + 评分，没有"自上次以来什么变了"的概念
- 如果引入 constraint_type，可以追踪"自上次 Dream 以来，哪些约束类型的分布发生了显著变化"
- 这比逐条评分更高效，也更能捕捉全局模式

### 3.6 Thread Topology (会话图谱)

`agent-graph-store` 维护 parent/child 线程关系：

```rust
trait AgentGraphStore {
    fn upsert_thread_spawn_edge(parent, child, status);
    fn set_thread_spawn_edge_status(child, status);
    fn list_thread_spawn_children(parent, status_filter);
    fn list_thread_spawn_descendants(root, status_filter);  // BFS
}
```

**洞见**：会话不是孤立的——sub-agent 会话和父会话之间有因果关系。记忆应该能沿着这个图谱传播。

**对我们的启示**：
- OpenSquilla 的 `sessions_spawn` 创建子会话，但没有持久化的拓扑关系
- 如果父会话的约束（如"这个项目用 Rust"）能自动传播到子会话，就不需要每次重新注入
- 这是 L2 constraint routing 的一个自然扩展：约束可以沿会话图谱继承

---

## 4. Codex 没有而我们有的（差异化优势）

| 我们的设计 | Codex 对应 | 差异 |
|-----------|-----------|------|
| **约束类型 ontology (10 类)** | 无显式分类，靠 LLM 自由判断 | 我们更结构化、可审计、可校准 |
| **Constraint-aware routing (L2)** | 无——检索是纯语义的 | 我们引入了"推理需求→信息类型"的映射 |
| **充分性检查 (L3)** | 无——注入后不验证 | 我们闭环验证"这些信息够不够用" |
| **Compaction archive 可搜索 (L0)** | JSONL 永不删除，但无 FTS | 我们在压缩后仍提供结构化搜索 |
| **三级降级链** | Phase 1 失败 → retry backoff | 我们的降级更细粒度 (LLM→启发式→默认) |
| **用户覆盖 (frontmatter)** | 无——用户不能直接标注记忆类型 | 我们允许人类专家修正自动分类 |

---

## 5. Codex 有而我们缺的（可借鉴方向）

| Codex 设计 | 我们当前状态 | 建议 |
|-----------|------------|------|
| **No-op signal gate** | 无——所有 chunk 都会被索引 | L1: 低信号 chunk 跳过分类 |
| **Usage telemetry** | 无 | 中期: 在检索路径加 usage 计数 |
| **Git-baseline diff consolidation** | Dream 是全量扫描 | 中期: Dream 引入增量 diff 模式 |
| **Memory citation** | 无——检索结果无溯源 | 短期: L2 结果带 constraint_type 标签 |
| **Progressive disclosure** | 无——top-k 平铺注入 | 中期: summary → detail → raw 三层 |
| **Secret redaction** | 无显式机制 | 短期: 在 flush 路径加 regex 过滤 |
| **Phase 1 concurrency + lease** | Dream 是单线程 cron | 低优先级: 当前规模不需要 |
| **Thread topology persistence** | 无 | 长期: 会话图谱 + 约束继承 |

---

## 6. 对我们设计的具体修正建议

基于 Codex 对比，建议对 DESIGN.md 做以下补充：

### 6.1 L1 补充: Signal Gate

在约束分类之前，增加一个轻量信号门：

```python
def should_classify(chunk: MemoryChunk) -> bool:
    """Skip classification for low-signal chunks."""
    # 纯状态更新、心跳、单字回复等
    if len(chunk.content.strip()) < 20:
        return False
    # 纯工具输出（无人类语言）
    if chunk.source == "tool_result" and not has_natural_language(chunk.content):
        return False
    return True
```

### 6.2 L2 补充: Result Provenance Markers

检索结果注入时，带有结构化元数据：

```
<memory_result constraint_type="procedure" confidence="0.85" boost="1.4">
[actual content]
</memory_result>
```

这让模型知道：
- 这条结果为什么被选中（约束匹配）
- 系统对分类的置信度
- 是否经过了 boost

### 6.3 Dream 补充: Incremental Diff Mode

```python
# 不是: for each chunk: score(chunk)
# 而是:
since_last_dream = get_chunks_modified_since(last_dream_ts)
distribution_shift = compute_constraint_distribution_shift(since_last_dream)
if distribution_shift.significant:
    # 全局重新评估受影响的约束类型
    affected_types = distribution_shift.affected_types
    re_evaluate_promotion(affected_types)
```

### 6.4 中期: Usage Tracking

```sql
ALTER TABLE memory_chunks ADD COLUMN usage_count INTEGER DEFAULT 0;
ALTER TABLE memory_chunks ADD COLUMN last_used_at INTEGER;
```

在 `memory_search` 和 `session_search` 返回结果时，异步更新 usage 计数。
Phase 2 (Dream) 使用 `usage_count` 作为 promotion 权重的额外信号。

---

## 7. 设计原则对齐确认

Codex 的实践验证了我们 DESIGN.md 中的几个核心原则：

| 我们的原则 | Codex 验证 |
|-----------|-----------|
| "压缩 = 目的下的语义等价变换" | Phase 1 prompt: "optimize for future user time saved" — 目的明确 |
| "数据只增不改" | "Raw rollouts are immutable evidence. NEVER edit raw rollouts." |
| "三级降级" | Phase 1: LLM → no-output → failed (with retry backoff) |
| "关闭=no-op" | Feature flag: `memories.enabled`, 关闭时整个管线不启动 |
| "隐私通过认同" | Secret redaction 是 prompt 层面的（模型认同），不是系统层面的访问控制 |

---

## 8. 总结: 一句话差异

> **Codex 的记忆系统是"行为改变管线"——从对话中提取能改变未来 agent 行为的信息。**
>
> **我们的 constraint-aware-memory 是"推理资源管理"——在检索时匹配当前推理的约束需求。**

两者不矛盾：
- Codex 解决的是 **"写什么"**（写入端质量控制）
- 我们解决的是 **"找什么"**（读取端需求匹配）

完整的系统需要两者：高质量的写入 + 精准的检索。我们的 L1 (约束分类) 实际上是在写入端引入了 Codex Phase 1 的结构化思想，而 L2/L3 是 Codex 完全没有的读取端创新。

---

## 附录 A: Codex 关键文件索引

| 文件 | 内容 |
|------|------|
| `codex-rs/memories/README.md` | 两阶段管线完整文档 |
| `codex-rs/memories/write/templates/memories/stage_one_system.md` | Phase 1 抽取 prompt (31KB) |
| `codex-rs/memories/write/templates/memories/consolidation.md` | Phase 2 整合 prompt (52KB) |
| `codex-rs/memories/write/src/lib.rs` | 写入路径入口 + 常量定义 |
| `codex-rs/memories/write/src/phase1.rs` | Phase 1 实现 (claim → extract → store) |
| `codex-rs/memories/write/src/phase2.rs` | Phase 2 实现 (sync → diff → agent → reset) |
| `codex-rs/memories/read/src/usage.rs` | 使用遥测分类 |
| `codex-rs/memories/read/src/citations.rs` | 记忆引用解析 |
| `codex-rs/context-fragments/src/fragment.rs` | ContextualUserFragment trait |
| `codex-rs/message-history/src/lib.rs` | JSONL append-only 历史 |
| `codex-rs/thread-store/README.md` | 线程存储边界 |
| `codex-rs/agent-graph-store/src/store.rs` | 会话图谱 trait |
| `AGENTS.md` | 模型可见上下文规则 (10K token cap, no rewrite) |

## 附录 B: Codex Phase 1 输出 Schema

```json
{
  "type": "object",
  "properties": {
    "rollout_summary": { "type": "string" },
    "rollout_slug": { "type": ["string", "null"] },
    "raw_memory": { "type": "string" }
  },
  "required": ["rollout_summary", "rollout_slug", "raw_memory"],
  "additionalProperties": false
}
```

## 附录 C: Codex Phase 1 高信号记忆分类

1. **Stable user operating preferences** — 用户反复要求/纠正/打断强制的
2. **High-leverage procedural knowledge** — 难以发现的快捷路径、失败防护
3. **Reliable task maps and decision triggers** — 真相在哪里、何时该转向
4. **Durable evidence about environment/workflow** — 稳定的工具习惯、仓库约定

对应我们的 ontology:
- (1) → `preference` + `constraint`
- (2) → `procedure` + `anti_pattern`
- (3) → `decision` + `pattern`
- (4) → `fact` + `event`
