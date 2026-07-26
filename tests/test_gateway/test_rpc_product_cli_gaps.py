from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import (
    ADMIN_SCOPE,
    METHOD_SCOPES,
    READ_SCOPE,
    REMOTE_OPERATOR_SCOPES,
    WRITE_SCOPE,
    authorize_call,
)
from opensquilla.memory.types import MemorySearchResult, MemorySource, SearchIntent
from opensquilla.search.registry import register_provider
from opensquilla.search.types import SearchProviderError, SearchProviderSpec, SearchResult
from opensquilla.session.models import MemoryDurableReceipt
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin.web import configure_search, run_web_discover_payload


@dataclass
class FakeMemoryManager:
    workspace_dir: Any
    memory_config: Any = field(default_factory=SimpleNamespace)
    search_calls: list[tuple[str, Any, Any]] | None = None
    status_payload: dict[str, Any] | None = None
    sync_calls: list[tuple[str, bool]] = field(default_factory=list)
    store: Any = None

    async def search(self, query: str, opts: Any, *, intent: Any) -> list[MemorySearchResult]:
        if self.search_calls is None:
            self.search_calls = []
        self.search_calls.append((query, opts, intent))
        return [
            MemorySearchResult(
                chunk_id="chunk-1",
                path="memory/a.md",
                source=MemorySource.memory,
                start_line=1,
                end_line=2,
                snippet="alpha snippet",
                score=0.9,
                vector_score=0.8,
                text_score=0.7,
                chunk_hash="hash-1",
                citation="memory/a.md#L1-L2",
            )
        ]

    async def status(self) -> dict[str, Any]:
        return self.status_payload or {
            "agent_id": "main",
            "file_count": 0,
            "chunk_count": 0,
            "total_size_bytes": 0,
            "source_counts": {},
            "vec_available": False,
            "fts_available": True,
            "degraded": [],
        }

    async def sync(self, *, reason: str = "manual", force: bool = False) -> None:
        self.sync_calls.append((reason, force))


class FakeStore:
    def __init__(self) -> None:
        self.rebuild_calls = 0

    async def rebuild(self) -> None:
        self.rebuild_calls += 1


def _ctx(**kwargs: Any) -> RpcContext:
    defaults: dict[str, Any] = {"conn_id": "test", "config": GatewayConfig()}
    defaults.update(kwargs)
    return RpcContext(**defaults)


@pytest.fixture(autouse=True)
def _reset_search_config():
    configure_search("duckduckgo", max_results=5)
    yield
    configure_search("duckduckgo", max_results=5)


@pytest.mark.asyncio
async def test_new_product_rpc_methods_are_classified_read_scope():
    dispatcher = get_dispatcher()
    for method in (
        "memory.list",
        "memory.search",
        "memory.show",
        "providers.status",
        "search.status",
    ):
        assert METHOD_SCOPES[method] == READ_SCOPE
        entry = dispatcher.get_entry(method)
        assert entry is not None
        assert entry.required_scope == READ_SCOPE


@pytest.mark.asyncio
async def test_memory_admin_rpc_methods_are_classified_admin_scope_and_deny_read_only():
    dispatcher = get_dispatcher()
    for method in (
        "memory.index",
        "memory.raw_fallbacks.list",
        "memory.raw_fallbacks.show",
        "memory.repair.list",
        "memory.repair.run",
        "memory.repair.show",
    ):
        assert METHOD_SCOPES[method] == ADMIN_SCOPE
        entry = dispatcher.get_entry(method)
        assert entry is not None
        assert entry.required_scope == ADMIN_SCOPE

    read_only = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    manager = FakeMemoryManager(workspace_dir="/tmp/memory")
    res = await dispatcher.dispatch(
        "r1",
        "memory.index",
        {"agentId": "main", "force": True},
        _ctx(principal=read_only, memory_managers={"main": manager}),
    )

    assert res.error is not None
    assert res.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_search_query_is_classified_write_scope_and_denies_read_only():
    dispatcher = get_dispatcher()
    entry = dispatcher.get_entry("search.query")
    assert METHOD_SCOPES["search.query"] == WRITE_SCOPE
    assert entry is not None
    assert entry.required_scope == WRITE_SCOPE

    read_only = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    res = await dispatcher.dispatch(
        "r1",
        "search.query",
        {"query": "hello"},
        _ctx(principal=read_only),
    )

    assert res.error is not None
    assert res.error.code == "UNAUTHORIZED"


