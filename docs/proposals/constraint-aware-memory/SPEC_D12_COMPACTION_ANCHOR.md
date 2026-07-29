# D12: Compaction Anchor 机制 — 实现 Spec

> **Status**: ✅ 已实现并完成当前会话与 full-fork 闭环验证
> **依赖**: L0（归档 Transcript 可搜索）✅ 已实现
> **分支**: `feature/constraint-aware-memory`
> **作者**: KunYu + OpenSquilla
> **日期**: 2026-07-29

---

## 1. 设计目标

让模型在 compaction 后能**主动、精确地展开原始信息**，而不是只能依赖 FTS 重新搜索。

核心原则（KunYu 确认）：
1. **模型主动性**：模型自己判断 summary 是否足够"锚定"，决定是否展开
2. **精确性**：按 ID 取原文，不走 FTS 匹配
3. **轻量**：一次精确查找，不需要构造搜索 query
4. **可组合**：summary 中多个 anchor，模型选择性展开
5. **向后兼容**：关闭/无 anchor 时行为与 L0 完全一致

---

## 2. 代码基线（已确认）

### 2.1 `compacted_transcript_entries` 表（`session/storage.py:547`）

```sql
CREATE TABLE IF NOT EXISTS compacted_transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    compaction_id TEXT,
    compaction_index INTEGER,
    original_entry_id INTEGER,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    reasoning_content TEXT,
    turn_usage TEXT,
    turn_context TEXT,
    created_at INTEGER NOT NULL,
    token_count INTEGER,
    provenance_kind TEXT,
    provenance_origin_session_id TEXT,
    provenance_source_session_key TEXT,
    provenance_source_channel TEXT,
    provenance_source_tool TEXT,
    archived_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
```

### 2.2 `_archive_transcript_entries`（`session/storage.py:6972`）

- 接收 `entries: list[TranscriptEntry]`，按顺序插入
- 同一 compaction 的所有 entries 共享 `compaction_id` / `compaction_index`
- 已有 `original_entry_id` 字段

### 2.3 `search_transcript`（`session/storage.py:7303`）

```python
async def search_transcript(
    self,
    query: str,
    session_id: str | None = None,
    limit: int = 20,
    *,
    include_archived: bool = True,
) -> list[dict[str, Any]]:
```

- 仅支持 FTS 全文搜索
- 返回：`id, session_key, role, snippet, created_at, source`

### 2.4 `_format_chunk_for_llm`（`session/compaction.py:535`）

```python
def _format_chunk_for_llm(chunk: list[dict[str, Any]]) -> str:
    # 输出格式: "[role]: content"
    # 无 entry 编号
```

### 2.5 `call_compaction_llm` system prompt（`session/compaction.py:598`）

```
"You are a conversation compactor. Summarize the conversation concisely, "
"preserving key facts, decisions, open questions, and action items. "
"Write in the same language as the conversation. "
"Focus on recent context over older history."
```

---

## 3. Anchor 格式

### 3.1 在 Summary 中的表示

```text
用户确认了 P10 的 S 束从 4 维降为 3 维 [anchor:2:entry_005]
```

格式：`[anchor:<compaction_index>:<entry_anchor_id>]`

- `compaction_index`：该 session 第几次 compaction（整数，从 0 开始）
- `entry_anchor_id`：本次 compaction 内该 entry 的稳定标识

**不包含 session_id**：因为 anchor 出现在当前 session 的 summary 中，session_id 是隐含的。这使格式更短、更可读。

### 3.2 `entry_anchor_id` 方案选择

| 方案 | 值 | 优点 | 缺点 |
|------|-----|------|------|
| A | `original_entry_id`（整数） | 简单 | 模型在 compaction 输入中看不到此 ID |
| **B（选定）** | **`entry_<n>`（3 位零填充）** | **可读、可预测、prompt 友好** | 需要生成 |
| C | 随机 UUID | 稳定 | 冗长、不可读 |

**选定方案 B**：`entry_000`, `entry_001`, ...，按 `created_at` 排序的 0-based 顺序编号。

### 3.3 完整 Anchor 引用（工具调用时）

模型调用 `session_search` 时传入：
```
anchor = "2:entry_005"
```

系统自动补充当前 session_id（模型不需要知道 session_id）。

---

## 4. Schema 变更

### 4.1 迁移：新增 `compaction_anchor_id` 列

```sql
-- migration: add_compaction_anchor_id
ALTER TABLE compacted_transcript_entries
ADD COLUMN compaction_anchor_id TEXT;

CREATE INDEX IF NOT EXISTS idx_compacted_anchor_lookup
ON compacted_transcript_entries(session_id, compaction_index, compaction_anchor_id);
```

- 可为 NULL（向后兼容旧数据）
- 组合查找：`(session_id, compaction_index, compaction_anchor_id)`

### 4.2 迁移：`session_summaries` 新增 `extracted_anchors`

```sql
ALTER TABLE session_summaries
ADD COLUMN extracted_anchors TEXT;  -- JSON array
```

