# L0: Archived Transcript Searchable — Implementation Spec

> Status: Implemented (commit 648628e6, 2026-07-29)
> Branch: `feature/constraint-aware-memory`
> Layer: 0 (infrastructure, always-on)
> Depends on: none
> Blocks: L1 (constraint annotation), L2 (constraint routing), L3 (sufficiency check)

## 1. Objective

Make every transcript entry that was ever written in a session full-text searchable — including entries that have been moved into the `compacted_transcript_entries` archive table during compaction.

Currently `search_transcript()` only queries `transcript_entries` (the active table). After compaction, archived entries are moved to `compacted_transcript_entries` which has **no FTS index and no search path**. The data exists but is unreachable by any tool.

After this work, `search_transcript()` — and therefore the `session_search` tool — returns results from both the active transcript and the archive, with source provenance clearly marked.

This is **not** a toggleable feature; it closes a capability gap. Privacy is handled through model alignment, not by hiding history (see DESIGN.md §12).

## 2. Scope

### 2.1 In scope

- Add FTS5 virtual table `compacted_transcript_fts` backed by `compacted_transcript_entries`.
- Add `INSERT`/`UPDATE`/`DELETE` triggers so the FTS index stays in sync.
- Extend `search_transcript()` to search archived entries (default: on).
- Extend `search_transcript_like()` to search archived entries (default: on).
- Backfill existing archived rows into the new FTS index on migration.
- Idempotent, non-blocking migration.
- Return contract extended with `"source"` provenance key.

### 2.2 Out of scope for L0

- Ranking fusion between active and archived results (simple strategy: active first, then archived fill).
- Cross-table duplicate suppression (a row is either active OR archived, never both — enforced by `rewrite_compacted_session()` which DELETEs from active after INSERT into archive).
- Changing `memory_search --source sessions` behavior (it uses `get_transcript()` which reads active-only; changing it is a separate decision tracked in DESIGN.md §11).
- Exposing new parameters in the `session_search` tool schema (the tool gets archived results automatically via the default parameter).

## 3. Data Model Changes

### 3.1 New FTS5 virtual table

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS compacted_transcript_fts
USING fts5(content, content=compacted_transcript_entries, content_rowid=id)
```

Mirrors the existing `transcript_fts` pattern (storage.py line ~326):
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts
USING fts5(content, content=transcript_entries, content_rowid=id)
```

### 3.2 New triggers

Follow the exact pattern of `_CREATE_FTS_TRIGGER_INSERT` / `_CREATE_FTS_TRIGGER_DELETE` / `_CREATE_FTS_TRIGGER_UPDATE` (storage.py lines ~332-345):

```sql
-- Insert trigger
CREATE TRIGGER IF NOT EXISTS compacted_transcript_fts_ai
AFTER INSERT ON compacted_transcript_entries BEGIN
    INSERT INTO compacted_transcript_fts(rowid, content)
    VALUES (new.id, new.content);
END;

-- Delete trigger
CREATE TRIGGER IF NOT EXISTS compacted_transcript_fts_ad
AFTER DELETE ON compacted_transcript_entries BEGIN
    INSERT INTO compacted_transcript_fts(compacted_transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

-- Update trigger
CREATE TRIGGER IF NOT EXISTS compacted_transcript_fts_au
AFTER UPDATE ON compacted_transcript_entries BEGIN
    INSERT INTO compacted_transcript_fts(compacted_transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO compacted_transcript_fts(rowid, content)
    VALUES (new.id, new.content);
END;
```

### 3.3 No new covering index needed

The FTS5 `rowid` lookups join back to `compacted_transcript_entries` via its primary key (`id`). Session-filtered queries use the existing `idx_compacted_transcript_session_id` index.

## 4. Migration Strategy

### 4.1 New migration method

```python
async def _migrate_compacted_transcript_fts(self) -> None:
    """Idempotently create FTS5 index for archived transcript entries."""
```

Called from `_create_schema()` after `_migrate_memory_durable_receipt_coverage_columns()` (the last migration in the current chain, line ~1549).

### 4.2 Idempotency

- `CREATE VIRTUAL TABLE IF NOT EXISTS` — no-op if table exists.
- `CREATE TRIGGER IF NOT EXISTS` — no-op if triggers exist.
- Backfill guarded by checking whether the FTS table has any rows:

```python
async with self._conn.execute(
    "SELECT COUNT(*) FROM compacted_transcript_fts"
) as cur:
    row = await cur.fetchone()
fts_count = row[0] if row else 0

if fts_count == 0:
    # Backfill from archive
    await self._conn.execute(
        "INSERT INTO compacted_transcript_fts(rowid, content) "
        "SELECT id, content FROM compacted_transcript_entries "
        "WHERE content IS NOT NULL"
    )
    await self._conn.commit()
```

### 4.3 Backfill performance