def test_sessions_delete_is_write_scope_and_allows_remote_operator():
    """Regression for #357 / #307: deleting a session from the Web UI must work for
    a no-auth remote operator.

    On the default Docker bind (``OPENSQUILLA_LISTEN=0.0.0.0``) the gateway is not a
    loopback bind, so even a ``127.0.0.1`` peer is not the local owner and is granted
    only :data:`REMOTE_OPERATOR_SCOPES` (read/write/approvals, no admin). While
    ``sessions.delete`` was admin-gated, every such delete failed with
    ``UNAUTHORIZED`` and the UI showed "Failed to delete session". It is now
    write-scoped like its sibling destructive ops (reset/truncate), so a remote
    operator is authorized.
    """
    dispatcher = get_dispatcher()
    assert METHOD_SCOPES["sessions.delete"] == WRITE_SCOPE
    entry = dispatcher.get_entry("sessions.delete")
    assert entry is not None
    assert entry.required_scope == WRITE_SCOPE

    # The no-auth remote-operator scope set never carries admin, which is exactly
    # why the old admin gate broke deletion on non-loopback binds.
    assert ADMIN_SCOPE not in REMOTE_OPERATOR_SCOPES

    allowed, missing = authorize_call(
        "sessions.delete", entry.required_scope, "operator", REMOTE_OPERATOR_SCOPES
    )
    assert allowed is True
    assert missing is None

    # And the old classification would still be denied for the same caller, proving
    # the admin gate was the cause rather than some unrelated wiring.
    denied, missing_admin = authorize_call(
        "sessions.delete", ADMIN_SCOPE, "operator", REMOTE_OPERATOR_SCOPES
    )
    assert denied is False
    assert missing_admin == ADMIN_SCOPE


@pytest.mark.asyncio
async def test_memory_search_uses_admin_intent_and_returns_wire_rows(tmp_path):
    manager = FakeMemoryManager(workspace_dir=tmp_path)
    res = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha", "agentId": "main", "limit": 3, "minScore": 0.0},
        _ctx(memory_managers={"main": manager}),
    )

    assert res.error is None, res.error
    assert res.payload["count"] == 1
    assert res.payload["results"][0]["chunkId"] == "chunk-1"
    assert manager.search_calls is not None
    assert manager.search_calls[0][2] is SearchIntent.ADMIN
    assert manager.search_calls[0][1].max_results == 3
    assert manager.search_calls[0][1].min_score == 0.0


@pytest.mark.asyncio
async def test_memory_search_accepts_source_filter(tmp_path):
    manager = FakeMemoryManager(workspace_dir=tmp_path)
    res = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha", "agentId": "main", "source": "sessions"},
        _ctx(memory_managers={"main": manager}),
    )

    assert res.error is None, res.error
    assert manager.search_calls is not None
    assert manager.search_calls[0][1].source is MemorySource.sessions


@pytest.mark.asyncio
async def test_memory_search_rejects_invalid_source_filter(tmp_path):
    res = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha", "agentId": "main", "source": "raw"},
        _ctx(memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)}),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_memory_search_defaults_to_bundled_query_shape(tmp_path):
    manager = FakeMemoryManager(workspace_dir=tmp_path)
    res = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha", "agentId": "main"},
        _ctx(memory_managers={"main": manager}),
    )

    assert res.error is None, res.error
    assert manager.search_calls is not None
    opts = manager.search_calls[0][1]
    assert opts.max_results == 6
    assert opts.min_score == 0.35
    assert opts.source is MemorySource.memory


@pytest.mark.asyncio
async def test_memory_search_blank_or_null_source_uses_curated_default(tmp_path):
    manager = FakeMemoryManager(workspace_dir=tmp_path)

    for source in (None, ""):
        res = await get_dispatcher().dispatch(
            "r1",
            "memory.search",
            {"query": "alpha", "agentId": "main", "source": source},
            _ctx(memory_managers={"main": manager}),
        )
        assert res.error is None, res.error

    assert manager.search_calls is not None
    assert manager.search_calls[0][1].source is MemorySource.memory
    assert manager.search_calls[1][1].source is MemorySource.memory


@pytest.mark.asyncio
async def test_memory_search_clamps_numeric_min_score_range(tmp_path):
    manager = FakeMemoryManager(workspace_dir=tmp_path)
    res = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha", "agentId": "main", "minScore": 2.0},
        _ctx(memory_managers={"main": manager}),
    )

    assert res.error is None, res.error
    assert manager.search_calls is not None
    assert manager.search_calls[0][1].min_score == 1.0


@pytest.mark.asyncio
async def test_memory_search_rejects_non_numeric_min_score(tmp_path):
    res = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha", "agentId": "main", "minScore": "bad"},
        _ctx(memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)}),
    )

    assert res.error is not None
    assert res.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_memory_search_reports_unavailable_and_missing_agent(tmp_path):
    unavailable = await get_dispatcher().dispatch(
        "r1",
        "memory.search",
        {"query": "alpha"},
        _ctx(),
    )
    missing = await get_dispatcher().dispatch(
        "r2",
        "memory.search",
        {"query": "alpha", "agentId": "ops"},
        _ctx(memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)}),
    )

    assert unavailable.error is not None
    assert unavailable.error.code == "UNAVAILABLE"
    assert missing.error is not None
    assert missing.error.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_memory_list_returns_curated_source_files_only(tmp_path):
    (tmp_path / "MEMORY.md").write_text("root\n", encoding="utf-8")
    memory_dir = tmp_path / "memory"
    archive_dir = memory_dir / "archive"
    hidden_dir = memory_dir / ".private"
    archive_dir.mkdir(parents=True)
    hidden_dir.mkdir(parents=True)
    (memory_dir / "a.md").write_text("one\ntwo\n", encoding="utf-8")
    (archive_dir / "x.md").write_text("curated\n", encoding="utf-8")
    (hidden_dir / "x.md").write_text("hidden\n", encoding="utf-8")

    res = await get_dispatcher().dispatch(
        "r1",
        "memory.list",
        {"agentId": "main"},
        _ctx(memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)}),
    )

    assert res.error is None, res.error
    paths = [row["path"] for row in res.payload["files"]]
    assert paths == ["MEMORY.md", "memory/a.md", "memory/archive/x.md"]
    assert res.payload["files"][1]["lineCount"] == 2


