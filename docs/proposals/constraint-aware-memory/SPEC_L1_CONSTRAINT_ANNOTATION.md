# L1: 约束类型标注 — 实现 Spec

> **Status**: ✅ 已实现并完成跨层验证
> **依赖**: 无（可独立运行）
> **Feature flag**: `memory.experimental.constraint_annotation = false`
> **分支**: `feature/constraint-aware-memory`
> **作者**: KunYu + OpenSquilla
> **日期**: 2026-07-29

---

## 1. 设计目标

在 memory chunk 写入时标注约束类型（`constraint_type`），为 L2 约束感知检索路由提供元数据基础。

核心原则（已对齐）：
1. **关闭 = no-op**：flag 关闭时行为与当前完全一致
2. **三级降级**：LLM → 启发式 → 默认 "fact"
3. **Signal Gate (D8)**：低信号 chunk 跳过分类（节省 30-40% LLM 调用）
4. **置信度保护 (D2)**：confidence < 0.6 → L2 不应用 boost（最坏 = 中性）
5. **用户可覆盖 (D6)**：Markdown frontmatter `constraint_type:` 优先于自动分类
6. **数据只增不改**：新增 nullable 列，不迁移已有数据

---

## 2. 约束类型 Ontology

### 核心 6 类（v0.7 激活）

| constraint_type | 语义 | FlushCandidate.kind 映射 |
|----------------|------|------------------------|
| `fact` | 客观事实（默认） | `fact` |
| `event` | 时间性事件 | `event` |
| `preference` | 用户稳定偏好 | `preference` |
| `decision` | 已做出的决策 + 理由 | `decision` |
| `procedure` | 操作步骤 | `procedure` |
| `goal` | 目标/意图 | `goal`, `todo` |

### 扩展 4 类（保留枚举，v0.8 再激活）

| constraint_type | 语义 | 推迟理由 |
|----------------|------|---------|
| `assumption` | 隐含假设 | 需要推理链分析 |
| `constraint` | 硬约束 | 与 assumption 边界模糊 |
| `anti_pattern` | 反面模式 | 与 pattern 是同一结构的两面 |
| `pattern` | 可迁移问题结构 | 匹配机制未就绪 |

---

## 3. Schema 变更

### 3.1 chunks 表新增列

`sql
ALTER TABLE chunks ADD COLUMN constraint_type TEXT DEFAULT 'fact';
ALTER TABLE chunks ADD COLUMN constraint_confidence REAL;
`

- `constraint_type`：默认 `'fact'`（向后兼容）
- `constraint_confidence`：NULL 表示未标注
- 已有行不受影响（ALTER TABLE ADD COLUMN 自动填充 DEFAULT）
- 幂等：`_ensure_schema` 中检测列是否存在

### 3.2 不加 FTS / 索引

`constraint_type` 不加入 FTS（不需要全文搜索约束类型）。
L2 路由在 Python 层做 boost，不需要 SQL 层按 constraint_type 过滤。

---

## 4. 分类管线

`
chunk 写入
  │
  ├─ Signal Gate (D8)
  │     ├─ 长度 < 20 字符 → 跳过，默认 fact
  │     ├─ 纯工具输出（无自然语言）→ 跳过，默认 fact
  │     └─ 心跳/状态消息 → 跳过，默认 fact
  │
  ├─ LLM 分类（主路径，constraint_annotation 开启时）
  │     ├─ 成功 → 输出 (type, confidence)
  │     └─ 失败/超时 → 启发式降级
  │
  ├─ 启发式分类（降级路径）
  │     ├─ 关键词匹配 → 输出 (type, confidence=0.5-0.7)
  │     └─ 无匹配 → 默认 "fact", confidence=0.4
  │
  └─ 写入 chunks 表
        └─ constraint_type, constraint_confidence
`

---

## 5. 触发点

### 触发点 A：memory 文件 sync（`index_file`）

`LongTermMemoryStore.index_file()` 内，chunk INSERT 之后、事务提交之前：

`python
if self._constraint_annotation_enabled:
    for i, (cid, ...) in enumerate(chunk_records):
        ct, conf = await self._classify_constraint(chunk_records[i][7])
        await self._db.execute(
            "UPDATE chunks SET constraint_type=?, constraint_confidence=? WHERE id=?",
            (ct, conf, cid),
        )
`

### 触发点 B：flush candidate 提取

`session_flush.py` 已有 `CandidateKind` 枚举。直接映射，无需额外 LLM 调用：

`python
_KIND_TO_CONSTRAINT = {
    "fact": "fact", "event": "event", "preference": "preference",
    "decision": "decision", "procedure": "procedure",
    "todo": "goal", "goal": "goal",
}
`

---

## 6. 分类器实现（`constraint_classifier.py`）

### 6.1 Signal Gate

| 条件 | 判定 |
|------|------|
| 长度 < 20 字符 | 低信号 → 跳过 |
| 字母/CJK 占比 < 0.3 | 低信号 → 跳过（纯工具输出） |
| 心跳/状态消息模式 | 低信号 → 跳过 |

### 6.2 LLM 分类 Prompt