- The archive table is append-only (rows are inserted during compaction, never updated in normal operation).
- Typical size: much smaller than active transcript (only compacted prefixes).
- Synchronous backfill during init is acceptable. If it becomes problematic for very large databases (>100k archived rows), a future iteration can move it to the `prepare_usage_backfill_indexes()` background worker pattern.

### 4.4 Ordering within `_create_schema()`

```
1. CREATE TABLE compacted_transcript_entries  (already exists, line ~1161)
2. CREATE indexes on compacted_transcript_entries  (already exists, lines ~1162-1165)
3. CREATE VIRTUAL TABLE transcript_fts  (already exists)
4. CREATE triggers for transcript_fts  (already exists)
5. ... other migrations ...
6. _migrate_compacted_transcript_fts()  ← NEW
   a. CREATE VIRTUAL TABLE compacted_transcript_fts
   b. CREATE triggers (ai, ad, au)
   c. Backfill if empty
```

Triggers are created BEFORE backfill so that any concurrent insert (unlikely during init, but defensive) is captured.

## 5. API Changes

### 5.1 `SessionStorage.search_transcript`

Current signature (line ~4849):

```python
@_serialized_read
async def search_transcript(
    self,
    query: str,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
```

New signature:

```python
@_serialized_read
async def search_transcript(
    self,
    query: str,
    session_id: str | None = None,
    limit: int = 20,
    *,
    include_archived: bool = True,
) -> list[dict[str, Any]]:
```

Behavior:
1. Always search active transcript first (existing logic, unchanged).
2. Tag each active result with `"source": "active"`.
3. If `include_archived` is True and `len(active_results) < limit`:
   - Call `_search_compacted_transcript(query, session_id, limit - len(active_results))`.
   - Tag each archived result with `"source": "archived"`.
   - Append to results.
4. Return `results[:limit]`.

### 5.2 `SessionStorage.search_transcript_like`

Current signature (line ~4966). Add `include_archived: bool = True` keyword-only parameter with the same fill strategy.

### 5.3 New private helper

```python
@_serialized_read
async def _search_compacted_transcript(
    self,
    query: str,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Full-text search across archived (compacted) transcript entries."""
```

Note: Since this is called from within `search_transcript` which already holds the serialized read lock, it should NOT have its own `@_serialized_read` decorator. Instead, make it a plain async method that assumes the caller holds the lock. Alternatively, inline the SQL in `search_transcript` to avoid the question entirely.

**Decision: inline the archived SQL in `search_transcript` to avoid re-entrancy issues with `@_serialized_read`.**

### 5.4 Return dict contract

```python
{
    "id": int,              # row id in source table
    "session_key": str,
    "role": str,            # "user" | "assistant" | "tool" | ...
    "snippet": str,         # FTS5 snippet with >>> <<< markers
    "created_at": int,      # epoch ms
    "source": "active" | "archived",  # NEW
}
```

The `"source"` key is additive. Existing callers that ignore unknown keys are unaffected.

## 6. SQL Queries

### 6.1 Active search (unchanged)

```sql
SELECT t.id, t.session_key, t.role, t.created_at,
       snippet(transcript_fts, 0, '>>>', '<<<', '...', 48) AS snippet
FROM transcript_fts f
JOIN transcript_entries t ON f.rowid = t.id
WHERE f.content MATCH ?
  [AND t.session_id = ?]
ORDER BY f.rank
LIMIT ?
```

### 6.2 Archived search (new)

```sql
SELECT c.id, c.session_key, c.role, c.created_at,
       snippet(compacted_transcript_fts, 0, '>>>', '<<<', '...', 48) AS snippet
FROM compacted_transcript_fts f
JOIN compacted_transcript_entries c ON f.rowid = c.id
WHERE f.content MATCH ?
  [AND c.session_id = ?]
ORDER BY f.rank
LIMIT ?
```

### 6.3 CJK consideration