@pytest.mark.asyncio
async def test_memory_show_line_slice_and_truncation(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "a.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (memory_dir / "long.md").write_text("x" * 9001, encoding="utf-8")
    manager = FakeMemoryManager(workspace_dir=tmp_path)

    sliced = await get_dispatcher().dispatch(
        "r1",
        "memory.show",
        {"path": "memory/a.md", "fromLine": 2, "lines": 1},
        _ctx(memory_managers={"main": manager}),
    )
    truncated = await get_dispatcher().dispatch(
        "r2",
        "memory.show",
        {"path": "memory/long.md"},
        _ctx(memory_managers={"main": manager}),
    )

    assert sliced.error is None, sliced.error
    assert sliced.payload["content"] == "two"
    assert sliced.payload["fromLine"] == 2
    assert sliced.payload["lineCount"] == 1
    assert truncated.error is None, truncated.error
    assert truncated.payload["truncated"] is True
    assert len(truncated.payload["content"]) == 8000


@pytest.mark.asyncio
async def test_memory_show_rejects_traversal_excess_lines_and_big_unsliced_file(tmp_path):
    memory_dir = tmp_path / "memory"
    archive_dir = memory_dir / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "x.md").write_text("curated", encoding="utf-8")
    big = memory_dir / "big.md"
    big.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    manager = FakeMemoryManager(workspace_dir=tmp_path)

    traversal = await get_dispatcher().dispatch(
        "r1",
        "memory.show",
        {"path": "../secret.md"},
        _ctx(memory_managers={"main": manager}),
    )
    archive = await get_dispatcher().dispatch(
        "r2",
        "memory.show",
        {"path": "memory/archive/x.md"},
        _ctx(memory_managers={"main": manager}),
    )
    too_many_lines = await get_dispatcher().dispatch(
        "r3",
        "memory.show",
        {"path": "memory/big.md", "lines": 501},
        _ctx(memory_managers={"main": manager}),
    )
    sliced_big = await get_dispatcher().dispatch(
        "r4",
        "memory.show",
        {"path": "memory/big.md", "lines": 1},
        _ctx(memory_managers={"main": manager}),
    )
    too_big = await get_dispatcher().dispatch(
        "r5",
        "memory.show",
        {"path": "memory/big.md"},
        _ctx(memory_managers={"main": manager}),
    )

    assert traversal.error is not None
    assert traversal.error.code == "INVALID_REQUEST"
    assert archive.error is None, archive.error
    assert archive.payload["content"] == "curated"
    assert too_many_lines.error is not None
    assert too_many_lines.error.code == "INVALID_REQUEST"
    assert sliced_big.error is None, sliced_big.error
    assert sliced_big.payload["lineCount"] == 1
    assert sliced_big.payload["truncated"] is True
    assert too_big.error is not None
    assert too_big.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_doctor_memory_status_deep_redacts_paths_and_raw_errors(tmp_path):
    leaky_path = tmp_path / "state" / "memory.db"

    class LeakyBackend:
        async def health_check(self):
            return {
                "backend": "sqlite",
                "status": "error",
                "entryCount": 1,
                "sizeBytes": 2,
                "error": f"{leaky_path} failed",
            }

    manager = FakeMemoryManager(
        workspace_dir=tmp_path,
        status_payload={
            "agent_id": "main",
            "db_path": str(leaky_path),
            "workspace_dir": str(tmp_path),
            "memory_dir": str(tmp_path / "memory"),
            "file_count": 1,
            "chunk_count": 2,
            "total_size_bytes": 3,
            "source_counts": {"memory": 1},
            "vec_available": False,
            "fts_available": True,
            "degraded": [
                {
                    "component": "store",
                    "operation": "probe",
                    "error": f"{leaky_path} exploded",
                }
            ],
            "retrieval_mode": "fts_only",
        },
    )
    ctx = _ctx(memory_managers={"main": manager})
    ctx.memory_backend = LeakyBackend()

    res = await get_dispatcher().dispatch(
        "r1",
        "doctor.memory.status",
        {"agentId": "main", "deep": True},
        ctx,
    )

    assert res.error is None, res.error
    rendered = repr(res.payload)
    assert str(tmp_path) not in rendered
    assert "memory.db" not in rendered
    assert "exploded" not in rendered
    assert res.payload["vecAvailable"] is False
    assert res.payload["ftsAvailable"] is True
    assert res.payload["memorySafety"] == {"status": "ok"}
    assert res.payload["semanticMemory"] == {
        "status": "healthy",
        "repairBacklogCount": 0,
    }
    assert res.payload["degraded"][0] == {
        "component": "store",
        "operation": "probe",
        "error": "redacted",
    }


