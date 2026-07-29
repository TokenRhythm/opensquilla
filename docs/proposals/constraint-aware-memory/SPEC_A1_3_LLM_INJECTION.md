# SPEC_A1-3: LLM 注入 Store 直接索引路径

> **Status**: 实施 v1.0
> **Date**: 2026-07-29
> **Scope**: 将 A1 分层升级分类接入 `store.index_file()` 直接索引路径
> **Depends on**: A1 (`3cf54191`) — `classify_constraint()` async 函数已实现

---

## 1. 问题陈述

A1 实现了 `classify_constraint()` async 函数（分层升级：启发式优先 → LLM 升级 → 回退），
但 `store.py` line 838 仍调用 `classify_constraint_sync()`（纯启发式，无 LLM）。

**后果**：直接索引路径（用户手动编辑 MEMORY.md 后触发 reindex）的 chunk 置信度
恒 < 0.6 → L2 boost 阈值 0.6 导致 boost 恒为 1.0 → L2 路由对非 flush 内容无效。

## 2. 设计

### 2.1 架构变更

```
当前：
  index_file() → BEGIN IMMEDIATE → classify_constraint_sync() → UPDATE → COMMIT

目标：
  index_file() → classify_constraint(async, llm_call)  ← 事务外（与 embeddings 并行）
              → BEGIN IMMEDIATE → UPDATE with pre-computed types → COMMIT
```

**关键约束**：LLM 网络 I/O 必须在事务外执行（参照 embeddings 模式 line 745-784），
否则 `BEGIN IMMEDIATE` 会持锁数秒阻塞其他写入。

### 2.2 LLM 调用适配器

`constraint_classifier.LlmCallFn = Callable[[str], Awaitable[str]]`

适配器在 `manager.py` 中创建，从 provider 对象构建：

```python
def _make_constraint_llm_call(provider: Any) -> LlmCallFn | None:
    """Create a simple text-in/text-out LLM call from a provider."""
    if provider is None:
        return None
    async def _call(prompt: str) -> str:
        from opensquilla.provider.types import ChatConfig, ContentBlockText, Message
        messages = [Message(role="user", content=[ContentBlockText(text=prompt)])]
        # Prefer complete() (simpler), fall back to chat() (streaming)
        complete = getattr(provider, "complete", None)
        if callable(complete):
            resp = await complete(messages=messages, max_tokens=100)
            return getattr(resp, "content", None) or getattr(resp, "text", "") or ""
        chat = getattr(provider, "chat", None)
        if not callable(chat):
            raise TypeError("Provider has no complete() or chat()")
        config = ChatConfig(max_tokens=100)
        stream = chat(messages, config=config)
        chunks = []
        async for event in stream:
            text = getattr(event, "text", "") or ""
            if text and getattr(event, "kind", "") == "text_delta":
                chunks.append(text)
        return "".join(chunks)
    return _call
```

**设计决策**：
- `max_tokens=100`：分类响应是单个词（fact/decision/...）
- 无 usage tracking：best-effort 分类，非用户可见
- 无 streaming 复杂性：只收集 text_delta
- 优先 `complete()`：更简单，避免 streaming 开销

### 2.3 接线链路

```
boot.py build_services()
  └→ build_memory_managers(config, agent_ids, provider_selector=provider_selector)
       └→ provider = provider_selector.resolve()  (once, best-effort)
       └→ llm_call = _make_constraint_llm_call(provider)  (if provider available)
       └→ LongTermMemoryStore(..., constraint_llm_call=llm_call)
            └→ index_file() uses classify_constraint(text, llm_call=self._constraint_llm_call)
```

### 2.4 降级链

| 条件 | 行为 | 等效于 |
|------|------|--------|
| provider_selector = None | llm_call = None → classify_constraint_sync() | 当前行为 |
| provider.resolve() 失败 | llm_call = None → classify_constraint_sync() | 当前行为 |
| LLM 调用失败 | classify_constraint() 回退到启发式 | A1 已实现 |
| 启发式 conf >= 0.6 | 跳过 LLM（A1 已实现） | A1 已实现 |

**保证**：实验功能永远不会让索引行为比当前更差。

### 2.5 事务结构变更

```python
# index_file() 中，在 BEGIN IMMEDIATE 之前：
constraint_results: list[tuple[ConstraintType, float | None]] | None = None
if self._constraint_annotation_enabled:
    constraint_results = []
    for _cid, _p, _src, _sl, _el, _h, _mdl, txt, _ts in chunk_records:
        try:
            ct, conf = await classify_constraint(txt, llm_call=self._constraint_llm_call)
        except Exception:
            ct, conf = classify_constraint_sync(txt)
        constraint_results.append((ct, conf))

# 事务内（替换原 line 832-844）：
if self._constraint_annotation_enabled and constraint_results:
    for i, (cid, ...) in enumerate(chunk_records):
        ct, conf = constraint_results[i]
        await self._db.execute(
            "UPDATE chunks SET constraint_type=?, constraint_confidence=? WHERE id=?",
            (ct.value, conf, cid),
        )
```

## 3. 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/store.py` | `__init__` 加 `constraint_llm_call` 参数；`index_file` 事务外计算约束类型 |
| `src/opensquilla/memory/manager.py` | `build_memory_managers` 加 `provider_selector` kwarg；创建适配器 |
| `src/opensquilla/gateway/boot.py` | 传 `provider_selector` 给 `build_memory_managers` |
| `tests/test_memory/test_constraint_annotation.py` | 新增 A1-3 集成测试 |

## 4. 测试计划

1. **Unit**: store with mock llm_call → verify async classify_constraint used
2. **Unit**: store without llm_call → verify sync fallback (current behavior)
3. **Unit**: llm_call raises → verify graceful fallback to heuristic
4. **Integration**: manager builds store with provider → verify llm_call wired
5. **Regression**: all existing constraint tests pass unchanged