格式：
```json
[
  {"compaction_index": 2, "entry_anchor_id": "entry_005", "context": "用户确认了 S 束降维"}
]
```

- 解析 summary 时自动提取
- 可选存储（不解析也不影响功能）

---

## 5. Compaction 侧改动

### 5.1 `_format_chunk_for_llm` — 添加 entry 编号

```python
def _format_chunk_for_llm(
    chunk: list[dict[str, Any]],
    *,
    anchor_base: int = 0,
    include_anchors: bool = False,
) -> str:
    """Format conversation entries into readable text for the compaction LLM."""
    lines: list[str] = []
    for idx, entry in enumerate(chunk):
        role = entry.get("role", "unknown")
        content = _summarize_if_envelope(str(entry.get("content") or ""))
        if include_anchors:
            header = f"[entry_{anchor_base + idx:03d} | {role}]"
        else:
            header = f"[{role}]"
        rendered_parts = [f"{header}: {content}"]
        tool_summary = _summarize_tool_calls_for_llm(entry.get("tool_calls"))
        if tool_summary:
            rendered_parts.append(tool_summary)
        reasoning_content = entry.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            rendered_parts.append(
                "[assistant reasoning omitted from compaction input: "
                f"{len(reasoning_content)} chars]"
            )
        lines.append("\n".join(part for part in rendered_parts if part))
    return "\n\n".join(lines)
```

### 5.2 `call_compaction_llm` — 追加 anchor 指令

在 system prompt 后追加（仅当 `compaction_anchor` feature flag 开启时）：

```python
_ANCHOR_INSTRUCTION = (
    "\n\nThe conversation entries above are labeled [entry_NNN | role]. "
    "When your summary references a specific statement, decision, or tool output "
    "whose exact original wording might matter later, append an anchor:\n"
    "  [anchor:<compaction_index>:entry_NNN]\n"
    "Use anchors sparingly — only where a future reader might need to expand "
    "for full detail. Do not anchor every entry. Do not invent entry numbers."
)
```

`compaction_index` 由调用方传入（已知值）。

### 5.3 `_archive_transcript_entries` — 写入 anchor ID

```python
async def _archive_transcript_entries(
    self,
    *,
    node: SessionNode,
    entries: list[TranscriptEntry],
    compaction_id: str | None,
    compaction_index: int | None,
    anchor_enabled: bool = False,  # NEW
) -> None:
    if not entries:
        return
    archived_at = _now_ms()
    # 按 created_at 排序，生成稳定 anchor ID
    sorted_entries = sorted(entries, key=lambda e: (e.created_at or 0, e.id or 0))
    for idx, entry in enumerate(sorted_entries):
        entry_data = entry.model_dump(exclude={"id"})
        entry_data["session_id"] = node.session_id
        entry_data["session_key"] = node.session_key
        archive_data: dict[str, Any] = {
            "session_id": entry_data.pop("session_id"),
            "session_key": entry_data.pop("session_key"),
            "compaction_id": compaction_id,
            "compaction_index": compaction_index,
            "compaction_anchor_id": f"entry_{idx:03d}" if anchor_enabled else None,
            "original_entry_id": entry.id,
            **entry_data,
            "archived_at": archived_at,
        }
        # ... INSERT (unchanged)
```

### 5.4 Summary 生成后解析 anchors

```python
import re

_ANCHOR_PATTERN = re.compile(
    r"\[anchor:(?P<compaction_index>\d+):(?P<entry_anchor_id>entry_\d+)\]"
)

def extract_anchors_from_summary(summary_text: str) -> list[dict[str, Any]]:
    """Parse anchor references from a compaction summary."""
    anchors = []
    for m in _ANCHOR_PATTERN.finditer(summary_text):
        anchors.append({
            "compaction_index": int(m.group("compaction_index")),
            "entry_anchor_id": m.group("entry_anchor_id"),
        })
    return anchors
```

---

## 6. 检索侧改动

### 6.1 `search_transcript` 新增 anchor 模式

```python
@_serialized_read
async def search_transcript(
    self,
    query: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    *,
    include_archived: bool = True,
    anchor: str | None = None,  # NEW: "compaction_index:entry_anchor_id"
) -> list[dict[str, Any]]:
```

**约束**：
- `anchor` 和 `query` 至少提供一个
- 提供 `anchor` 时默认绑定当前 ToolContext 会话；仅显式跨会话查询需要 `session_id`

**Anchor 模式实现**：

```python
if anchor:
    if not session_id:
        raise ValueError("anchor lookup requires session_id")
    parts = anchor.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"invalid anchor format: {anchor!r}")
    compaction_index_str, entry_anchor_id = parts
    try:
        compaction_index = int(compaction_index_str)
    except ValueError:
        raise ValueError(f"invalid compaction_index in anchor: {anchor!r}")

    sql = """
        SELECT id, session_key, role, created_at, content AS snippet
        FROM compacted_transcript_entries
        WHERE session_id = ?
          AND compaction_index = ?
          AND compaction_anchor_id = ?
        ORDER BY created_at
        LIMIT ?
    """
    params = [session_id, compaction_index, entry_anchor_id, limit]
    async with self.conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        r["source"] = "archived"
        r["anchor"] = anchor
    return results
```