`
Classify this memory chunk into exactly one type:
[fact, event, preference, decision, procedure, goal]

fact: a stable objective fact
event: something that happened at a specific time
preference: a user's ongoing preference or style choice
decision: a choice made with reasoning
procedure: a step-by-step process or how-to
goal: a target, intention, or task to be done

Chunk:
{chunk_text}

Reply with ONLY the type name.
`

### 6.3 启发式规则

`python
_HEURISTIC_RULES = [
    (["decided", "chose", "选择", "决定", "we went with"], "decision", 0.6),
    (["prefer", "like", "偏好", "喜欢", "习惯"], "preference", 0.6),
    (["step", "how to", "run", "步骤", "流程", "安装"], "procedure", 0.6),
    (["goal", "todo", "task", "目标", "任务", "计划", "继续"], "goal", 0.5),
    (["yesterday", "last week", "昨天", "上周", "刚刚"], "event", 0.5),
]
# 无匹配 → ("fact", 0.4)
`

### 6.4 降级链

`
Signal Gate 未通过 → ("fact", None)
LLM 成功 → (type, 0.8)
LLM 失败 → 启发式 → (type, 0.4-0.6)
启发式无匹配 → ("fact", 0.4)
`

---

## 7. Feature Flag

### 7.1 Config（`gateway/config.py`）

`python
class MemoryExperimentalConfig(BaseModel):
    constraint_annotation: bool = False   # L1
    constraint_routing: bool = False      # L2 (future)
    sufficiency_check: bool = False       # L3 (future)

class MemoryConfig(BaseSettings):
    ...
    experimental: MemoryExperimentalConfig = Field(default_factory=MemoryExperimentalConfig)
`

### 7.2 传递路径

`
config.memory.experimental.constraint_annotation
  → MemoryManager.__init__()
    → LongTermMemoryStore(constraint_annotation_enabled=...)
      → index_file() 条件调用分类器
`

### 7.3 TOML

`	oml
[memory.experimental]
constraint_annotation = false
`

---

## 8. 用户覆盖（Frontmatter）

`markdown
---
constraint_type: procedure
---
部署流程：build → test → push → verify
`

Sync 时解析 frontmatter → 覆盖自动分类结果，confidence = 1.0。

---

## 9. 降级与边界

| 场景 | 行为 |
|------|------|
| feature flag 关闭 | 不分类，所有 chunk 保持 DEFAULT 'fact' |
| chunk < 20 字符 | 跳过分类，默认 fact，confidence = NULL |
| 纯工具输出 | 跳过分类，默认 fact |
| LLM 不可用 | 启发式降级 |
| LLM 返回无效类型 | 启发式降级 |
| 启发式无匹配 | 默认 fact，confidence = 0.4 |
| confidence < 0.6 | 存入但 L2 不应用 boost（中性） |
| 用户 frontmatter 覆盖 | 使用用户标注，confidence = 1.0 |
| 旧数据无 constraint_type | DEFAULT 'fact'（数据库 DEFAULT） |
| 分类超时 | 降级到启发式，不阻塞写入 |

---

## 10. 改动范围

| 文件 | 变更 | 风险 |
|------|------|------|
| `src/opensquilla/memory/types.py` | 新增 `ConstraintType` 枚举 + 映射 | 低 |
| `src/opensquilla/memory/constraint_classifier.py` | **新文件**：分类器 | 低 |
| `src/opensquilla/memory/store.py` | Schema 扩展 + `index_file` 集成 | 低 |
| `src/opensquilla/memory/session_flush.py` | CandidateKind → ConstraintType 映射 | 低 |
| `src/opensquilla/gateway/config.py` | `MemoryExperimentalConfig` | 低 |
| `tests/test_memory/test_constraint_annotation.py` | **新文件** | 低 |

---

## 11. 验收标准（DoD）

- [ ] Schema 迁移：`constraint_type` + `constraint_confidence` 列（幂等）
- [ ] `ConstraintType` 枚举（核心 6 + 扩展 4）
- [ ] `constraint_classifier.py`：Signal Gate + LLM + 启发式 + 降级链
- [ ] `index_file` 路径集成分类器（feature flag 控制）
- [ ] Frontmatter `constraint_type:` 解析覆盖
- [ ] `session_flush` CandidateKind 映射
- [ ] `MemoryExperimentalConfig` + `MemoryConfig.experimental`
- [ ] 单元测试：Signal Gate、启发式分类、映射、降级
- [ ] 回归测试：flag 关闭时行为不变
- [ ] 集成测试：完整 sync → classify → 存储 流程
- [ ] 现有测试全部通过（无回归）

---

## 12. 与 L0/D12/L2 的关系

- **L0**：归档 transcript 可搜索（正交，无依赖）
- **D12**：compaction anchor（正交，无依赖）
- **L2**：约束感知检索路由（依赖 L1 的 constraint_type 元数据）
  - L1 关闭 → 所有 chunk `constraint_type = "fact"` → L2 boost 1.0 → no-op
  - L1 开启 + L2 关闭 → 分类结果存储但不影响检索
  - L1 开启 + L2 开启 → 完整约束感知检索