The existing `sanitize_fts_query()` (line ~4832) strips non-alphanumeric characters and wraps tokens in quotes. This means CJK text is already handled poorly in the active search (SQLite's `unicode61` tokenizer does not segment CJK). The archived search inherits the same limitation. This is a known issue tracked separately; L0 does not make it worse.

## 7. Tool Layer Changes

### 7.1 `session_search` tool (session_search.py)

No parameter changes needed. The tool calls:
```python
results = await active_storage.search_transcript(
    query=query, session_id=session_id, limit=limit,
)
```

Since `include_archived` defaults to `True`, archived results appear automatically.

### 7.2 Description update

```python
description=(
    "Full-text search across persisted session transcripts, including entries "
    "archived during compaction. Returns matching excerpts with session context. "
    "Use when exact prior chat wording, transcript context, or code snippets "
    "from persisted sessions are needed. Ordinary recall should start with "
    "memory_search, which defaults to curated memory source files. To search "
    "indexed session snippets through memory_search, use source=sessions or "
    "source=all. session_search does not search MEMORY.md or memory/**/*.md."
),
```

### 7.3 Result serialization update

Add `"source"` to the returned JSON:

```python
{
    "session_key": r["session_key"],
    "role": r["role"],
    "snippet": r["snippet"],
    "created_at": r["created_at"],
    "source": r.get("source", "active"),  # backwards-safe
}
```

## 8. Backwards Compatibility

| Surface | Impact |
|---------|--------|
| `SessionStorage.search_transcript` | New keyword-only param with default; existing positional callers unaffected |
| `SessionStorage.search_transcript_like` | Same |
| `session_search` tool schema | No new parameters; results may include archived hits |
| Return dict | Additive `"source"` key; callers ignoring extra keys are safe |
| Database file | New FTS table + triggers created on next startup; no schema version bump needed (IF NOT EXISTS pattern) |
| Session source sync (`SessionSourceIndexer`) | Unchanged; still active-only via `get_transcript()` |
| `sessions_history` tool | Unchanged; reads active transcript only |

## 9. Testing Strategy

### 9.1 Unit tests (new file or extend existing)

Location: `tests/session/test_storage_compacted_fts.py`

| Test | Assertion |
|------|-----------|
| `test_archived_entry_searchable_after_compaction` | Compact a session, search for a term only in the archived prefix → hit returned with `source="archived"` |
| `test_active_entry_still_searchable` | Term in active tail → hit with `source="active"` |
| `test_session_id_filter_archived` | Two sessions compacted; search with session_id → only matching session's archived hits |
| `test_include_archived_false` | Same setup, `include_archived=False` → no archived hits |
| `test_fts_trigger_insert` | Manually INSERT into `compacted_transcript_entries` → searchable immediately |
| `test_fts_trigger_delete` | DELETE an archived row → no longer searchable |
| `test_fts_trigger_update` | UPDATE content of archived row → new content searchable, old not |
| `test_null_content_skipped` | INSERT with `content=NULL` → no FTS error, not searchable |
| `test_migration_idempotent` | Run `_create_schema()` twice → no error, no duplicate rows |
| `test_backfill_populates_existing` | Pre-populate archive without FTS, run migration → FTS populated |

### 9.2 Integration test

- Full compaction cycle: create session → add entries → trigger compaction → verify `session_search` tool returns archived hits.

### 9.3 Regression

- Existing `test_storage.py` tests must pass unchanged (active search behavior is preserved).

## 10. Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Backfill slow on huge archive (>100k rows) | Low (typical <10k) | Synchronous for now; move to background worker if needed |
| FTS index disk overhead | Low | FTS5 index is typically 30-50% of source text size |
| `content` is NULL for some rows | Medium (tool results may have NULL content) | `WHERE content IS NOT NULL` in backfill; triggers insert NULL which FTS5 handles gracefully (indexes empty string) |
| Re-entrancy with `@_serialized_read` | Medium | Inline archived SQL in `search_transcript` rather than calling a separate decorated method |
| CJK search still broken | Known | Not made worse; separate fix tracked |
| Concurrent compaction during migration | Very low | Migration runs during `_create_schema()` before any session activity |

## 11. Files to Touch

| File | Change |
|------|--------|
| `src/opensquilla/session/storage.py` | DDL constants (`_CREATE_COMPACTED_TRANSCRIPT_FTS`, 3 triggers), `_migrate_compacted_transcript_fts()`, extend `search_transcript()` and `search_transcript_like()` |
| `src/opensquilla/tools/builtin/session_search.py` | Update description string; add `"source"` to result serialization |
| `tests/session/test_storage_compacted_fts.py` | New test file (10 tests) |
| `docs/proposals/constraint-aware-memory/DESIGN.md` | Update L0 status to "implemented" |

## 12. Definition of Done

- [x] `storage.py` contains `_CREATE_COMPACTED_TRANSCRIPT_FTS` and three trigger constants.
- [x] `_initialize_schema()` calls `_migrate_compacted_transcript_fts()` after existing migrations.
- [x] `search_transcript(include_archived=True)` returns archived hits with `"source"` key.
- [x] `search_transcript_like(include_archived=True)` same.
- [x] `session_search` tool description updated; result includes `"source"`.
- [x] All 11 unit tests pass (10 spec tests + 1 LIKE search extension, 2.06s).
- [x] Existing test suite passes (374 passed, 1 pre-existing failure unrelated to L0, 0 regressions).
- [x] Commit `648628e6` on `feature/constraint-aware-memory`, NOT merged to `main`.

## 13. Implementation Order

1. DDL constants + triggers (pure additive, no behavior change)
2. `_migrate_compacted_transcript_fts()` + call site in `_create_schema()`
3. Extend `search_transcript()` with archived fallback
4. Extend `search_transcript_like()` with archived fallback
5. Update `session_search.py` description + result serialization
6. Write tests
7. Run full test suite
8. Commit