@pytest.mark.asyncio
async def test_doctor_memory_status_accepts_memory_backend_protocol_health(tmp_path):
    class ProtocolBackend:
        async def health(self):
            return {
                "backend": "sqlite",
                "status": "ok",
                "entryCount": 4,
                "sizeBytes": 5,
            }

    manager = FakeMemoryManager(workspace_dir=tmp_path)
    ctx = _ctx(memory_managers={"main": manager})
    ctx.memory_backend = ProtocolBackend()

    res = await get_dispatcher().dispatch(
        "r1",
        "doctor.memory.status",
        {"agentId": "main"},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["backend"] == "sqlite"
    assert res.payload["status"] == "ok"
    assert res.payload["entryCount"] == 4
    assert res.payload["sizeBytes"] == 5


@pytest.mark.asyncio
async def test_doctor_memory_status_unavailable_includes_split_health_fields():
    res = await get_dispatcher().dispatch(
        "memory-health-unavailable",
        "doctor.memory.status",
        {"agentId": "main"},
        _ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["backend"] == "none"
    assert res.payload["status"] == "unavailable"
    assert res.payload["memorySafety"] == {"status": "ok"}
    assert res.payload["semanticMemory"] == {
        "status": "healthy",
        "repairBacklogCount": 0,
    }


@pytest.mark.asyncio
async def test_doctor_memory_status_splits_safety_from_semantic_repair_health(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-1",
                scope="checkpoint",
                source_path="memory/checkpoints/s1.md",
                idempotency_key="checkpoint:s1",
                status="checkpoint_saved",
                reason="hash mismatch",
                created_at=now_ms,
            )
        )
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-1",
                scope="repair",
                source_path="memory/.raw_fallbacks/raw.md",
                idempotency_key="repair:raw.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=now_ms,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)

        res = await get_dispatcher().dispatch(
            "memory-health-split",
            "doctor.memory.status",
            {"agentId": "main"},
            _ctx(
                memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)},
                session_manager=session_manager,
            ),
        )

        assert res.error is None, res.error
        assert res.payload["memorySafety"]["status"] == "error"
        assert res.payload["semanticMemory"] == {
            "status": "degraded",
            "repairBacklogCount": 1,
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_doctor_memory_status_warns_for_large_semantic_repair_backlog(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        for idx in range(11):
            await storage.upsert_memory_durable_receipt(
                MemoryDurableReceipt(
                    session_key="agent:main:webchat:s1",
                    session_id="session-1",
                    scope="repair",
                    source_path=f"memory/.raw_fallbacks/raw-{idx}.md",
                    idempotency_key=f"repair:raw-{idx}.md",
                    status="repair_pending",
                    reason="parse_failed_archived",
                    created_at=now_ms,
                )
            )
        session_manager = FakeStorageRepairSessionManager(storage)

        res = await get_dispatcher().dispatch(
            "memory-health-warning",
            "doctor.memory.status",
            {"agentId": "main"},
            _ctx(
                memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)},
                session_manager=session_manager,
            ),
        )

        assert res.error is None, res.error
        assert res.payload["memorySafety"]["status"] == "ok"
        assert res.payload["semanticMemory"] == {
            "status": "warning",
            "repairBacklogCount": 11,
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_doctor_memory_status_health_is_agent_scoped(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-main",
                scope="checkpoint",
                source_path="memory/checkpoints/main.md",
                idempotency_key="checkpoint:main",
                status="checkpoint_failed",
                reason="archive_failed",
                created_at=now_ms,
            )
        )
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-main",
                scope="repair",
                source_path="memory/.raw_fallbacks/main.md",
                idempotency_key="repair:main.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=now_ms,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)

        res = await get_dispatcher().dispatch(
            "memory-health-agent-scope",
            "doctor.memory.status",
            {"agentId": "ops"},
            _ctx(
                memory_managers={"ops": FakeMemoryManager(workspace_dir=tmp_path)},
                session_manager=session_manager,
            ),
        )

        assert res.error is None, res.error
        assert res.payload["memorySafety"]["status"] == "ok"
        assert res.payload["semanticMemory"] == {
            "status": "healthy",
            "repairBacklogCount": 0,
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_doctor_memory_status_warns_for_oldest_pending_repair_age(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    old_ms = int(datetime.now(UTC).timestamp() * 1000) - (25 * 60 * 60 * 1000)
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-1",
                scope="repair",
                source_path="memory/.raw_fallbacks/old.md",
                idempotency_key="repair:old.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=old_ms,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)

        res = await get_dispatcher().dispatch(
            "memory-health-oldest-warning",
            "doctor.memory.status",
            {"agentId": "main"},
            _ctx(
                memory_managers={"main": FakeMemoryManager(workspace_dir=tmp_path)},
                session_manager=session_manager,
            ),
        )

        assert res.error is None, res.error
        assert res.payload["memorySafety"]["status"] == "ok"
        assert res.payload["semanticMemory"] == {
            "status": "warning",
            "repairBacklogCount": 1,
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_index_force_rebuilds_then_force_syncs(tmp_path):
    store = FakeStore()
    manager = FakeMemoryManager(workspace_dir=tmp_path, store=store)

    res = await get_dispatcher().dispatch(
        "r1",
        "memory.index",
        {"agentId": "main", "force": True},
        _ctx(memory_managers={"main": manager}),
    )

    assert res.error is None, res.error
    assert store.rebuild_calls == 1
    assert manager.sync_calls == [("manual", True)]
    assert res.payload["force"] is True


@pytest.mark.asyncio
async def test_memory_index_non_force_uses_ordinary_sync(tmp_path):
    store = FakeStore()
    manager = FakeMemoryManager(workspace_dir=tmp_path, store=store)

    res = await get_dispatcher().dispatch(
        "r1",
        "memory.index",
        {"agentId": "main"},
        _ctx(memory_managers={"main": manager}),
    )

    assert res.error is None, res.error
    assert store.rebuild_calls == 0
    assert manager.sync_calls == [("manual", False)]
    assert res.payload["force"] is False


@pytest.mark.asyncio
async def test_raw_fallback_admin_list_show_is_sidecar_only(tmp_path):
    raw_dir = tmp_path / "memory" / ".raw_fallbacks"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "raw.md"
    raw_path.write_text("# Raw flush (timeout)\nsecret transcript\n", encoding="utf-8")
    (tmp_path / "memory" / "a.md").write_text("curated\n", encoding="utf-8")
    manager = FakeMemoryManager(workspace_dir=tmp_path)

    listed = await get_dispatcher().dispatch(
        "r1",
        "memory.raw_fallbacks.list",
        {"agentId": "main"},
        _ctx(memory_managers={"main": manager}),
    )
    shown = await get_dispatcher().dispatch(
        "r2",
        "memory.raw_fallbacks.show",
        {"agentId": "main", "path": "memory/.raw_fallbacks/raw.md"},
        _ctx(memory_managers={"main": manager}),
    )
    traversal = await get_dispatcher().dispatch(
        "r3",
        "memory.raw_fallbacks.show",
        {"agentId": "main", "path": "memory/.raw_fallbacks/../a.md"},
        _ctx(memory_managers={"main": manager}),
    )

    assert listed.error is None, listed.error
    assert listed.payload["files"][0]["path"] == "memory/.raw_fallbacks/raw.md"
    assert shown.error is None, shown.error
    assert "secret transcript" in shown.payload["content"]
    assert traversal.error is not None
    assert traversal.error.code == "INVALID_REQUEST"


class FakeRepairSessionManager:
    def __init__(self) -> None:
        self.summary = SimpleNamespace(
            id=7,
            session_id="session-1",
            session_key="agent:main:thread-1",
            compaction_id="cmp-1",
            trigger_reason="gateway_auto_summarize",
            flush_receipt_status="degraded_forensic",
            removed_count=2,
            covered_through_id=9,
            created_at=123,
        )
        self.entries = [
            SimpleNamespace(
                id=1,
                message_id="m1",
                role="user",
                content="preimage fact",
                token_count=3,
                created_at=111,
            )
        ]
        self.status_updates: list[tuple[int | None, str]] = []

    async def list_degraded_compactions(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        assert agent_id == "main"
        assert limit > 0
        return [self.summary]

    async def get_compaction_preimage(self, summary: Any) -> list[Any]:
        assert summary is self.summary
        return list(self.entries)

    async def mark_compaction_repair_status(self, summary: Any, status: str) -> None:
        self.status_updates.append((getattr(summary, "id", None), status))


class FakeEmptyRepairSessionManager(FakeRepairSessionManager):
    async def list_degraded_compactions(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        assert agent_id == "main"
        assert limit > 0
        return []


class FakeStorageRepairSessionManager(FakeEmptyRepairSessionManager):
    def __init__(self, storage: Any) -> None:
        super().__init__()
        self.storage = storage


class FakeRepairFlushService:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Any], str, dict[str, Any]]] = []

    async def execute(self, transcript: list[Any], session_key: str, **kwargs: Any) -> Any:
        self.calls.append((list(transcript), session_key, dict(kwargs)))
        return SimpleNamespace(
            mode="llm",
            indexed_chunk_count=1,
            integrity_status="ok",
            output_coverage_status="ok",
            invalid_candidate_count=0,
            candidate_missing_ids=[],
            obligation_status="ok",
            obligation_missing_ids=[],
            to_dict=lambda: {
                "mode": "llm",
                "indexed_chunk_count": 1,
                "integrity_status": "ok",
                "output_coverage_status": "ok",
                "invalid_candidate_count": 0,
                "candidate_missing_ids": [],
                "obligation_status": "ok",
                "obligation_missing_ids": [],
            },
        )


@pytest.mark.asyncio
async def test_memory_repair_admin_lists_shows_and_runs_preimage():
    session_manager = FakeRepairSessionManager()
    flush_service = FakeRepairFlushService()
    ctx = _ctx(session_manager=session_manager, flush_service=flush_service)

    listed = await get_dispatcher().dispatch(
        "r1",
        "memory.repair.list",
        {"agentId": "main"},
        ctx,
    )
    shown = await get_dispatcher().dispatch(
        "r2",
        "memory.repair.show",
        {"agentId": "main", "sessionKey": "agent:main:thread-1", "compactionId": "cmp-1"},
        ctx,
    )
    repaired = await get_dispatcher().dispatch(
        "r3",
        "memory.repair.run",
        {"agentId": "main", "sessionKey": "agent:main:thread-1", "compactionId": "cmp-1"},
        ctx,
    )

    assert listed.error is None, listed.error
    assert listed.payload["items"][0]["flushReceiptStatus"] == "degraded_forensic"
    assert shown.error is None, shown.error
    assert shown.payload["entries"][0]["content"] == "preimage fact"
    assert shown.payload["preimageHash"]
    assert shown.payload["entryIdRange"] == [1, 1]
    assert repaired.error is None, repaired.error
    assert repaired.payload["results"][0]["status"] == "repaired"
    assert repaired.payload["results"][0]["preimageHash"] == shown.payload["preimageHash"]
    assert flush_service.calls[0][1] == "agent:main:thread-1"
    assert flush_service.calls[0][2]["message_window"] == 0
    assert session_manager.status_updates == [(7, "repaired")]


@pytest.mark.asyncio
async def test_memory_repair_admin_lists_shows_and_runs_raw_fallback(tmp_path):
    raw_dir = tmp_path / "memory" / ".raw_fallbacks"
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "raw.md"
    raw_path.write_text(
        "# Raw flush (timeout)\n\n"
        "user: remember raw repair marker RR-1\n"
        "assistant: acknowledged\n",
        encoding="utf-8",
    )
    session_manager = FakeEmptyRepairSessionManager()
    flush_service = FakeRepairFlushService()
    memory_manager = FakeMemoryManager(workspace_dir=tmp_path)
    ctx = _ctx(
        session_manager=session_manager,
        flush_service=flush_service,
        memory_managers={"main": memory_manager},
    )

    listed = await get_dispatcher().dispatch(
        "rr1",
        "memory.repair.list",
        {"agentId": "main"},
        ctx,
    )
    shown = await get_dispatcher().dispatch(
        "rr2",
        "memory.repair.show",
        {"agentId": "main", "path": "memory/.raw_fallbacks/raw.md"},
        ctx,
    )
    repaired = await get_dispatcher().dispatch(
        "rr3",
        "memory.repair.run",
        {"agentId": "main", "path": "memory/.raw_fallbacks/raw.md"},
        ctx,
    )

    assert listed.error is None, listed.error
    assert listed.payload["items"][0]["sourceType"] == "raw_fallback"
    assert listed.payload["items"][0]["path"] == "memory/.raw_fallbacks/raw.md"
    assert shown.error is None, shown.error
    assert shown.payload["sourceType"] == "raw_fallback"
    assert shown.payload["entries"][0]["content"] == "remember raw repair marker RR-1"
    assert repaired.error is None, repaired.error
    assert repaired.payload["results"][0]["sourceType"] == "raw_fallback"
    assert repaired.payload["results"][0]["status"] == "repaired"
    assert flush_service.calls[0][0][0].content == "remember raw repair marker RR-1"
    assert flush_service.calls[0][2]["message_window"] == 0


@pytest.mark.asyncio
async def test_memory_repair_admin_list_uses_durable_ledger_queue(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-1",
                scope="repair",
                source_path="memory/.raw_fallbacks/raw.md",
                idempotency_key="repair:raw.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=20,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)
        ctx = _ctx(session_manager=session_manager)

        listed = await get_dispatcher().dispatch(
            "rr-ledger",
            "memory.repair.list",
            {"agentId": "main", "limit": 10},
            ctx,
        )

        assert listed.error is None, listed.error
        assert listed.payload["items"][0]["sourceType"] == "raw_fallback"
        assert listed.payload["items"][0]["path"] == "memory/.raw_fallbacks/raw.md"
        assert listed.payload["items"][0]["repairStatus"] == "repair_pending"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_repair_admin_list_path_filter_searches_beyond_page_limit(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-1",
                scope="repair",
                source_path="memory/.raw_fallbacks/first.md",
                idempotency_key="repair:first.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=1,
            )
        )
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s2",
                session_id="session-2",
                scope="repair",
                source_path="memory/.raw_fallbacks/second.md",
                idempotency_key="repair:second.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=2,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)
        ctx = _ctx(session_manager=session_manager)

        listed = await get_dispatcher().dispatch(
            "rr-path-limit",
            "memory.repair.list",
            {
                "agentId": "main",
                "limit": 1,
                "path": "memory/.raw_fallbacks/second.md",
            },
            ctx,
        )

        assert listed.error is None, listed.error
        assert listed.payload["count"] == 1
        assert listed.payload["items"][0]["path"] == "memory/.raw_fallbacks/second.md"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_repair_admin_list_scopes_durable_queue_by_agent_id(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:main:webchat:s1",
                session_id="session-main",
                scope="repair",
                source_path="memory/.raw_fallbacks/main.md",
                idempotency_key="repair:main.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=1,
            )
        )
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:ops:webchat:s2",
                session_id="session-ops",
                scope="repair",
                source_path="memory/.raw_fallbacks/ops.md",
                idempotency_key="repair:ops.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=2,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)
        ctx = _ctx(session_manager=session_manager)

        listed = await get_dispatcher().dispatch(
            "rr-agent-scope",
            "memory.repair.list",
            {"agentId": "ops", "limit": 10},
            ctx,
        )

        assert listed.error is None, listed.error
        assert listed.payload["count"] == 1
        assert listed.payload["items"][0]["sessionKey"] == "agent:ops:webchat:s2"
        assert listed.payload["items"][0]["path"] == "memory/.raw_fallbacks/ops.md"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_repair_admin_list_treats_agent_scope_prefix_as_literal(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:ops:webchat:s1",
                session_id="session-ops",
                scope="repair",
                source_path="memory/.raw_fallbacks/ops.md",
                idempotency_key="repair:wildcard-ops.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=1,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)
        ctx = _ctx(session_manager=session_manager)

        listed = await get_dispatcher().dispatch(
            "rr-agent-literal-scope",
            "memory.repair.list",
            {"agentId": "op_", "limit": 10},
            ctx,
        )

        assert listed.error is None, listed.error
        assert listed.payload["count"] == 0
        assert listed.payload["items"] == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_repair_admin_list_canonicalizes_percent_agent_before_scoping(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:ops:webchat:s1",
                session_id="session-ops",
                scope="repair",
                source_path="memory/.raw_fallbacks/ops.md",
                idempotency_key="repair:percent-ops.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=1,
            )
        )
        await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key="agent:op:webchat:s2",
                session_id="session-op",
                scope="repair",
                source_path="memory/.raw_fallbacks/op.md",
                idempotency_key="repair:percent-op.md",
                status="repair_pending",
                reason="parse_failed_archived",
                created_at=2,
            )
        )
        session_manager = FakeStorageRepairSessionManager(storage)
        ctx = _ctx(session_manager=session_manager)

        listed = await get_dispatcher().dispatch(
            "rr-agent-percent-scope",
            "memory.repair.list",
            {"agentId": "op%", "limit": 10},
            ctx,
        )

        assert listed.error is None, listed.error
        assert listed.payload["count"] == 1
        assert listed.payload["items"][0]["sessionKey"] == "agent:op:webchat:s2"
        assert listed.payload["items"][0]["path"] == "memory/.raw_fallbacks/op.md"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_doctor_memory_status_deep_surfaces_repair_and_raw_sidecars(tmp_path):
    raw_dir = tmp_path / "memory" / ".raw_fallbacks"
    raw_dir.mkdir(parents=True)
    (raw_dir / "raw.md").write_text("# Raw flush (timeout)\npreimage\n", encoding="utf-8")
    manager = FakeMemoryManager(workspace_dir=tmp_path)
    session_manager = FakeRepairSessionManager()

    res = await get_dispatcher().dispatch(
        "r1",
        "doctor.memory.status",
        {"agentId": "main", "deep": True},
        _ctx(memory_managers={"main": manager}, session_manager=session_manager),
    )

    assert res.error is None, res.error
    assert res.payload["pendingRepairCount"] == 1
    assert res.payload["recentPreimages"][0]["compactionId"] == "cmp-1"
    assert res.payload["rawFallbackCount"] == 1
    assert res.payload["recentRawFallbacks"][0]["path"] == "memory/.raw_fallbacks/raw.md"