### 6.2 工具层暴露

`session_search` 工具 schema 新增可选参数：

```json
{
  "anchor": {
    "type": "string",
    "description": "Exact anchor reference from a compaction summary, format: '<compaction_index>:<entry_anchor_id>'. Defaults to the current session; explicit cross-session lookup requires session_id."
  }
}
```

### 6.3 System Prompt 告知模型

在 compaction summary 注入位置追加（仅当 summary 含 anchor 时）：

```text
This summary contains [anchor:N:entry_NNN] references. If you need the exact
original text behind a statement, call session_search with
anchor="N:entry_NNN". The current session is selected automatically.
Only use anchors explicitly present in the summary. Do not guess anchor values.
```

---

## 7. Feature Flag

```toml
[memory.experimental]
compaction_anchor = false  # default off
```

关闭时：
- `_format_chunk_for_llm` 不输出 entry 编号
- `call_compaction_llm` 不追加 anchor 指令
- `_archive_transcript_entries` 不写 `compaction_anchor_id`
- `search_transcript` 不接受 `anchor` 参数（忽略）
- System prompt 不提及 anchor

---

## 8. 降级与边界

| 场景 | 行为 |
|------|------|
| summary 无 anchor | 行为与 L0 完全一致 |
| anchor 格式错误 | 返回清晰错误信息，不崩溃 |
| anchor 指向已删除 entry | 返回空列表 |
| 旧数据无 `compaction_anchor_id` | anchor 查找返回空；FTS 仍可用 |
| feature flag 关闭 | 全链路 no-op |
| 多 chunk compaction | 每个 chunk 独立编号（anchor_base 累加） |
| LLM 生成了错误 anchor | 解析时忽略不存在的 entry_id |

---

## 9. 数据流全景

```
Compaction 触发
  │
  ├─ _format_chunk_for_llm(include_anchors=True)
  │     → "[entry_000 | user]: ..."
  │     → "[entry_001 | assistant]: ..."
  │
  ├─ call_compaction_llm(system + _ANCHOR_INSTRUCTION)
  │     → summary: "用户确认了 X [anchor:2:entry_005]"
  │
  ├─ extract_anchors_from_summary(summary)
  │     → [{"compaction_index": 2, "entry_anchor_id": "entry_005"}]
  │
  ├─ _archive_transcript_entries(anchor_enabled=True)
  │     → 每个 entry 写入 compaction_anchor_id = "entry_000", "entry_001", ...
  │
  └─ rewrite_compacted_session(summary + extracted_anchors)
        → session_summaries.extracted_anchors = JSON

后续推理中模型需要展开：
  │
  ├─ 模型看到 summary 中 [anchor:2:entry_005]
  ├─ 调用 session_search(anchor="2:entry_005")
  └─ 返回原始 entry 全文（content, role, tool_calls, created_at）
```

---

## 10. 验收标准（DoD）

- [ ] Schema 迁移：`compaction_anchor_id` 列 + 索引
- [ ] Schema 迁移：`session_summaries.extracted_anchors` 列
- [ ] `_format_chunk_for_llm` 支持 `include_anchors` + `anchor_base`
- [ ] `call_compaction_llm` 条件追加 `_ANCHOR_INSTRUCTION`
- [ ] `_archive_transcript_entries` 写入 `compaction_anchor_id`
- [ ] `extract_anchors_from_summary` 解析函数
- [ ] `search_transcript` 支持 `anchor` 参数精确查找
- [ ] `session_search` 工具 schema 暴露 `anchor`
- [ ] System prompt 条件告知模型 anchor 用法
- [ ] Feature flag `[memory.experimental.compaction_anchor]`
- [ ] 单元测试：anchor 解析、精确查找、格式错误、空结果
- [ ] 集成测试：完整 compaction → anchor → 展开 流程

---

## 11. 与 L1/L2 的关系

D12 与约束感知 memory 是**正交**的：
- D12 解决**原始信息可展开性**（compaction 维度）
- L1/L2 解决**检索时按推理需求组织**（memory 维度）

未来交汇点：
- L2 检索结果如果来自 compacted transcript，Provenance Marker 可携带 anchor
- L3 充分性检查可基于 anchor 验证"摘要是否保留了足够信息"

---

## 12. 待确认项

| # | 问题 | 默认 |
|---|------|------|
| 1 | `entry_anchor_id` 用 `entry_<n>` 方案？ | 是 |
| 2 | anchor 查找是否允许跨 session？ | 默认仅当前会话；显式 session_id 遵循 owner-only 工具边界 |
| 3 | summary 中保留原始 `[anchor:...]` 文本？ | 是（模型可读可展开） |
| 4 | 多 chunk 时 anchor_base 如何累加？ | 按 chunk 顺序累加 |