@pytest.mark.asyncio
async def test_providers_status_redacts_keys_and_rejects_unknown_provider():
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/model",
            "api_key": "secret-key",
        }
    )
    ok = await get_dispatcher().dispatch(
        "r1",
        "providers.status",
        {"provider": "openrouter"},
        _ctx(config=cfg),
    )
    unknown = await get_dispatcher().dispatch(
        "r2",
        "providers.status",
        {"provider": "definitely_missing"},
        _ctx(config=cfg),
    )

    assert ok.error is None, ok.error
    rendered = repr(ok.payload)
    assert "secret-key" not in rendered
    assert ok.payload["providers"][0]["apiKeyConfigured"] is True
    assert unknown.error is not None
    assert unknown.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_providers_status_honors_configured_active_api_key_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_KEY", "custom-env-key")
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/model",
            "api_key_env": "OPENSQUILLA_PROVIDER_KEY",
        }
    )

    res = await get_dispatcher().dispatch(
        "r1",
        "providers.status",
        {"provider": "openrouter"},
        _ctx(config=cfg),
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyConfigured"] is True
    assert row["apiKeyEnv"] == "OPENSQUILLA_PROVIDER_KEY"
    assert "custom-env-key" not in repr(res.payload)


@pytest.mark.asyncio
async def test_providers_status_reports_ok_api_key_shape_and_null_latency(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/model",
            "api_key": "sk-or-v1-abcdef",
        }
    )

    res = await get_dispatcher().dispatch(
        "r1", "providers.status", {"provider": "openrouter"}, _ctx(config=cfg)
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyShape"] == "ok"
    # No ProviderStatsStore on the context -> latency is present but null.
    assert "latency" in row
    assert row["latency"] is None
    assert "sk-or-v1-abcdef" not in repr(res.payload)


@pytest.mark.asyncio
async def test_providers_status_flags_url_shaped_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/model",
            "api_key": "https://example.invalid/pasted-endpoint",
        }
    )

    res = await get_dispatcher().dispatch(
        "r1", "providers.status", {"provider": "openrouter"}, _ctx(config=cfg)
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyShape"] == "looks_like_url"
    assert "https://example.invalid/pasted-endpoint" not in repr(res.payload)


@pytest.mark.asyncio
async def test_providers_status_flags_env_name_shaped_api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "OPENROUTER_API_KEY")
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "model": "openrouter/model"}
    )

    res = await get_dispatcher().dispatch(
        "r1", "providers.status", {"provider": "openrouter"}, _ctx(config=cfg)
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyShape"] == "looks_like_env_name"


@pytest.mark.asyncio
async def test_providers_status_flags_shell_paste_dollar_env_name(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "openrouter/model",
            "api_key": "$DEEPSEEK_API_KEY",
        }
    )

    res = await get_dispatcher().dispatch(
        "r1", "providers.status", {"provider": "openrouter"}, _ctx(config=cfg)
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyShape"] == "looks_like_env_name"


@pytest.mark.asyncio
async def test_providers_status_generic_all_caps_key_is_ok(monkeypatch):
    # Legitimate all-uppercase key material must not trip the env-name lint.
    monkeypatch.setenv("OPENROUTER_API_KEY", "AB12CD34EF56GH78")
    cfg = GatewayConfig(
        llm={"provider": "openrouter", "model": "openrouter/model"}
    )

    res = await get_dispatcher().dispatch(
        "r1", "providers.status", {"provider": "openrouter"}, _ctx(config=cfg)
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyConfigured"] is True
    assert row["apiKeyShape"] == "ok"


@pytest.mark.asyncio
async def test_providers_status_absent_key_reports_ok_shape(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "openrouter/model"})

    res = await get_dispatcher().dispatch(
        "r1", "providers.status", {"provider": "openrouter"}, _ctx(config=cfg)
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["apiKeyConfigured"] is False
    assert row["apiKeyShape"] == "ok"


@pytest.mark.asyncio
async def test_providers_status_surfaces_latency_snapshot_from_store(monkeypatch):
    from opensquilla.gateway.provider_stats import ProviderStatsStore

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = ProviderStatsStore()
    for _ in range(5):
        store.record(
            provider_id="openrouter",
            model="openrouter/model",
            ttft_ms=150,
            duration_ms=900,
            ok=True,
        )
    cfg = GatewayConfig(llm={"provider": "openrouter", "model": "openrouter/model"})

    res = await get_dispatcher().dispatch(
        "r1",
        "providers.status",
        {"provider": "openrouter"},
        _ctx(config=cfg, provider_stats=store),
    )

    assert res.error is None, res.error
    row = res.payload["providers"][0]
    assert row["latency"] == {
        "p50TtftMs": 150,
        "p95TtftMs": None,
        "samples": 5,
        "windowMinutes": 60,
    }


class FakeSearchProvider:
    name = "fake_search_ok"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return [SearchResult(title="Title", url="https://example.com", snippet=query)]


class FailingSearchProvider:
    name = "fake_search_fail"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchProviderError(
            provider=self.name,
            kind="network",
            message="network down",
            retryable=True,
        )


@pytest.fixture(autouse=True)
def _hermetic_sandbox_runtime():
    """These tests assume the unconfigured (sandbox-off) runtime state.

    Any earlier test that boots gateway services configures the process-wide
    SandboxRuntime; a leaked runtime with a managed network mode makes the
    search RPCs fail with SandboxDenied. Reset to the neutral state so this
    module's behavior does not depend on test ordering.
    """
    from opensquilla.sandbox.integration import reset_runtime

    reset_runtime()
    yield


@pytest.mark.asyncio
async def test_search_status_and_query_return_structured_payloads():
    register_provider(
        "fake_search_ok",
        FakeSearchProvider,
        SearchProviderSpec(provider_id="fake_search_ok"),
    )
    configure_search("fake_search_ok", max_results=4, diagnostics=True)

    status = await get_dispatcher().dispatch("r1", "search.status", {}, _ctx())
    query = await get_dispatcher().dispatch(
        "r2",
        "search.query",
        {"query": "hello", "limit": 2},
        _ctx(),
    )

    assert status.error is None, status.error
    assert status.payload["provider"] == "fake_search_ok"
    assert status.payload["apiKeyConfigured"] is False
    assert query.error is None, query.error
    assert query.payload["ok"] is True
    assert query.payload["results"][0]["snippet"] == "hello"


@pytest.mark.asyncio
async def test_search_query_provider_failure_is_ok_false_payload():
    register_provider(
        "fake_search_fail",
        FailingSearchProvider,
        SearchProviderSpec(provider_id="fake_search_fail"),
    )
    configure_search("fake_search_fail", diagnostics=True)

    res = await get_dispatcher().dispatch(
        "r1",
        "search.query",
        {"query": "hello"},
        _ctx(),
    )

    assert res.error is None, res.error
    assert res.payload["ok"] is False
    assert res.payload["retry_allowed"] is False
    assert res.payload["error"]["kind"] == "network"
    assert res.payload["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_search_sensitive_query_is_not_echoed_by_tool_or_rpc():
    secret_query = "API_KEY=super-secret-value"
    helper_payload = await run_web_discover_payload(secret_query)
    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "search.query",
        {"query": secret_query},
        _ctx(),
    )

    assert "super-secret-value" not in repr(helper_payload)
    assert "API_KEY" not in repr(helper_payload)
    assert "sensitive" in repr(helper_payload).lower()
    assert helper_payload["query"] == "[redacted]"
    assert rpc_res.error is None, rpc_res.error
    assert repr(rpc_res.payload).find("super-secret-value") == -1
    assert repr(rpc_res.payload).find("API_KEY") == -1
    assert rpc_res.payload["query"] == "[redacted]"
    assert rpc_res.payload["ok"] is False
    assert rpc_res.payload["retry_allowed"] is False
