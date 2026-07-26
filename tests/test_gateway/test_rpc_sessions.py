"""Tests for sessions domain RPC handlers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.agents.registry import AgentRegistry
from opensquilla.agents.scope import default_workspace_dir
from opensquilla.attachment_refs import transcript_material_path
from opensquilla.engine.types import DoneEvent, ErrorEvent
from opensquilla.gateway import rpc_chat, rpc_sessions
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.attachment_ingest import (
    MAX_STAGED_PDF_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import AgentEntryConfig, GatewayConfig
from opensquilla.gateway.input_normalization import LARGE_PASTE_CHARS, estimate_text_tokens
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_sessions import _normalize_terminal_event_payload
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.gateway.uploads import set_upload_store
from opensquilla.gateway.websocket import SubscriptionManager, get_registry
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.models import TranscriptEntry

_DEFAULT_PRINCIPAL = Principal(
    role="operator", scopes=frozenset(["operator.admin"]), is_owner=True, authenticated=True
)


@dataclass
class FakeSession:
    session_key: str = "agent:main:abc123"
    session_id: str = "abc123"
    status: str = "running"
    agent_id: str = "main"
    created_at: int = 1000
    updated_at: int = 2000
    display_name: str | None = None
    derived_title: str | None = None
    channel: str | None = None
    chat_type: str = "unknown"
    group_id: str | None = None
    subject: str | None = None
    last_channel: str | None = None
    last_to: str | None = None
    last_account_id: str | None = None
    last_thread_id: str | None = None
    delivery_context: dict | None = None
    parent_session_key: str | None = None
    spawned_by: str | None = None
    origin: dict | None = None
    model: str | None = None
    model_override: str | None = None
    epoch: int = 0


class FakeStorage:
    def __init__(self, sessions: list[FakeSession] | None = None):
        self._sessions = {s.session_key: s for s in (sessions or [])}
        self._transcripts: dict[str, list] = {}
        self._agent_tasks: dict[str, list[SimpleNamespace]] = {}
        self.memory_durable_receipts: list[Any] = []
        self.list_agent_tasks_calls: list[str | None] = []
        self.list_agent_tasks_for_sessions_calls: list[tuple[str, ...]] = []

    async def list_sessions(self, limit: int | None = None) -> list[FakeSession]:
        result = list(self._sessions.values())
        if limit:
            result = result[:limit]
        return result

    async def get_session(self, key: str) -> FakeSession | None:
        return self._sessions.get(key)

    async def delete_session(self, key: str) -> None:
        if key not in self._sessions:
            raise KeyError(f"Session not found: {key}")
        del self._sessions[key]

    async def delete_transcript(self, session_id: str) -> None:
        self._transcripts.pop(session_id, None)

    async def increment_epoch(self, key: str) -> int:
        session = self._sessions[key]
        session.epoch += 1
        return session.epoch

    async def get_transcript(
        self, session_id: str, limit: int | None = None, offset: int = 0
    ) -> list[Any]:
        rows = list(self._transcripts.get(session_id, []))
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def list_user_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        limit_per_session: int = 3,
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for session_id in session_ids:
            values = [
                str(getattr(row, "content", "") or "")
                for row in self._transcripts.get(session_id, [])
                if str(getattr(row, "role", "") or "").lower() == "user"
                and getattr(row, "content", None)
            ]
            result[session_id] = values[:limit_per_session]
        return result

    async def list_agent_tasks(
        self,
        session_key: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SimpleNamespace]:
        self.list_agent_tasks_calls.append(session_key)
        if session_key is None:
            rows = [row for values in self._agent_tasks.values() for row in values]
        else:
            rows = list(self._agent_tasks.get(session_key, []))
        if status is not None:
            rows = [row for row in rows if getattr(row, "status", None) == status]
        return rows[offset : offset + limit]

    async def list_agent_tasks_for_sessions(
        self,
        session_keys: list[str],
        limit_per_session: int = 100,
    ) -> dict[str, list[SimpleNamespace]]:
        self.list_agent_tasks_for_sessions_calls.append(tuple(session_keys))
        return {
            key: list(self._agent_tasks.get(key, []))[:limit_per_session] for key in session_keys
        }

    async def list_memory_durable_receipts(
        self,
        session_key: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        coverage_turn_id: str | None = None,
        coverage_hash: str | None = None,
        coverage_entry_count: int | None = None,
        idempotency_key: str | None = None,
        limit: int = 100,
    ) -> list[Any]:
        rows = list(self.memory_durable_receipts)
        if session_key is not None:
            rows = [row for row in rows if getattr(row, "session_key", None) == session_key]
        if session_id is not None:
            rows = [row for row in rows if getattr(row, "session_id", None) == session_id]
        if scope is not None:
            rows = [row for row in rows if getattr(row, "scope", None) == scope]
        if status is not None:
            rows = [row for row in rows if getattr(row, "status", None) == status]
        if coverage_turn_id is not None:
            rows = [
                row for row in rows if getattr(row, "coverage_turn_id", None) == coverage_turn_id
            ]
        if coverage_hash is not None:
            rows = [row for row in rows if getattr(row, "coverage_hash", None) == coverage_hash]
        if coverage_entry_count is not None:
            rows = [
                row
                for row in rows
                if getattr(row, "coverage_entry_count", None) == coverage_entry_count
            ]
        if idempotency_key is not None:
            rows = [row for row in rows if getattr(row, "idempotency_key", None) == idempotency_key]
        return rows[:limit]


class FakeSessionManager:
    def __init__(self, sessions: list[FakeSession] | None = None):
        self._storage = FakeStorage(sessions)
        self.created_messages: list[tuple[str, str, str]] = []
        self.removed_messages: list[tuple[str, str]] = []
        self.updated_turn_contexts: list[tuple[str, str, dict[str, Any]]] = []
        self.applied_intents: list[tuple[str, str]] = []
        self.truncate_calls: list[tuple[str, int]] = []
        self.compact_calls: list[tuple[str, int, object | None]] = []
        self.compact_kwargs: list[dict[str, Any]] = []
        self.compact_instructions: list[str | None] = []
        self.compact_summary = "summary for compacted context"
        self.compact_summary_source = "fallback"
        self.transcript: list[Any] = []

    async def append_message(self, key: str, role: str = "user", content: str = "") -> Any:
        self.created_messages.append((key, role, content))
        return SimpleNamespace(
            message_id=f"msg-{len(self.created_messages)}",
            role=role,
            content=content,
        )

    async def remove_message(self, key: str, message_id: str) -> bool:
        self.removed_messages.append((key, message_id))
        return True

    async def update_message_turn_context(
        self,
        key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        self.updated_turn_contexts.append((key, message_id, dict(context)))
        return True

    async def create(
        self,
        session_key: str,
        agent_id: str = "main",
        display_name: str | None = None,
        model: str | None = None,
    ):
        session = FakeSession(
            session_key=session_key,
            session_id=session_key.rsplit(":", 1)[-1],
            agent_id=agent_id,
            display_name=display_name,
            model=model,
        )
        self._storage._sessions[session_key] = session
        return session

    async def get_or_create(
        self,
        session_key: str,
        agent_id: str = "main",
        display_name: str | None = None,
    ):
        session = await self._storage.get_session(session_key)
        if session is not None:
            return session
        return await self.create(
            session_key=session_key,
            agent_id=agent_id,
            display_name=display_name,
        )

    async def get_transcript(self, key: str) -> list:
        return list(self.transcript)

    async def truncate(self, session_key: str, max_messages: int = 20) -> dict:
        session = await self._storage.get_session(session_key)
        if session is None:
            raise KeyError(f"Session not found: {session_key}")
        self.truncate_calls.append((session_key, max_messages))
        return {"truncated": False, "before_count": 0, "after_count": 0}

    async def compact(self, session_key: str, context_window_tokens: int, config=None) -> str:
        session = await self._storage.get_session(session_key)
        if session is None:
            raise KeyError(f"Session not found: {session_key}")
        self.compact_calls.append((session_key, context_window_tokens, config))
        return self.compact_summary

    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config=None,
        custom_instructions: str | None = None,
        **kwargs: Any,
    ):
        self.compact_kwargs.append(dict(kwargs))
        self.compact_instructions.append(custom_instructions)
        summary = await self.compact(session_key, context_window_tokens, config)
        return SimpleNamespace(
            summary=summary,
            removed_count=1 if summary else 0,
            kept_entries=[],
            summary_source=self.compact_summary_source if summary else "skipped",
            tokens_before=1200,
            tokens_after=400,
            remaining_budget_tokens=max(context_window_tokens - 400, 0),
        )

    async def apply_intent(self, session_key: str, intent: str, **kwargs):
        self.applied_intents.append((session_key, str(intent)))
        session = await self._storage.get_session(session_key)
        if session is None:
            session = await self.create(session_key, agent_id=kwargs.get("agent_id", "main"))
            return session, True
        if str(intent) == "new_chat":
            raise ValueError("session_key conflict")
        if str(intent) == "continue":
            return session, False
        if str(intent) != "reset_same_key":
            raise KeyError(f"Session not found: {session_key}")
        old_id = session.session_id
        session.epoch = await self._storage.increment_epoch(session_key)
        await self._storage.delete_transcript(old_id)
        session.session_id = f"{old_id}-rotated"
        return session, True


class SlowCompactionSessionManager(FakeSessionManager):
    def __init__(self, sessions: list[FakeSession] | None = None):
        super().__init__(sessions)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def compact(self, session_key: str, context_window_tokens: int, config=None) -> str:
        self.started.set()
        await self.release.wait()
        return await super().compact(session_key, context_window_tokens, config)


def make_ctx(session_manager=None, **kwargs) -> RpcContext:
    role = kwargs.pop("role", "operator")
    scopes = kwargs.pop("scopes", None)
    if scopes is not None:
        principal = Principal(
            role=role, scopes=frozenset(scopes), is_owner=role == "operator", authenticated=True
        )
    else:
        principal = _DEFAULT_PRINCIPAL
    defaults = {
        "conn_id": "test-conn",
        "principal": principal,
        "config": GatewayConfig(memory={"flush_enabled": False}),
    }
    defaults.update(kwargs)
    ctx = RpcContext(**defaults)
    ctx.session_manager = session_manager
    return ctx


def _capture_compaction_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, dict[str, Any]]]:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _record_emit(
        _ctx: RpcContext,
        session_key: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        emitted.append((session_key, event_name, payload))

    monkeypatch.setattr(rpc_sessions, "_emit_to_subscribers", _record_emit)
    return emitted


def _checkpoint_receipt(
    session: FakeSession,
    *,
    turn_id: str,
    entries: list[Any],
    status: str = "checkpoint_saved",
) -> SimpleNamespace:
    from opensquilla.memory.checkpoint import checkpoint_coverage_hash, checkpoint_turn_id

    return SimpleNamespace(
        session_key=session.session_key,
        session_id=session.session_id,
        turn_id=turn_id,
        scope="checkpoint",
        status=status,
        source_path="memory/.checkpoints/agent-main-webchat-abc/turn-1.jsonl",
        content_hash="h1",
        coverage_turn_id=checkpoint_turn_id(entries),
        coverage_hash=checkpoint_coverage_hash(entries),
        coverage_entry_count=len(entries),
    )


class _FakeCompactionProvider:
    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str = "provider-key",
        model: str = "provider/model",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    @property
    def model(self) -> str:
        return self._model


class _FakeSelectorClone:
    def __init__(self, provider: _FakeCompactionProvider) -> None:
        self.provider = provider
        self.override_calls: list[str] = []

    def override_model(self, model: str) -> None:
        self.override_calls.append(model)
        self.provider._model = model

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _FakeProviderSelector:
    def __init__(self, provider: _FakeCompactionProvider | None = None) -> None:
        self.provider = provider or _FakeCompactionProvider()
        self.clone_instance = _FakeSelectorClone(self.provider)
        self.override_calls: list[str] = []

    def clone(self) -> _FakeSelectorClone:
        return self.clone_instance

    def override_model(self, model: str) -> None:
        self.override_calls.append(model)

    def resolve(self) -> _FakeCompactionProvider:
        return self.provider


class _LegacyCompactManager:
    def __init__(self, session: FakeSession) -> None:
        self._storage = FakeStorage([session])
        self.compact_calls: list[tuple[str, int]] = []

    async def compact(self, session_key: str, context_window_tokens: int) -> str:
        self.compact_calls.append((session_key, context_window_tokens))
        return "legacy summary"


class _ReplayConn:
    def __init__(self, conn_id: str) -> None:
        self.conn_id = conn_id
        self.events: list[tuple[str, dict, dict | None]] = []

    async def send_event(
        self,
        event: str,
        payload: dict | None = None,
        meta: dict | None = None,
    ) -> None:
        self.events.append((event, payload or {}, meta))


class _RecordingTurnRunner:
    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def run(self, message: str, session_key: str, **kwargs):
        self.run_calls.append({"message": message, "session_key": session_key, **kwargs})
        yield DoneEvent()


class _FakeUploadStore:
    def __init__(self, entries: dict[str, tuple[bytes, dict[str, Any]]]) -> None:
        self.entries = entries
        self.evicted: list[str] = []

    async def get(self, file_uuid: str) -> tuple[bytes, dict[str, Any]]:
        return self.entries[file_uuid]

    async def evict(self, file_uuid: str) -> bool:
        self.evicted.append(file_uuid)
        return self.entries.pop(file_uuid, None) is not None


def _exact_pdf(size: int) -> bytes:
    header = b"%PDF-1.4\n"
    return header + b"a" * (size - len(header))


def _ctx_config_with_media_root(tmp_path) -> GatewayConfig:
    cfg = GatewayConfig(memory={"flush_enabled": False})
    cfg.attachments.media_root = str(tmp_path)
    return cfg


@pytest.fixture
def dispatcher():
    return get_dispatcher()


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def ctx_with_sessions(session):
    return make_ctx(session_manager=FakeSessionManager([session]))


@pytest.fixture
def ctx_no_manager():
    return make_ctx(session_manager=None)


class TestSessionsCreate:
    @pytest.mark.asyncio
    async def test_create_stub(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch(
            "r1", "sessions.create", {"agentId": "myagent"}, ctx_no_manager
        )
        assert res.ok is True
        assert res.payload["key"].startswith("agent:myagent:")
        assert "sessionId" in res.payload

    @pytest.mark.asyncio
    async def test_create_defaults(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch("r1", "sessions.create", None, ctx_no_manager)
        assert res.ok is True
        assert res.payload["key"].startswith("agent:main:")

    @pytest.mark.asyncio
    async def test_create_cli_kind_uses_cli_session_namespace(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch(
            "r1", "sessions.create", {"agentId": "myagent", "kind": "cli"}, ctx_no_manager
        )
        assert res.ok is True
        assert res.payload["key"].startswith("agent:myagent:cli:")

    @pytest.mark.asyncio
    async def test_create_webchat_kind_uses_webchat_session_namespace(
        self, dispatcher, ctx_no_manager
    ):
        res = await dispatcher.dispatch(
            "r1", "sessions.create", {"agentId": "myagent", "kind": "webchat"}, ctx_no_manager
        )
        assert res.ok is True
        assert res.payload["key"].startswith("agent:myagent:webchat:")

    @pytest.mark.asyncio
    async def test_create_with_message_requires_manager(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "myagent", "message": "hello"},
            ctx_no_manager,
        )
        assert res.ok is False
        assert res.error.code == "UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_create_with_message_seeds_transcript(self, dispatcher):
        session_manager = FakeSessionManager()
        ctx = make_ctx(session_manager=session_manager)
        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "myagent", "message": "hello"},
            ctx,
        )
        assert res.ok is True
        assert res.payload["seededMessage"] is True
        assert session_manager.created_messages == [(res.payload["key"], "user", "hello")]

    @pytest.mark.asyncio
    async def test_create_uses_agent_registry_model_when_model_not_explicit(self, dispatcher):
        cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="agent/default")])
        registry = AgentRegistry(cfg, persist_changes=False)
        session_manager = FakeSessionManager()
        ctx = make_ctx(session_manager=session_manager, config=cfg, agent_registry=registry)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "ops"},
            ctx,
        )

        assert res.ok is True
        session = session_manager._storage._sessions[res.payload["key"]]
        assert session.model == "agent/default"

    @pytest.mark.asyncio
    async def test_create_explicit_model_overrides_agent_registry_model(self, dispatcher):
        cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="agent/default")])
        registry = AgentRegistry(cfg, persist_changes=False)
        session_manager = FakeSessionManager()
        ctx = make_ctx(session_manager=session_manager, config=cfg, agent_registry=registry)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "ops", "model": "explicit/model"},
            ctx,
        )

        assert res.ok is True
        session = session_manager._storage._sessions[res.payload["key"]]
        assert session.model == "explicit/model"

    @pytest.mark.asyncio
    async def test_create_rejects_missing_agent_when_registry_present(self, dispatcher):
        cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="agent/default")])
        registry = AgentRegistry(cfg, persist_changes=False)
        session_manager = FakeSessionManager()
        ctx = make_ctx(session_manager=session_manager, config=cfg, agent_registry=registry)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "ghost"},
            ctx,
        )

        assert res.ok is False
        assert res.error.code == "agent.not_found"
        assert res.error.details == {"agentId": "ghost"}

    @pytest.mark.asyncio
    async def test_create_with_create_if_missing_does_not_create_agent(self, dispatcher):
        cfg = GatewayConfig()
        registry = AgentRegistry(cfg, persist_changes=False)
        session_manager = FakeSessionManager()
        ctx = make_ctx(
            session_manager=session_manager,
            config=cfg,
            agent_registry=registry,
            scopes=["operator.write"],
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {
                "agentId": "dragons",
                "agentName": "Dragons",
                "createAgentIfMissing": True,
                "model": "openai/test",
            },
            ctx,
        )

        assert res.ok is False
        assert res.error.code == "agent.not_found"
        assert res.error.details == {"agentId": "dragons"}
        assert cfg.agents == []
        assert session_manager._storage._sessions == {}

    @pytest.mark.asyncio
    async def test_create_with_create_if_missing_existing_agent_no_duplicate(self, dispatcher):
        cfg = GatewayConfig(agents=[AgentEntryConfig(id="ops", model="agent/default")])
        registry = AgentRegistry(cfg, persist_changes=False)
        session_manager = FakeSessionManager()
        ctx = make_ctx(session_manager=session_manager, config=cfg, agent_registry=registry)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "ops", "createAgentIfMissing": True},
            ctx,
        )

        assert res.ok is True
        assert sum(1 for a in cfg.agents if a.id == "ops") == 1

    @pytest.mark.asyncio
    async def test_create_main_agent_passes_without_registry(self, dispatcher):
        # No agent_registry on ctx; agentId="main" must always pass through.
        session_manager = FakeSessionManager()
        ctx = make_ctx(session_manager=session_manager)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.create",
            {"agentId": "main"},
            ctx,
        )

        assert res.ok is True


class TestSessionsList:
    @staticmethod
    def _assert_contract_base(row: dict[str, object]) -> None:
        for key in (
            "key",
            "agent_id",
            "agentId",
            "status",
            "updated_at",
            "updatedAt",
            "message_count",
            "entry_count",
            "effectiveAgentId",
            "sessionKind",
            "surface",
            "conversationKind",
            "title",
            "groupLabel",
            "messageCount",
            "runStatus",
            "interactive",
        ):
            assert key in row

    @pytest.mark.asyncio
    async def test_list_contract_webchat_row(self, dispatcher):
        session = FakeSession(session_key="agent:main:webchat:default")
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["effectiveAgentId"] == "main"
        assert row["sessionKind"] == "chat"
        assert row["surface"] == "webchat"
        assert row["conversationKind"] == "direct"
        assert row["groupLabel"] == "Web chat"
        assert row["messageCount"] == row["message_count"]
        assert row["runStatus"] == "idle"
        assert row["interactive"] is True

    @pytest.mark.asyncio
    async def test_list_includes_workspace_from_run_context(self, dispatcher, tmp_path):
        workspace = tmp_path / "project-alpha"
        workspace.mkdir()
        session = FakeSession(
            session_key="agent:main:webchat:workspace",
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "workspace": str(workspace),
                    "run_mode": "trusted",
                }
            },
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["workspace"] == str(workspace)
        assert row["workspaceLabel"] == "project-alpha"
        assert row["workspaceDisplayPath"] == str(workspace)

    @pytest.mark.asyncio
    async def test_list_uses_explicit_config_workspace(self, dispatcher, tmp_path):
        workspace = tmp_path / "project-beta"
        workspace.mkdir()
        session = FakeSession(session_key="agent:main:webchat:workspace-config")
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            config=GatewayConfig(workspace_dir=str(workspace), memory={"flush_enabled": False}),
        )

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["workspace"] == str(workspace)
        assert row["workspaceLabel"] == "project-beta"
        assert row["workspaceDisplayPath"] == str(workspace)

    @pytest.mark.asyncio
    async def test_list_keeps_default_opensquilla_workspace_flat(
        self, dispatcher, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla-home"))
        workspace = default_workspace_dir()
        workspace.mkdir(parents=True)
        session = FakeSession(
            session_key="agent:main:webchat:default-workspace",
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "workspace": str(workspace),
                    "run_mode": "trusted",
                }
            },
        )
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            config=GatewayConfig(memory={"flush_enabled": False}),
        )

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert "workspace" not in row
        assert "workspaceLabel" not in row
        assert "workspaceDisplayPath" not in row

    @pytest.mark.asyncio
    async def test_list_returns_each_session_once(self, dispatcher):
        sessions = [
            FakeSession(session_key="agent:main:webchat:first"),
            FakeSession(session_key="agent:main:webchat:second"),
        ]
        ctx = make_ctx(session_manager=FakeSessionManager(sessions))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        assert [row["key"] for row in res.payload["sessions"]] == [
            "agent:main:webchat:first",
            "agent:main:webchat:second",
        ]
        assert res.payload["count"] == 2

    @pytest.mark.asyncio
    async def test_list_webchat_title_uses_first_user_message(self, dispatcher):
        session = FakeSession(
            session_key="agent:main:webchat:semantic-title",
            display_name="WebChat",
        )
        manager = FakeSessionManager([session])
        manager._storage._transcripts[session.session_id] = [
            SimpleNamespace(role="system", content="runtime note"),
            SimpleNamespace(
                role="user",
                content="[2026-06-04T19:25+08:00 Thu Asia/Shanghai]\nLLM位置编码方式",
            ),
        ]
        ctx = make_ctx(session_manager=manager)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["display_name"] == "WebChat"
        assert row["title"] == "LLM位置编码方式"

    @pytest.mark.asyncio
    async def test_list_webchat_title_extracts_json_text(self, dispatcher):
        session = FakeSession(session_key="agent:main:webchat:json-title")
        manager = FakeSessionManager([session])
        manager._storage._transcripts[session.session_id] = [
            SimpleNamespace(
                role="user",
                content=json.dumps({"text": "Agent PM面试清单"}, ensure_ascii=False),
            ),
        ]
        ctx = make_ctx(session_manager=manager)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        assert res.payload["sessions"][0]["title"] == "Agent PM面试清单"

    @pytest.mark.asyncio
    async def test_list_contract_cli_current_tui_compatible_row(self, dispatcher):
        session = FakeSession(session_key="agent:main:cli:a1b2c3d4")
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "chat"
        assert row["surface"] == "cli"
        assert row["conversationKind"] == "main"
        assert row["groupLabel"] == "CLI"
        assert row["interactive"] is False

    @pytest.mark.asyncio
    async def test_list_contract_main_agent_chat_row(self, dispatcher):
        session = FakeSession(session_key="agent:ops:main", agent_id="ops")
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["effectiveAgentId"] == "ops"
        assert row["sessionKind"] == "chat"
        assert row["surface"] == "unknown"
        assert row["conversationKind"] == "main"
        assert row["groupLabel"] == "Chats"
        assert row["interactive"] is False

    @pytest.mark.asyncio
    async def test_list_contract_direct_agent_chat_row(self, dispatcher):
        session = FakeSession(session_key="agent:main:direct:user-1")
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "chat"
        assert row["surface"] == "unknown"
        assert row["conversationKind"] == "direct"
        assert row["groupLabel"] == "Chats"
        assert row["interactive"] is False

    @pytest.mark.asyncio
    async def test_list_classifies_custom_named_channel_sessions(self, dispatcher):
        # Operators name channels freely ("飞书", "slack-eng"); the session key
        # and last_channel carry that NAME, not the platform type. The view must
        # resolve it through the configured name->type map or every custom-named
        # channel's sessions land in the sidebar as unclassifiable "unknown".
        session = FakeSession(
            session_key="agent:main:飞书:direct:ou_demo_user",
            last_channel="飞书",
            last_to="ou_demo_user",
        )
        config = GatewayConfig(
            memory={"flush_enabled": False},
            channels={
                "channels": [
                    {
                        "type": "feishu",
                        "name": "飞书",
                        "app_id": "cli_dummy",
                        "app_secret": "dummy",
                    }
                ]
            },
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]), config=config)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["sessionKind"] == "channel"
        assert row["surface"] == "feishu"
        assert row["conversationKind"] == "direct"

    @pytest.mark.asyncio
    async def test_list_contract_slack_channel_thread_row(self, dispatcher):
        thread_id = "1717000000.000100"
        session = FakeSession(
            session_key=f"agent:main:slack:group:C123:thread:{thread_id}",
            last_channel="slack",
            last_to="C123",
            last_thread_id=thread_id,
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "channel"
        assert row["surface"] == "slack"
        assert row["conversationKind"] == "group"
        assert row["thread"] == {"id": thread_id, "kind": "thread"}
        assert row["channel"] is None
        assert row["channelContext"] == {
            "name": "slack",
            "id": "C123",
            "threadId": thread_id,
        }
        assert row["groupLabel"] == "Slack"
        assert row["interactive"] is False

    @pytest.mark.asyncio
    async def test_list_contract_preserves_legacy_channel_field(self, dispatcher):
        session = FakeSession(
            session_key="agent:main:slack:group:C123",
            channel="slack",
            last_to="C123",
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["channel"] == "slack"
        assert row["channelContext"] == {"name": "slack", "id": "C123"}

    @pytest.mark.asyncio
    async def test_list_contract_telegram_topic_row(self, dispatcher):
        session = FakeSession(
            session_key="agent:main:telegram:group:chat-1:topic:topic-9",
            last_channel="telegram",
            last_to="chat-1",
            last_thread_id="topic-9",
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "channel"
        assert row["surface"] == "telegram"
        assert row["conversationKind"] == "group"
        assert row["thread"] == {"id": "topic-9", "kind": "topic"}
        assert row["groupLabel"] == "Telegram"

    @pytest.mark.asyncio
    async def test_list_contract_subagent_task_row(self, dispatcher):
        parent_key = "agent:main:webchat:default"
        session = FakeSession(
            session_key="agent:main:subagent:760b927a",
            parent_session_key=parent_key,
            spawned_by="task-123",
            origin={"kind": "subagent", "spawnDepth": 1},
        )
        manager = FakeSessionManager([session])
        manager._storage._agent_tasks[session.session_key] = [
            SimpleNamespace(
                task_id="task-123",
                status="running",
                queue_mode="followup",
                run_kind="subagent",
                source_kind="subagent",
                created_at=100,
                started_at=110,
                finished_at=None,
                terminal_reason=None,
            )
        ]
        ctx = make_ctx(session_manager=manager, task_runtime=None)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "task"
        assert row["surface"] == "subagent"
        assert row["conversationKind"] == "unknown"
        assert row["runStatus"] == "running"
        assert row["interactive"] is False
        assert row["parent"] == {
            "key": parent_key,
            "taskId": "task-123",
            "spawnDepth": 1,
        }

    @pytest.mark.asyncio
    async def test_list_contract_run_status_matches_legacy_interrupted_state(self, dispatcher):
        session = FakeSession(session_key="agent:main:webchat:interrupted")
        manager = FakeSessionManager([session])
        manager._storage._agent_tasks[session.session_key] = [
            SimpleNamespace(
                task_id="task-abandoned",
                status="abandoned",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=100,
                started_at=110,
                finished_at=120,
                terminal_reason="process_restart",
            )
        ]
        ctx = make_ctx(session_manager=manager, task_runtime=None)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["run_status"] == "interrupted"
        assert row["runStatus"] == "interrupted"

    @pytest.mark.asyncio
    async def test_list_contract_cron_isolated_row(self, dispatcher):
        session = FakeSession(
            session_key="cron:daily-summary:run:abc123",
            display_name="Daily summary",
            origin={
                "kind": "cron",
                "jobId": "daily-summary",
                "sessionTarget": "isolated",
            },
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "cron"
        assert row["surface"] == "cron"
        assert row["groupLabel"] == "Cron"
        assert row["interactive"] is False
        assert row["cron"] == {
            "jobId": "daily-summary",
            "sessionTarget": "isolated",
        }

    @pytest.mark.asyncio
    async def test_list_contract_cron_delivery_keeps_feishu_channel_identity(self, dispatcher):
        session_key = "agent:main:feishu:group:oc_123"
        session = FakeSession(
            session_key=session_key,
            last_channel="feishu",
            last_to="oc_123",
            origin={
                "kind": "channel",
                "cron": {
                    "jobId": "launch-check",
                    "sessionTarget": "session",
                    "targetSessionKey": session_key,
                },
            },
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "channel"
        assert row["surface"] == "feishu"
        assert row["conversationKind"] == "group"
        assert row["channel"] is None
        assert row["channelContext"] == {"name": "feishu", "id": "oc_123"}
        assert row["cron"] == {
            "jobId": "launch-check",
            "sessionTarget": "session",
            "targetSessionKey": session_key,
        }

    @pytest.mark.asyncio
    async def test_list_contract_legacy_agent_mismatch_uses_effective_agent(self, dispatcher):
        session = FakeSession(
            session_key="agent:kid-project:webchat:test",
            agent_id="main",
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["agentId"] == "main"
        assert row["effectiveAgentId"] == "kid-project"
        assert row["sessionKind"] == "chat"
        assert row["surface"] == "webchat"
        assert row["conversationKind"] == "direct"
        assert row["interactive"] is True

    @pytest.mark.asyncio
    async def test_list_contract_unknown_fallback_row(self, dispatcher):
        session = FakeSession(
            session_key="legacy-weird-session",
            session_id="legacy-weird-session-id",
            status="running",
            display_name=None,
            origin=None,
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        self._assert_contract_base(row)
        assert row["sessionKind"] == "unknown"
        assert row["surface"] == "unknown"
        assert row["conversationKind"] == "unknown"
        assert row["title"] == "legacy-weird-session"
        assert row["groupLabel"] == "Other"
        assert row["runStatus"] == "idle"
        assert row["interactive"] is False

    @pytest.mark.asyncio
    async def test_list_includes_source_and_delivery_metadata(self, dispatcher):
        session = FakeSession(
            session_key="agent:main:webchat:abc12345",
            display_name="WebChat",
            last_channel="slack",
            last_to="C123",
            last_account_id="acct-1",
            last_thread_id="1700.1",
            delivery_context={"channel_id": "C123"},
        )
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["agent_id"] == "main"
        assert row["display_name"] == "WebChat"
        assert row["source_kind"] == "webui"
        assert row["channel_kind"] == "slack"
        assert row["last_channel"] == "slack"
        assert row["last_to"] == "C123"
        assert row["delivery_context"] == {"channel_id": "C123"}

    @pytest.mark.asyncio
    async def test_list_exposes_persisted_active_task_without_runtime(self, dispatcher):
        session = FakeSession(session_key="agent:main:webchat:task-ledger")
        manager = FakeSessionManager([session])
        manager._storage._agent_tasks[session.session_key] = [
            SimpleNamespace(
                task_id="task-1",
                status="running",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=100,
                started_at=110,
                finished_at=None,
                terminal_reason=None,
            )
        ]
        ctx = make_ctx(session_manager=manager, task_runtime=None)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["tasks"][0]["task_id"] == "task-1"
        assert row["active_task"]["task_id"] == "task-1"
        assert row["last_task"]["task_id"] == "task-1"
        assert row["run_status"] == "running"

    @pytest.mark.asyncio
    async def test_list_uses_active_task_activity_for_recents_sort_key(self, dispatcher):
        session = FakeSession(
            session_key="agent:main:webchat:active-sort",
            updated_at=100,
        )
        manager = FakeSessionManager([session])
        manager._storage._agent_tasks[session.session_key] = [
            SimpleNamespace(
                task_id="task-running",
                status="running",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=300,
                updated_at=450,
                started_at=400,
                finished_at=None,
                terminal_reason=None,
            )
        ]
        ctx = make_ctx(session_manager=manager, task_runtime=None)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["runStatus"] == "running"
        assert row["lastActivityAt"] == 450
        assert row["updatedAt"] == 450
        assert row["updated_at"] == 100

    @pytest.mark.asyncio
    async def test_list_prefers_running_active_task_over_newer_queued_task(self, dispatcher):
        session = FakeSession(session_key="agent:main:webchat:running-priority")
        manager = FakeSessionManager([session])
        manager._storage._agent_tasks[session.session_key] = [
            SimpleNamespace(
                task_id="task-running",
                status="running",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=100,
                started_at=110,
                finished_at=None,
                terminal_reason=None,
            ),
            SimpleNamespace(
                task_id="task-queued",
                status="queued",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=200,
                started_at=None,
                finished_at=None,
                terminal_reason=None,
            ),
        ]
        ctx = make_ctx(session_manager=manager, task_runtime=None)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        row = res.payload["sessions"][0]
        assert row["active_task"]["task_id"] == "task-running"
        assert row["run_status"] == "running"

    @pytest.mark.asyncio
    async def test_list_batches_persisted_task_state_for_visible_sessions(self, dispatcher):
        one = FakeSession(session_key="agent:main:webchat:one")
        two = FakeSession(session_key="agent:main:webchat:two")
        manager = FakeSessionManager([one, two])
        manager._storage._agent_tasks[one.session_key] = [
            SimpleNamespace(
                task_id="task-one",
                status="running",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=100,
                started_at=110,
                finished_at=None,
                terminal_reason=None,
            )
        ]
        manager._storage._agent_tasks[two.session_key] = [
            SimpleNamespace(
                task_id="task-two",
                status="succeeded",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=90,
                started_at=95,
                finished_at=120,
                terminal_reason="completed",
            )
        ]
        ctx = make_ctx(session_manager=manager, task_runtime=None)

        res = await dispatcher.dispatch("r1", "sessions.list", None, ctx)

        assert res.ok is True
        by_key = {row["key"]: row for row in res.payload["sessions"]}
        assert by_key[one.session_key]["active_task"]["task_id"] == "task-one"
        assert by_key[two.session_key]["last_task"]["task_id"] == "task-two"
        assert manager._storage.list_agent_tasks_for_sessions_calls == [
            (one.session_key, two.session_key)
        ]
        assert manager._storage.list_agent_tasks_calls == []


class TestSessionsSend:
    @pytest.mark.asyncio
    async def test_send_valid(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "hello"},
            ctx_with_sessions,
        )
        assert res.ok is True
        assert ctx_with_sessions.session_manager.applied_intents == [
            (session.session_key, "continue")
        ]

    @pytest.mark.asyncio
    async def test_send_apply_intent_waits_for_session_lock(self, dispatcher, session):
        manager = FakeSessionManager([session])
        turn_runner = _RecordingTurnRunner()
        lock = turn_runner._get_session_lock(session.session_key)
        await lock.acquire()
        ctx = make_ctx(session_manager=manager, turn_runner=turn_runner)

        send_task = asyncio.create_task(
            dispatcher.dispatch(
                "r1",
                "sessions.send",
                {"key": session.session_key, "message": "hello"},
                ctx,
            )
        )
        await asyncio.sleep(0)

        assert manager.applied_intents == []
        assert send_task.done() is False

        lock.release()
        res = await send_task
        background = get_agent_task_registry().get(session.session_key)
        if background is not None:
            await background

        assert res.ok is True
        assert manager.applied_intents == [(session.session_key, "continue")]

    @pytest.mark.asyncio
    async def test_send_preserves_persisted_message_on_context_budget_terminal_error(
        self, dispatcher, session
    ):
        class _BudgetErrorTurnRunner(_RecordingTurnRunner):
            async def run(self, message: str, session_key: str, **kwargs):
                self.run_calls.append({"message": message, "session_key": session_key, **kwargs})
                yield ErrorEvent(
                    message='{"fallback_reason":"provider_request_budget_exhausted"}',
                    code="provider_request_budget_exhausted",
                )

        manager = FakeSessionManager([session])
        runner = _BudgetErrorTurnRunner()
        ctx = make_ctx(session_manager=manager, turn_runner=runner)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "keep this overlong input"},
            ctx,
        )
        task = get_agent_task_registry().get(session.session_key)
        if task is not None:
            await task

        assert res.ok is True
        assert manager.created_messages == [
            (session.session_key, "user", "keep this overlong input")
        ]
        assert manager.removed_messages == []

    @pytest.mark.asyncio
    async def test_send_passes_persisted_user_message_id_to_task_runtime(self, dispatcher, session):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "hello"},
            ctx,
        )

        assert res.ok is True
        assert runtime.enqueue_calls[0]["persisted_user_message_id"] == "msg-1"
        assert (
            runtime.enqueue_calls[0]["envelope"].metadata.get("persisted_user_message_id") is None
        )

    @pytest.mark.asyncio
    async def test_send_returns_stable_turn_and_surface_identity(self, dispatcher, session):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.envelope = None
                self.turn_id = None

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.envelope = envelope
                self.turn_id = kwargs["task_id"]
                return SimpleNamespace(
                    task_id=self.turn_id,
                    session_key=envelope.session_key,
                    status="queued",
                )

        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "hello",
                "clientMessageId": "client-msg-1",
                "surfaceId": "tui:process-1",
            },
            ctx,
        )

        assert res.ok is True
        assert res.payload == {
            "status": "accepted",
            "key": session.session_key,
            "session_key": session.session_key,
            "session_id": session.session_id,
            "task_id": runtime.turn_id,
            "turn_id": runtime.turn_id,
            "client_message_id": "client-msg-1",
            "user_message_id": "msg-1",
            "surface_id": "tui:process-1",
        }
        assert runtime.envelope.metadata["client_message_id"] == "client-msg-1"
        assert runtime.envelope.metadata["surface_id"] == "tui:process-1"
        assert isinstance(runtime.turn_id, str) and runtime.turn_id

    @pytest.mark.asyncio
    async def test_send_collect_keeps_preallocated_turn_identity(self, dispatcher, session):
        from opensquilla.gateway.routing import RouteEnvelope, SourceKind
        from opensquilla.gateway.task_runtime import TaskRuntime
        from opensquilla.session.models import AgentTaskRecord

        class RuntimeStorage:
            def __init__(self) -> None:
                self.records: dict[str, AgentTaskRecord] = {}
                self.turn_context_updates: list[tuple[str, str, dict[str, Any]]] = []

            async def create_agent_task(self, record: AgentTaskRecord) -> None:
                self.records[record.task_id] = record

            async def update_agent_task(self, task_id: str, **kwargs: Any) -> None:
                record = self.records[task_id]
                for key, value in kwargs.items():
                    if hasattr(record, key):
                        object.__setattr__(record, key, value)

            async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
                return self.records.get(task_id)

            async def list_agent_tasks(self, **_kwargs: Any) -> list[AgentTaskRecord]:
                return list(self.records.values())

            async def update_transcript_turn_context(
                self,
                session_key: str,
                message_id: str,
                context: dict[str, Any],
            ) -> bool:
                self.turn_context_updates.append(
                    (session_key, message_id, dict(context))
                )
                return True

        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        runs: list[tuple[str, str]] = []

        async def handler(run: Any) -> None:
            runs.append((run.task_id, run.message))
            if run.message == "blocker":
                blocker_started.set()
                await release_blocker.wait()

        runtime_storage = RuntimeStorage()
        runtime = TaskRuntime(
            storage=runtime_storage,
            turn_handler=handler,
            max_concurrency=1,
        )
        blocker_envelope = RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="test",
            agent_id="main",
            session_key=session.session_key,
            input_provenance={"kind": "test"},
        )
        blocker = await runtime.enqueue(blocker_envelope, "blocker")
        await asyncio.wait_for(blocker_started.wait(), timeout=2.0)

        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)
        first = await dispatcher.dispatch(
            "r-collect-1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "first",
                "queueMode": "collect",
                "clientMessageId": "client-collect-1",
            },
            ctx,
        )
        second = await dispatcher.dispatch(
            "r-collect-2",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "second",
                "queueMode": "collect",
                "clientMessageId": "client-collect-2",
            },
            ctx,
        )

        assert first.ok is True
        assert second.ok is True
        assert first.payload["task_id"] == first.payload["turn_id"]
        assert second.payload["task_id"] == second.payload["turn_id"]
        assert second.payload["turn_id"] == first.payload["turn_id"]
        assert runtime_storage.turn_context_updates[-1][2] == {
            "turn_id": first.payload["turn_id"],
            "client_message_id": "client-collect-2",
            "surface_id": "web:test-conn",
            "intent": "send",
            "disposition": "queued",
            "target_turn_id": first.payload["turn_id"],
            "revision": 2,
        }

        release_blocker.set()
        await runtime.wait(blocker.task_id, timeout=2.0)
        await runtime.wait(first.payload["task_id"], timeout=2.0)
        assert runs == [
            (blocker.task_id, "blocker"),
            (first.payload["task_id"], "first\nsecond"),
        ]

    @pytest.mark.asyncio
    async def test_send_marks_empty_transcript_as_fresh_user_session(self, dispatcher, session):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        manager.transcript = []
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "hello"},
            ctx,
        )

        assert res.ok is True
        assert runtime.enqueue_calls[0]["fresh_user_session"] is True

    @pytest.mark.asyncio
    async def test_send_marks_non_empty_transcript_as_not_fresh_user_session(
        self, dispatcher, session
    ):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(role="user", content="previous")]
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "hello"},
            ctx,
        )

        assert res.ok is True
        assert runtime.enqueue_calls[0]["fresh_user_session"] is False

    @pytest.mark.asyncio
    async def test_send_uses_source_run_mode_without_persisting_to_session(self, dispatcher):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        class UpdatingFakeSessionManager(FakeSessionManager):
            def __init__(self, sessions: list[FakeSession]) -> None:
                super().__init__(sessions)
                self.updates: list[tuple[str, dict[str, Any]]] = []

            async def update(self, session_key: str, **fields: Any):
                self.updates.append((session_key, fields))
                session = await self._storage.get_session(session_key)
                if session is None:
                    raise KeyError(f"Session not found: {session_key}")
                for key, value in fields.items():
                    setattr(session, key, value)
                return session

        session = FakeSession(
            session_key="agent:main:webchat:run-mode-source",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": "/workspace",
                }
            },
        )
        runtime = RecordingTaskRuntime()
        manager = UpdatingFakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "hello",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "web",
                    "runMode": "full",
                },
            },
            ctx,
        )

        assert res.ok is True
        envelope = runtime.enqueue_calls[0]["envelope"]
        assert envelope.metadata["run_mode"] == "full"
        assert envelope.metadata["sandbox_run_context"]["run_mode"] == "full"
        assert envelope.metadata["elevated"] == "full"
        assert session.origin["sandbox_run_context"]["run_mode"] == "standard"
        assert manager.updates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requested_run_mode", "expected_run_mode"),
        [
            ("full", "trusted"),
            ("trusted", "trusted"),
            ("standard", "standard"),
        ],
    )
    async def test_send_non_owner_source_run_mode_is_principal_scoped_without_persisting(
        self,
        dispatcher,
        requested_run_mode: str,
        expected_run_mode: str,
    ):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        session = FakeSession(
            session_key=f"agent:main:webchat:non-owner-{requested_run_mode}",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": "/workspace",
                }
            },
        )
        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        principal = Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=False,
            authenticated=True,
        )
        ctx = make_ctx(
            session_manager=manager,
            task_runtime=runtime,
            principal=principal,
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "hello",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "web",
                    "runMode": requested_run_mode,
                },
            },
            ctx,
        )

        assert res.ok is True
        envelope = runtime.enqueue_calls[0]["envelope"]
        assert envelope.metadata["run_mode"] == expected_run_mode
        assert envelope.metadata["sandbox_run_context"]["run_mode"] == expected_run_mode
        assert envelope.metadata.get("elevated") != "full"
        assert session.origin["sandbox_run_context"]["run_mode"] == "standard"

    @pytest.mark.asyncio
    async def test_chat_send_forwards_source_run_mode_to_sessions_send(self, dispatcher):
        chat_session = FakeSession(
            session_key="agent:main:webchat:chat-run-mode-source",
            session_id="chat-run-mode-source",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": "/workspace",
                }
            },
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        chat_ctx = make_ctx(session_manager=chat_manager, turn_runner=chat_runner)

        res = await dispatcher.dispatch(
            "r-chat-run-mode",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": "hello",
                "_source": {"runMode": "full"},
            },
            chat_ctx,
        )

        assert res.ok is True
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task
        assert chat_runner.run_calls[0]["tool_context"].run_mode == "full"
        assert chat_runner.run_calls[0]["tool_context"].elevated == "full"
        assert chat_session.origin["sandbox_run_context"]["run_mode"] == "standard"

    @pytest.mark.asyncio
    async def test_chat_send_non_owner_full_source_run_mode_downgrades_to_trusted(self, dispatcher):
        chat_session = FakeSession(
            session_key="agent:main:webchat:chat-non-owner-full-source",
            session_id="chat-non-owner-full-source",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": "/workspace",
                }
            },
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        principal = Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=False,
            authenticated=True,
        )
        chat_ctx = make_ctx(
            session_manager=chat_manager,
            turn_runner=chat_runner,
            principal=principal,
        )

        res = await dispatcher.dispatch(
            "r-chat-non-owner-run-mode",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": "hello",
                "_source": {"runMode": "full"},
            },
            chat_ctx,
        )

        assert res.ok is True
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task
        assert chat_runner.run_calls[0]["tool_context"].run_mode == "trusted"
        assert chat_runner.run_calls[0]["tool_context"].elevated != "full"
        assert chat_session.origin["sandbox_run_context"]["run_mode"] == "standard"

    @pytest.mark.asyncio
    async def test_send_strips_hidden_preflight_payload_before_task_runtime(
        self, dispatcher, session
    ):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)
        hidden_message = (
            "Original visible request\n\n"
            "Confirmed request fields:\n"
            "- audience: decision owner\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->\n"
            "<!-- opensquilla:meta_preflight_run_id=01KTCQUEUE -->"
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": hidden_message,
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            ctx,
        )

        assert res.ok is True
        assert runtime.enqueue_calls[0]["message"] == "Original visible request"
        assert runtime.enqueue_calls[0]["semantic_message"] == hidden_message

    @pytest.mark.asyncio
    async def test_send_marks_direct_runner_empty_transcript_as_fresh_user_session(
        self, dispatcher
    ):
        session = FakeSession(session_key="agent:main:webchat:fresh-direct")
        manager = FakeSessionManager([session])
        manager.transcript = []
        runner = _RecordingTurnRunner()
        ctx = make_ctx(
            session_manager=manager,
            task_runtime=None,
            turn_runner=runner,
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "hello"},
            ctx,
        )
        task = get_agent_task_registry().get(session.session_key)
        if task is not None:
            await task

        assert res.ok is True
        assert runner.run_calls[0]["fresh_user_session"] is True

    def test_send_prefers_agent_encoded_in_session_key_for_routing(self, dispatcher):
        class RecordingTaskRuntime:
            def __init__(self) -> None:
                self.enqueue_calls: list[dict[str, Any]] = []

            async def enqueue(self, envelope, message: str, **kwargs: Any):
                self.enqueue_calls.append({"envelope": envelope, "message": message, **kwargs})
                return SimpleNamespace(
                    task_id="task-1",
                    session_key=envelope.session_key,
                    status="queued",
                )

        session = FakeSession(
            session_key="agent:kid-project:webchat:test",
            session_id="test",
            agent_id="main",
        )
        runtime = RecordingTaskRuntime()
        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        async def _run():
            return await dispatcher.dispatch(
                "r1",
                "sessions.send",
                {"key": session.session_key, "message": "hello"},
                ctx,
            )

        res = asyncio.run(_run())

        assert res.ok is True
        assert runtime.enqueue_calls[0]["envelope"].agent_id == "kid-project"

    def test_legacy_session_error_payload_is_terminal_message_normalized(self):
        payload = _normalize_terminal_event_payload(
            "session.event.error",
            {
                "message": "Session event stream idle before terminal event",
                "code": "stream_idle_timeout",
            },
        )

        assert payload["message"] == "The task timed out before it could finish."
        assert payload["terminal_message"] == "The task timed out before it could finish."
        assert payload["terminal_reason"] == "timeout"
        assert payload["error_message"] == "The task timed out before it could finish."

    def test_terminal_error_payload_carries_typed_turn_outcome(self):
        # Every terminal error must ship the typed TurnOutcome so surfaces can
        # render a specific cause + retryability, not just the human string.
        payload = _normalize_terminal_event_payload(
            "session.event.error",
            {
                "message": "Autonomous execution paused after repeated sandbox denials.",
                "code": "sandbox_threshold_exceeded",
            },
        )

        outcome = payload["turn_outcome"]
        assert outcome["kind"] == "blocked"
        assert outcome["reason"] == "sandbox_threshold_exceeded"
        assert outcome["retryable"] is True
        # And the human message is the actionable, resume-oriented phrasing.
        assert "resume" in payload["message"].lower()

    def test_terminal_error_turn_outcome_defaults_to_failed_for_unknown_code(self):
        payload = _normalize_terminal_event_payload(
            "session.event.error",
            {"message": "boom", "code": "SomeRuntimeError"},
        )
        assert payload["turn_outcome"]["kind"] == "failed"

    @pytest.mark.asyncio
    async def test_send_reset_same_key_intent_applies_before_append(
        self, dispatcher, ctx_with_sessions, session
    ):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "fresh start",
                "intent": "reset_same_key",
            },
            ctx_with_sessions,
        )

        assert res.ok is True
        assert ctx_with_sessions.session_manager.applied_intents == [
            (session.session_key, "reset_same_key")
        ]
        assert ctx_with_sessions.session_manager.created_messages[0] == (
            session.session_key,
            "user",
            "fresh start",
        )

    @pytest.mark.asyncio
    async def test_send_new_chat_intent_creates_missing_key(self, dispatcher):
        manager = FakeSessionManager()
        ctx = make_ctx(session_manager=manager)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": "agent:default:fresh",
                "message": "fresh",
                "intent": "new_chat",
            },
            ctx,
        )

        assert res.ok is True
        assert manager.applied_intents == [("agent:main:fresh", "new_chat")]
        assert manager.created_messages[0] == ("agent:main:fresh", "user", "fresh")

    @pytest.mark.asyncio
    async def test_send_missing_message(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1", "sessions.send", {"key": session.session_key}, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_send_missing_key(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch("r1", "sessions.send", {"message": "hi"}, ctx_with_sessions)
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_send_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": "nonexistent", "message": "hi"},
            ctx_with_sessions,
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_send_rejects_too_many_attachments(self, dispatcher, ctx_with_sessions, session):
        # The per-turn cap is 10; 11 must be rejected.
        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "hi",
                "attachments": [{"type": "image/png", "data": "QQ=="}] * 11,
            },
            ctx_with_sessions,
        )
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_send_persists_web_attachment_display_text_without_changing_cli(
        self,
        dispatcher,
    ):
        attachment = {"type": "image/png", "data": "aW1hZ2U=", "name": "image.png"}

        web_session = FakeSession(
            session_key="agent:main:webchat:web-display",
            session_id="web-display",
        )
        web_manager = FakeSessionManager([web_session])
        web_runner = _RecordingTurnRunner()
        web_ctx = make_ctx(session_manager=web_manager, turn_runner=web_runner)
        web_res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": web_session.session_key,
                "message": "Describe these attachments",
                "displayText": "",
                "attachments": [attachment],
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            web_ctx,
        )
        web_task = get_agent_task_registry().get(web_session.session_key)
        if web_task is not None:
            await web_task

        assert web_res.ok is True
        web_persisted = json.loads(web_manager.created_messages[0][2])
        assert web_persisted["text"] == "Describe these attachments"
        assert web_persisted["display_text"] == ""
        assert web_runner.run_calls[0]["message"] == "Describe these attachments"

        cli_session = FakeSession(
            session_key="agent:main:cli:cli-display",
            session_id="cli-display",
        )
        cli_manager = FakeSessionManager([cli_session])
        cli_runner = _RecordingTurnRunner()
        cli_ctx = make_ctx(session_manager=cli_manager, turn_runner=cli_runner)
        cli_res = await dispatcher.dispatch(
            "r2",
            "sessions.send",
            {
                "key": cli_session.session_key,
                "message": "Describe these attachments",
                "displayText": "",
                "attachments": [attachment],
                "_source": {"caller_kind": "cli", "channel_kind": "cli"},
            },
            cli_ctx,
        )
        cli_task = get_agent_task_registry().get(cli_session.session_key)
        if cli_task is not None:
            await cli_task

        assert cli_res.ok is True
        cli_persisted = json.loads(cli_manager.created_messages[0][2])
        assert cli_persisted["text"] == "Describe these attachments"
        assert "display_text" not in cli_persisted
        assert cli_runner.run_calls[0]["message"] == "Describe these attachments"

    @pytest.mark.asyncio
    async def test_send_persists_web_display_text_without_attachments(
        self,
        dispatcher,
    ):
        session = FakeSession(
            session_key="agent:main:webchat:hidden-confirmation",
            session_id="hidden-confirmation",
        )
        manager = FakeSessionManager([session])
        runner = _RecordingTurnRunner()
        ctx = make_ctx(session_manager=manager, turn_runner=runner)
        hidden_message = (
            "Confirmed request fields:\n"
            "- audience: decision owner\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->"
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": hidden_message,
                "displayText": "请帮我判断这份供应商续费材料",
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            ctx,
        )
        task = get_agent_task_registry().get(session.session_key)
        if task is not None:
            await task

        assert res.ok is True
        persisted = json.loads(manager.created_messages[0][2])
        assert persisted["text"] == hidden_message
        assert persisted["display_text"] == "请帮我判断这份供应商续费材料"
        assert persisted["attachments"] == []
        assert runner.run_calls[0]["message"] == ""
        assert runner.run_calls[0]["semantic_message"] == hidden_message

    @pytest.mark.asyncio
    async def test_send_sanitizes_legacy_web_preflight_confirmation_display_text(
        self,
        dispatcher,
    ):
        session = FakeSession(
            session_key="agent:main:webchat:legacy-hidden-confirmation",
            session_id="legacy-hidden-confirmation",
        )
        manager = FakeSessionManager([session])
        runner = _RecordingTurnRunner()
        ctx = make_ctx(session_manager=manager, turn_runner=runner)
        original = (
            "请帮我判断这份供应商续费材料：这个合同要不要签、拒绝还是谈判，并给我一份决策表。\n\n"
            "合同摘录：\n"
            "- 服务期：2026-07-01 到 2027-06-30\n"
            "- 价格：每月 $4,800，较上一年上涨 38%"
        )
        hidden_message = (
            "请帮我判断这份供应商续费材料：这个合同要不要签、拒绝还是谈判，并给我一份决策表。\n\n"
            f"{original}\n\n"
            "Confirmed request fields:\n"
            "- audience: decision owner\n"
            "- decision_question: 签不签合同\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->\n"
            "<!-- opensquilla:meta_preflight_run_id=01KTC2NFJ4ZXB20PSNTJEKYPS7 -->\n"
            "<!-- opensquilla:meta_preflight_fields=abc -->"
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": hidden_message,
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            ctx,
        )
        task = get_agent_task_registry().get(session.session_key)
        if task is not None:
            await task

        assert res.ok is True
        persisted = json.loads(manager.created_messages[0][2])
        assert persisted["text"] == hidden_message
        assert persisted["display_text"] == original
        assert "Confirmed request fields" not in persisted["display_text"]
        assert "opensquilla:meta_preflight_confirmed" not in persisted["display_text"]
        assert runner.run_calls[0]["message"] == original
        assert runner.run_calls[0]["semantic_message"] == hidden_message

    @pytest.mark.asyncio
    async def test_send_hides_marker_only_web_preflight_confirmation_display_text(
        self,
        dispatcher,
    ):
        session = FakeSession(
            session_key="agent:main:webchat:marker-only-hidden-confirmation",
            session_id="marker-only-hidden-confirmation",
        )
        manager = FakeSessionManager([session])
        runner = _RecordingTurnRunner()
        ctx = make_ctx(session_manager=manager, turn_runner=runner)
        hidden_message = (
            "<!-- opensquilla:meta_preflight_confirmed=1 -->\n"
            "<!-- opensquilla:meta_preflight_run_id=01KTCMARKERONLY -->"
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": hidden_message,
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            ctx,
        )
        task = get_agent_task_registry().get(session.session_key)
        if task is not None:
            await task

        assert res.ok is True
        persisted = json.loads(manager.created_messages[0][2])
        assert persisted["text"] == hidden_message
        assert persisted["display_text"] == ""
        assert runner.run_calls[0]["message"] == ""
        assert runner.run_calls[0]["semantic_message"] == hidden_message

    @pytest.mark.asyncio
    async def test_web_large_paste_is_normalized_before_turn_runner(
        self,
        dispatcher,
        tmp_path,
    ):
        raw = "a" * LARGE_PASTE_CHARS
        placeholder = "Please process the attached pasted text."
        web_session = FakeSession(
            session_key="agent:main:webchat:web-large-paste",
            session_id="web-large-paste",
        )
        web_manager = FakeSessionManager([web_session])
        web_runner = _RecordingTurnRunner()
        web_ctx = make_ctx(
            session_manager=web_manager,
            turn_runner=web_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-web-large-paste",
            "sessions.send",
            {
                "key": web_session.session_key,
                "message": raw,
                "inputProvenance": {"kind": "webchat_clip", "surface": "test"},
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            web_ctx,
        )
        web_task = get_agent_task_registry().get(web_session.session_key)
        if web_task is not None:
            await web_task

        assert res.ok is True
        assert web_runner.run_calls[0]["message"] == placeholder
        assert web_runner.run_calls[0]["semantic_message"] == placeholder
        runner_attachments = web_runner.run_calls[0]["attachments"]
        assert len(runner_attachments) == 1
        assert runner_attachments[0]["kind"] == "attachment_ref"
        assert runner_attachments[0]["source"] == "input_normalization"
        assert runner_attachments[0]["type"] == "text/plain"
        assert runner_attachments[0]["name"].startswith("webchat-paste-")
        assert "data" not in runner_attachments[0]
        assert runner_attachments[0]["_provider_inline_policy"] == "preview_only"
        material_path = transcript_material_path(
            tmp_path,
            web_session.session_id,
            runner_attachments[0]["sha256"],
        )
        assert material_path.read_text(encoding="utf-8") == raw

        persisted = json.loads(web_manager.created_messages[0][2])
        assert persisted["text"] == placeholder
        assert len(persisted["attachments"]) == 1
        assert persisted["attachments"][0]["sha256_ref"] == runner_attachments[0]["sha256"]
        assert persisted["attachments"][0]["name"].startswith("webchat-paste-")

        provenance = web_runner.run_calls[0]["input_provenance"]
        assert provenance["kind"] == "webchat_clip"
        assert provenance["surface"] == "test"
        assert provenance["input_normalization"]["guard_action"] == ("generated_text_attachment")
        assert provenance["input_normalization"]["original_chars"] == len(raw)
        assert provenance["input_normalization"]["generated_attachment_count"] == 1
        assert provenance["input_normalization"]["material_estimated_tokens"] == (
            estimate_text_tokens(raw)
        )

    @pytest.mark.asyncio
    async def test_web_large_paste_material_uses_canonical_session_id(
        self,
        dispatcher,
        tmp_path,
    ):
        raw = "a" * LARGE_PASTE_CHARS
        web_session = FakeSession(
            session_key="agent:main:webchat:web-large-paste",
            session_id="canonical-transcript-id",
        )
        web_manager = FakeSessionManager([web_session])
        web_runner = _RecordingTurnRunner()
        web_ctx = make_ctx(
            session_manager=web_manager,
            turn_runner=web_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-web-large-paste-canonical-id",
            "sessions.send",
            {
                "key": web_session.session_key,
                "message": raw,
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
            },
            web_ctx,
        )
        web_task = get_agent_task_registry().get(web_session.session_key)
        if web_task is not None:
            await web_task

        assert res.ok is True
        runtime_attachment = web_runner.run_calls[0]["attachments"][0]
        assert runtime_attachment["scope"] == web_session.session_id
        canonical_path = transcript_material_path(
            tmp_path,
            web_session.session_id,
            runtime_attachment["sha256"],
        )
        suffix_path = transcript_material_path(
            tmp_path,
            web_session.session_key.rsplit(":", 1)[-1],
            runtime_attachment["sha256"],
        )
        assert canonical_path.read_text(encoding="utf-8") == raw
        assert not suffix_path.exists()

    @pytest.mark.asyncio
    async def test_sessions_send_large_paste_defaults_to_web_guard(
        self,
        dispatcher,
        tmp_path,
    ):
        raw = "a" * LARGE_PASTE_CHARS
        placeholder = "Please process the attached pasted text."
        web_session = FakeSession(
            session_key="agent:main:webchat:untagged-large-paste",
            session_id="untagged-large-paste",
        )
        web_manager = FakeSessionManager([web_session])
        web_runner = _RecordingTurnRunner()
        web_ctx = make_ctx(
            session_manager=web_manager,
            turn_runner=web_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-untagged-large-paste",
            "sessions.send",
            {
                "key": web_session.session_key,
                "message": raw,
            },
            web_ctx,
        )
        web_task = get_agent_task_registry().get(web_session.session_key)
        if web_task is not None:
            await web_task

        assert res.ok is True
        assert web_runner.run_calls[0]["message"] == placeholder
        assert web_runner.run_calls[0]["semantic_message"] == placeholder
        runner_attachments = web_runner.run_calls[0]["attachments"]
        assert len(runner_attachments) == 1
        assert runner_attachments[0]["kind"] == "attachment_ref"
        assert runner_attachments[0]["source"] == "input_normalization"
        assert runner_attachments[0]["type"] == "text/plain"
        assert runner_attachments[0]["name"].startswith("webchat-paste-")
        assert "data" not in runner_attachments[0]

        persisted = json.loads(web_manager.created_messages[0][2])
        assert persisted["text"] == placeholder
        assert len(persisted["attachments"]) == 1
        assert persisted["attachments"][0]["sha256_ref"] == runner_attachments[0]["sha256"]
        assert persisted["attachments"][0]["name"].startswith("webchat-paste-")

        provenance = web_runner.run_calls[0]["input_provenance"]
        assert provenance["input_normalization"]["guard_action"] == ("generated_text_attachment")
        assert provenance["input_normalization"]["original_chars"] == len(raw)
        assert provenance["input_normalization"]["generated_attachment_count"] == 1
        assert provenance["input_normalization"]["material_estimated_tokens"] == (
            estimate_text_tokens(raw)
        )

    @pytest.mark.asyncio
    async def test_cli_large_message_is_not_auto_attachmentized(
        self,
        dispatcher,
    ):
        raw = "a" * LARGE_PASTE_CHARS
        cli_session = FakeSession(
            session_key="agent:main:cli:cli-large-paste",
            session_id="cli-large-paste",
        )
        cli_manager = FakeSessionManager([cli_session])
        cli_runner = _RecordingTurnRunner()
        cli_ctx = make_ctx(session_manager=cli_manager, turn_runner=cli_runner)

        res = await dispatcher.dispatch(
            "r-cli-large-paste",
            "sessions.send",
            {
                "key": cli_session.session_key,
                "message": raw,
                "_source": {"caller_kind": "cli", "channel_kind": "cli"},
            },
            cli_ctx,
        )
        cli_task = get_agent_task_registry().get(cli_session.session_key)
        if cli_task is not None:
            await cli_task

        assert res.ok is True
        assert cli_manager.created_messages[0][2] == raw
        assert cli_runner.run_calls[0]["message"] == raw
        assert cli_runner.run_calls[0]["semantic_message"] == raw
        assert cli_runner.run_calls[0]["attachments"] == []
        assert "input_normalization" not in cli_runner.run_calls[0]["input_provenance"]

    @pytest.mark.asyncio
    async def test_chat_send_large_web_paste_uses_sessions_guard(
        self,
        dispatcher,
        tmp_path,
    ):
        assert rpc_chat._handle_chat_send is not None
        raw = "a" * LARGE_PASTE_CHARS
        placeholder = "Please process the attached pasted text."
        attachment = {"type": "text/plain", "data": "bm90ZQ==", "name": "note.txt"}
        chat_session = FakeSession(
            session_key="agent:main:webchat:chat-large-paste",
            session_id="chat-large-paste",
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        chat_ctx = make_ctx(
            session_manager=chat_manager,
            turn_runner=chat_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-chat-large-paste",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": raw,
                "displayText": "",
                "attachments": [attachment],
            },
            chat_ctx,
        )
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task

        assert res.ok is True
        assert chat_runner.run_calls[0]["message"] == placeholder
        assert chat_runner.run_calls[0]["semantic_message"] == placeholder
        assert len(chat_runner.run_calls[0]["attachments"]) == 2
        assert chat_runner.run_calls[0]["attachments"][0]["kind"] == "attachment_ref"
        assert "data" not in chat_runner.run_calls[0]["attachments"][0]
        assert chat_runner.run_calls[0]["attachments"][0]["name"].startswith("webchat-paste-")
        assert chat_runner.run_calls[0]["attachments"][1]["name"] == "note.txt"
        persisted = json.loads(chat_manager.created_messages[0][2])
        assert persisted["text"] == placeholder
        assert persisted["display_text"] == ""

    @pytest.mark.asyncio
    async def test_chat_send_forwards_display_text_without_attachments(
        self,
        dispatcher,
    ):
        assert rpc_chat._handle_chat_send is not None
        chat_session = FakeSession(
            session_key="agent:main:webchat:chat-hidden-confirmation",
            session_id="chat-hidden-confirmation",
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        chat_ctx = make_ctx(session_manager=chat_manager, turn_runner=chat_runner)
        hidden_message = (
            "Confirmed request fields:\n"
            "- audience: decision owner\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->"
        )

        res = await dispatcher.dispatch(
            "r-chat-hidden-confirmation",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": hidden_message,
                "displayText": "请帮我判断这份供应商续费材料",
            },
            chat_ctx,
        )
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task

        assert res.ok is True
        persisted = json.loads(chat_manager.created_messages[0][2])
        assert persisted["text"] == hidden_message
        assert persisted["display_text"] == "请帮我判断这份供应商续费材料"
        assert persisted["attachments"] == []
        assert chat_runner.run_calls[0]["message"] == ""
        assert chat_runner.run_calls[0]["semantic_message"] == hidden_message

    @pytest.mark.asyncio
    async def test_chat_send_client_normalized_paste_preserves_provenance(
        self,
        dispatcher,
        tmp_path,
    ):
        assert rpc_chat._handle_chat_send is not None
        raw = "a" * LARGE_PASTE_CHARS
        placeholder = "Please process the attached pasted text."
        attachment = {
            "type": "text/plain",
            "mime": "text/plain",
            "data": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "name": "webchat-paste-20260531-000000.txt",
        }
        client_provenance = {
            "kind": "web_message",
            "source": "WebChat",
            "input_normalization": {
                "source": "input_normalization",
                "original_chars": len(raw),
                "material_estimated_tokens": estimate_text_tokens(raw),
                "marker_score": 0,
                "generated_attachment_count": 1,
                "guard_action": "generated_text_attachment",
            },
        }
        chat_session = FakeSession(
            session_key="agent:main:webchat:client-normalized-paste",
            session_id="client-normalized-paste",
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        chat_ctx = make_ctx(
            session_manager=chat_manager,
            turn_runner=chat_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-chat-client-normalized-paste",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": placeholder,
                "displayText": placeholder,
                "attachments": [attachment],
                "inputProvenance": client_provenance,
            },
            chat_ctx,
        )
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task

        assert res.ok is True
        assert chat_runner.run_calls[0]["message"] == placeholder
        assert chat_runner.run_calls[0]["semantic_message"] == placeholder
        attachments = chat_runner.run_calls[0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["kind"] == "attachment_ref"
        assert attachments[0]["source"] == "input_normalization"
        assert "data" not in attachments[0]
        assert attachments[0]["_provider_inline_policy"] == "preview_only"
        provenance = chat_runner.run_calls[0]["input_provenance"]
        assert provenance["kind"] == "web_message"
        assert provenance["source"] == "WebChat"
        assert provenance["input_normalization"]["guard_action"] == ("generated_text_attachment")
        assert provenance["input_normalization"]["original_chars"] == len(raw)
        assert provenance["input_normalization"]["material_estimated_tokens"] == (
            estimate_text_tokens(raw)
        )

    @pytest.mark.asyncio
    async def test_chat_send_client_normalized_paste_without_provenance_is_inferred(
        self,
        dispatcher,
        tmp_path,
    ):
        assert rpc_chat._handle_chat_send is not None
        raw = "界" * LARGE_PASTE_CHARS
        placeholder = "Please process the attached pasted text."
        attachment = {
            "type": "text/plain",
            "mime": "text/plain",
            "data": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "name": "webchat-paste-20260531-000000.txt",
        }
        chat_session = FakeSession(
            session_key="agent:main:webchat:client-normalized-no-provenance",
            session_id="client-normalized-no-provenance",
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        chat_ctx = make_ctx(
            session_manager=chat_manager,
            turn_runner=chat_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-chat-client-normalized-no-provenance",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": placeholder,
                "displayText": placeholder,
                "attachments": [attachment],
            },
            chat_ctx,
        )
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task

        assert res.ok is True
        assert chat_runner.run_calls[0]["message"] == placeholder
        assert chat_runner.run_calls[0]["semantic_message"] == placeholder
        attachments = chat_runner.run_calls[0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["kind"] == "attachment_ref"
        assert attachments[0]["source"] == "input_normalization"
        assert "data" not in attachments[0]
        material_path = transcript_material_path(
            tmp_path,
            chat_session.session_id,
            attachments[0]["sha256"],
        )
        assert material_path.read_text(encoding="utf-8") == raw
        provenance = chat_runner.run_calls[0]["input_provenance"]
        assert provenance["input_normalization"]["guard_action"] == ("generated_text_attachment")
        assert provenance["input_normalization"]["original_chars"] == len(raw)
        assert provenance["input_normalization"]["material_estimated_tokens"] == (
            estimate_text_tokens(raw)
        )

    @pytest.mark.asyncio
    async def test_chat_send_client_normalized_paste_server_metadata_wins(
        self,
        dispatcher,
        tmp_path,
    ):
        assert rpc_chat._handle_chat_send is not None
        raw = "界" * LARGE_PASTE_CHARS
        placeholder = "Please process the attached pasted text."
        attachment = {
            "type": "text/plain",
            "mime": "text/plain",
            "data": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "name": "webchat-paste-20260531-000000.txt",
        }
        client_provenance = {
            "kind": "web_message",
            "input_normalization": {
                "source": "input_normalization",
                "original_chars": 1,
                "material_estimated_tokens": 1,
                "marker_score": 0,
                "generated_attachment_count": 1,
                "guard_action": "generated_text_attachment",
            },
        }
        chat_session = FakeSession(
            session_key="agent:main:webchat:client-normalized-server-wins",
            session_id="client-normalized-server-wins",
        )
        chat_manager = FakeSessionManager([chat_session])
        chat_runner = _RecordingTurnRunner()
        chat_ctx = make_ctx(
            session_manager=chat_manager,
            turn_runner=chat_runner,
            config=_ctx_config_with_media_root(tmp_path),
        )

        res = await dispatcher.dispatch(
            "r-chat-client-normalized-server-wins",
            "chat.send",
            {
                "sessionKey": chat_session.session_key,
                "message": placeholder,
                "displayText": placeholder,
                "attachments": [attachment],
                "inputProvenance": client_provenance,
            },
            chat_ctx,
        )
        chat_task = get_agent_task_registry().get(chat_session.session_key)
        if chat_task is not None:
            await chat_task

        assert res.ok is True
        provenance = chat_runner.run_calls[0]["input_provenance"]
        assert provenance["kind"] == "web_message"
        assert provenance["input_normalization"]["original_chars"] == len(raw)
        assert provenance["input_normalization"]["material_estimated_tokens"] == (
            estimate_text_tokens(raw)
        )

    @pytest.mark.asyncio
    async def test_send_rejects_aggregate_attachment_cap_before_start_and_evict(
        self, dispatcher, ctx_with_sessions, session
    ):
        one_pdf = _exact_pdf(MAX_TOTAL_ATTACHMENT_BYTES // 3 + 1)
        assert len(one_pdf) < MAX_STAGED_PDF_BYTES
        entries = {
            f"u-pdf-{index}": (
                one_pdf,
                {
                    "mime": "application/pdf",
                    "name": f"{index}.pdf",
                    "sha256": "x",
                    "size": len(one_pdf),
                },
            )
            for index in range(3)
        }
        store = _FakeUploadStore(entries)
        set_upload_store(store)  # type: ignore[arg-type]
        try:
            res = await dispatcher.dispatch(
                "r1",
                "sessions.send",
                {
                    "key": session.session_key,
                    "message": "hi",
                    "attachments": [
                        {
                            "file_uuid": file_uuid,
                            "mime": "application/pdf",
                            "name": meta["name"],
                        }
                        for file_uuid, (_payload, meta) in entries.items()
                    ],
                },
                ctx_with_sessions,
            )
        finally:
            set_upload_store(None)

        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"
        assert ctx_with_sessions.session_manager.created_messages == []
        assert store.evicted == []
        assert set(store.entries) == set(entries)

    @pytest.mark.asyncio
    async def test_send_staged_upload_persists_and_runs_with_material_ref(
        self,
        dispatcher,
        tmp_path,
        session,
    ):
        payload = b"%PDF-1.4\nbody\n"
        sha = hashlib.sha256(payload).hexdigest()
        store = _FakeUploadStore(
            {
                "u-pdf": (
                    payload,
                    {
                        "mime": "application/pdf",
                        "name": "r.pdf",
                        "sha256": sha,
                        "size": len(payload),
                    },
                )
            }
        )
        manager = FakeSessionManager([session])
        runner = _RecordingTurnRunner()
        cfg = GatewayConfig()
        cfg.attachments.media_root = str(tmp_path)
        ctx = make_ctx(session_manager=manager, config=cfg, turn_runner=runner)
        set_upload_store(store)  # type: ignore[arg-type]
        try:
            res = await dispatcher.dispatch(
                "r1",
                "sessions.send",
                {
                    "key": session.session_key,
                    "message": "summarise",
                    "attachments": [
                        {"file_uuid": "u-pdf", "mime": "application/pdf", "name": "r.pdf"}
                    ],
                },
                ctx,
            )
            task = get_agent_task_registry().get(session.session_key)
            if task is not None:
                await task
        finally:
            set_upload_store(None)

        assert res.ok is True
        assert store.evicted == ["u-pdf"]
        persisted = json.loads(manager.created_messages[0][2])
        persisted_att = persisted["attachments"][0]
        assert persisted_att == {
            "sha256_ref": sha,
            "name": "r.pdf",
            "mime": "application/pdf",
            "size": len(payload),
        }
        runtime_att = runner.run_calls[0]["attachments"][0]
        assert runtime_att["kind"] == "attachment_ref"
        assert runtime_att["sha256"] == sha
        assert runtime_att["scope"] == session.session_id
        assert "data" not in runtime_att
        assert "file_uuid" not in runtime_att
        assert (tmp_path / "transcripts" / session.session_id / sha).read_bytes() == payload

    @pytest.mark.asyncio
    async def test_send_admits_opaque_attachment_by_default(
        self, dispatcher, ctx_with_sessions, session
    ):
        # Default config: an unrendered binary attachment is admitted as an
        # opaque item instead of failing the send with INVALID_REQUEST.
        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "hi",
                "attachments": [
                    {"type": "application/x-shellscript", "data": "AA==", "name": "x.sh"}
                ],
            },
            ctx_with_sessions,
        )
        assert res.ok is True

    @pytest.mark.asyncio
    async def test_send_rejects_opaque_attachment_when_admission_disabled(
        self, dispatcher, session
    ):
        # attachments.accept_opaque=false restores the legacy fail-closed
        # admission gate for unrendered media types end-to-end.
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            config=GatewayConfig(
                memory={"flush_enabled": False},
                attachments={"accept_opaque": False},
            ),
        )
        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {
                "key": session.session_key,
                "message": "hi",
                "attachments": [{"type": "application/x-shellscript", "data": "AA=="}],
            },
            ctx,
        )
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_send_uses_agent_registry_model_when_session_model_missing(
        self, dispatcher, tmp_path
    ):
        session = FakeSession(session_key="agent:ops:abc123", agent_id="ops", model=None)
        manager = FakeSessionManager([session])
        agent_workspace = tmp_path / "ops-workspace"
        cfg = GatewayConfig(
            agents=[
                AgentEntryConfig(
                    id="ops",
                    model="agent/default",
                    workspace=str(agent_workspace),
                )
            ]
        )
        registry = AgentRegistry(cfg, persist_changes=False)
        runner = _RecordingTurnRunner()
        ctx = make_ctx(
            session_manager=manager,
            config=cfg,
            agent_registry=registry,
            turn_runner=runner,
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.send",
            {"key": session.session_key, "message": "hello"},
            ctx,
        )
        task = get_agent_task_registry().get(session.session_key)
        if task is not None:
            await task

        assert res.ok is True
        assert runner.run_calls[0]["model"] == "agent/default"
        assert runner.run_calls[0]["tool_context"].workspace_dir == str(agent_workspace)


class TestSessionsSteer:
    @pytest.mark.asyncio
    async def test_steer_persists_and_injects_into_active_task(
        self,
        dispatcher,
        session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[dict[str, Any]] = []

        class Runtime:
            async def active_task_id(self, key: str) -> str | None:
                assert key == session.session_key
                return "turn-running"

            async def steer(self, key: str, message: str, **kwargs: Any) -> str | None:
                calls.append({"key": key, "message": message, **kwargs})
                return "turn-running"

        emitted = _capture_compaction_emits(monkeypatch)
        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=Runtime())
        res = await dispatcher.dispatch(
            "r-steer",
            "sessions.steer",
            {
                "key": session.session_key,
                "message": "change direction",
                "clientMessageId": "client-steer",
                "surfaceId": "tui:test",
            },
            ctx,
        )

        assert res.ok is True
        assert res.payload["accepted"] is True
        assert res.payload["turn_id"] == "turn-running"
        assert manager.created_messages == [
            (session.session_key, "user", "change direction")
        ]
        assert calls[0]["persisted_user_message_id"] == "msg-1"
        assert calls[0]["client_message_id"] == "client-steer"
        assert manager.updated_turn_contexts[0][2]["disposition"] == "steering"
        assert manager.updated_turn_contexts[0][2]["turn_id"] == "turn-running"
        assert emitted[0][1] == "session.event.steer"

    @pytest.mark.asyncio
    async def test_steer_race_rolls_back_and_reports_idle(self, dispatcher, session) -> None:
        class Runtime:
            async def active_task_id(self, _key: str) -> str | None:
                return "turn-ending"

            async def steer(self, _key: str, _message: str, **_kwargs: Any) -> None:
                return None

        manager = FakeSessionManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=Runtime())
        res = await dispatcher.dispatch(
            "r-steer-race",
            "sessions.steer",
            {"key": session.session_key, "message": "late"},
            ctx,
        )

        assert res.ok is True
        assert res.payload == {
            "status": "idle",
            "accepted": False,
            "key": session.session_key,
        }
        assert manager.removed_messages == [(session.session_key, "msg-1")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rollback_failure", ["missing", "exception"])
    async def test_steer_race_dirty_rollback_fails_closed_without_duplicate_fallback(
        self,
        dispatcher,
        session,
        rollback_failure: str,
    ) -> None:
        class Runtime:
            async def active_task_id(self, _key: str) -> str | None:
                return "turn-ending"

            async def steer(self, _key: str, _message: str, **_kwargs: Any) -> None:
                return None

        class DirtyManager(FakeSessionManager):
            async def remove_message(self, key: str, message_id: str) -> bool:
                self.removed_messages.append((key, message_id))
                if rollback_failure == "exception":
                    raise OSError("storage unavailable")
                return False

        manager = DirtyManager([session])
        ctx = make_ctx(session_manager=manager, task_runtime=Runtime())
        res = await dispatcher.dispatch(
            "r-steer-race-dirty",
            "sessions.steer",
            {
                "key": session.session_key,
                "message": "late but durable",
                "clientMessageId": "client-dirty-steer",
            },
            ctx,
        )

        assert res.ok is False
        assert res.error.code == "STEER_RACE_DIRTY"
        assert res.error.retryable is False
        assert res.error.details["fallback_safe"] is False
        assert res.error.details["orphan_message_id"] == "msg-1"
        assert manager.created_messages == [
            (session.session_key, "user", "late but durable")
        ]
        assert manager.removed_messages == [(session.session_key, "msg-1")]
        assert manager.updated_turn_contexts[-1][2]["disposition"] == "rejected"
        assert manager.updated_turn_contexts[-1][2]["client_message_id"] == (
            "client-dirty-steer"
        )


class TestSessionsAbort:
    @pytest.mark.asyncio
    async def test_abort_valid(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1", "sessions.abort", {"key": session.session_key}, ctx_with_sessions
        )
        assert res.ok is True

    @pytest.mark.asyncio
    async def test_abort_passes_cancel_source_to_runtime(self, dispatcher, session):
        class Runtime:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def cancel(
                self,
                session_key: str | None = None,
                source: str | None = None,
                reason: str | None = None,
            ) -> int:
                self.calls.append({"session_key": session_key, "source": source, "reason": reason})
                return 1

        runtime = Runtime()
        ctx = make_ctx(session_manager=FakeSessionManager([session]), task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.abort",
            {"key": session.session_key, "source": "webui_escape"},
            ctx,
        )

        assert res.ok is True
        assert runtime.calls == [
            {
                "session_key": session.session_key,
                "source": "webui_escape",
                "reason": "user_abort",
            }
        ]

    @pytest.mark.asyncio
    async def test_abort_with_task_id_cancels_only_that_runtime_task(self, dispatcher, session):
        class Runtime:
            def __init__(self) -> None:
                self.cancel_calls: list[dict[str, Any]] = []
                self.wait_calls: list[str] = []

            async def list(self, session_key: str | None = None):
                assert session_key == session.session_key
                return [
                    SimpleNamespace(task_id="task-old", status="running"),
                    SimpleNamespace(task_id="task-new", status="queued"),
                ]

            async def cancel(
                self,
                task_id: str | None = None,
                session_key: str | None = None,
                source: str | None = None,
                reason: str | None = None,
            ) -> int:
                self.cancel_calls.append(
                    {
                        "task_id": task_id,
                        "session_key": session_key,
                        "source": source,
                        "reason": reason,
                    }
                )
                return 1

            async def wait(self, task_id: str):
                self.wait_calls.append(task_id)
                return SimpleNamespace(task_id=task_id, status="cancelled")

        runtime = Runtime()
        ctx = make_ctx(session_manager=FakeSessionManager([session]), task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.abort",
            {"key": session.session_key, "task_id": "task-old", "source": "webui_stop"},
            ctx,
        )

        assert res.ok is True
        assert runtime.cancel_calls == [
            {
                "task_id": "task-old",
                "session_key": None,
                "source": "webui_stop",
                "reason": "user_abort",
            }
        ]
        assert runtime.wait_calls == ["task-old"]

    @pytest.mark.asyncio
    async def test_chat_user_stop_cancels_the_whole_session_even_with_a_stale_task_id(
        self, dispatcher, session
    ):
        class Runtime:
            def __init__(self) -> None:
                self.cancel_calls: list[dict[str, Any]] = []

            async def list(self, session_key: str | None = None):
                assert session_key == session.session_key
                return [SimpleNamespace(task_id="task-new", status="running")]

            async def cancel(
                self,
                task_id: str | None = None,
                session_key: str | None = None,
                source: str | None = None,
                reason: str | None = None,
            ) -> int:
                self.cancel_calls.append(
                    {
                        "task_id": task_id,
                        "session_key": session_key,
                        "source": source,
                        "reason": reason,
                    }
                )
                return 1

            async def wait(self, task_id: str):
                return SimpleNamespace(task_id=task_id, status="cancelled")

        runtime = Runtime()
        ctx = make_ctx(session_manager=FakeSessionManager([session]), task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "chat.abort",
            {
                "sessionKey": session.session_key,
                "taskId": "task-old",
                "source": "webui_stop",
            },
            ctx,
        )

        assert res.ok is True
        assert runtime.cancel_calls == [
            {
                "task_id": None,
                "session_key": session.session_key,
                "source": "webui_stop",
                "reason": "user_abort",
            }
        ]

    @pytest.mark.asyncio
    async def test_chat_user_stop_cancels_all_descendant_subagent_sessions(
        self, dispatcher, session, monkeypatch
    ):
        child_key = "agent:worker:subagent:child"
        grandchild_key = "agent:reviewer:subagent:grandchild"
        unrelated_key = "agent:worker:subagent:unrelated"
        sessions = [
            session,
            FakeSession(
                session_key=child_key,
                spawned_by=session.session_key,
                parent_session_key=session.session_key,
            ),
            FakeSession(
                session_key=grandchild_key,
                spawned_by=child_key,
                parent_session_key=child_key,
            ),
            FakeSession(session_key=unrelated_key, spawned_by="agent:main:webchat:other"),
        ]

        class TreeSessionManager(FakeSessionManager):
            async def list_sessions(
                self,
                *,
                spawned_by: str | None = None,
                limit: int = 100,
                offset: int = 0,
            ) -> list[dict[str, Any]]:
                rows = list(self._storage._sessions.values())
                if spawned_by is not None:
                    rows = [row for row in rows if row.spawned_by == spawned_by]
                return [row.__dict__ for row in rows[offset : offset + limit]]

        class Runtime:
            def __init__(self) -> None:
                self.cancel_calls: list[str] = []
                self.successful_cancel_calls: list[str] = []
                self.wait_calls: list[str] = []
                self.active_tasks = {
                    session.session_key: "task-parent",
                    grandchild_key: "task-grandchild",
                }
                self.cancelled_tasks: dict[str, str] = {}

            async def list(self, session_key: str | None = None):
                task_id = self.active_tasks.get(session_key or "")
                if task_id is None:
                    return []
                status = "queued" if session_key == grandchild_key else "running"
                return [SimpleNamespace(task_id=task_id, status=status)]

            async def cancel(
                self,
                session_key: str | None = None,
                source: str | None = None,
                reason: str | None = None,
            ) -> int:
                assert source == "webui_stop"
                assert reason == "user_abort"
                assert session_key is not None
                self.cancel_calls.append(session_key)
                task_id = self.active_tasks.get(session_key)
                if task_id is None:
                    return 0
                self.successful_cancel_calls.append(session_key)
                self.cancelled_tasks[task_id] = session_key
                return 1

            async def wait(self, task_id: str):
                self.wait_calls.append(task_id)
                session_key = self.cancelled_tasks.pop(task_id)
                self.active_tasks.pop(session_key, None)
                if task_id == "task-parent":
                    # Reproduce the spawn race: the child session row existed
                    # during the first scan, but its runtime task appears only
                    # while the parent cancellation is draining.
                    self.active_tasks[child_key] = "task-child"
                return SimpleNamespace(task_id=task_id, status="cancelled")

        background_cancel_calls: list[str] = []
        approval_cancel_calls: list[str] = []
        emitted: list[tuple[str, str, dict[str, Any]]] = []

        async def cancel_background(session_key: str) -> int:
            background_cancel_calls.append(session_key)
            return 1

        class ApprovalQueue:
            def resolve_pending_for_session(self, session_key: str, *, approved: bool) -> int:
                assert approved is False
                approval_cancel_calls.append(session_key)
                return 1

        async def emit(
            _ctx: RpcContext,
            session_key: str,
            event_name: str,
            payload: dict[str, Any],
        ) -> None:
            emitted.append((session_key, event_name, payload))

        monkeypatch.setattr(
            "opensquilla.gateway.subagent_announce.cancel_background_completion_for_session",
            cancel_background,
        )
        monkeypatch.setattr(
            "opensquilla.gateway.approval_queue.get_approval_queue",
            lambda: ApprovalQueue(),
        )
        monkeypatch.setattr(rpc_sessions, "_emit_to_subscribers", emit)

        runtime = Runtime()
        manager = TreeSessionManager(sessions)
        ctx = make_ctx(session_manager=manager, task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "chat.abort",
            {"sessionKey": session.session_key, "source": "webui_stop"},
            ctx,
        )

        assert res.ok is True
        assert res.payload["aborted"] is True
        assert res.payload["cancelled_tasks"] == 3
        assert res.payload["cancelled_sessions"] == 3
        assert runtime.cancel_calls == [
            session.session_key,
            child_key,
            grandchild_key,
            child_key,
        ]
        assert runtime.successful_cancel_calls == [session.session_key, grandchild_key, child_key]
        assert runtime.wait_calls == ["task-parent", "task-grandchild", "task-child"]
        assert background_cancel_calls == [session.session_key, child_key, grandchild_key]
        assert approval_cancel_calls == [
            session.session_key,
            child_key,
            grandchild_key,
            child_key,
        ]
        assert unrelated_key not in runtime.cancel_calls
        assert emitted == [
            (
                session.session_key,
                "sessions.changed",
                {
                    "schema_version": 1,
                    "key": session.session_key,
                    "reason": "task_terminal",
                    "run_status": "cancelled",
                    "last_task": {
                        "status": "cancelled",
                        "terminal_reason": "user_abort",
                    },
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_abort_waits_for_runtime_task_to_settle(self, dispatcher, session):
        class Runtime:
            def __init__(self) -> None:
                self.status = "running"
                self.cancel_calls: list[dict[str, Any]] = []
                self.wait_calls: list[str] = []

            async def list(self, session_key: str | None = None):
                assert session_key == session.session_key
                return [SimpleNamespace(task_id="task-live", status=self.status)]

            async def cancel(
                self,
                session_key: str | None = None,
                source: str | None = None,
                reason: str | None = None,
            ) -> int:
                self.cancel_calls.append(
                    {"session_key": session_key, "source": source, "reason": reason}
                )
                return 1

            async def wait(self, task_id: str):
                self.wait_calls.append(task_id)
                self.status = "cancelled"
                return SimpleNamespace(task_id=task_id, status=self.status)

        runtime = Runtime()
        ctx = make_ctx(session_manager=FakeSessionManager([session]), task_runtime=runtime)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.abort",
            {"key": session.session_key, "source": "webui_stop"},
            ctx,
        )

        assert res.ok is True
        assert runtime.cancel_calls == [
            {
                "session_key": session.session_key,
                "source": "webui_stop",
                "reason": "user_abort",
            }
        ]
        assert runtime.wait_calls == ["task-live"]

    @pytest.mark.asyncio
    async def test_abort_no_manager(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch("r1", "sessions.abort", {"key": "any"}, ctx_no_manager)
        assert res.ok is True  # no-op

    @pytest.mark.asyncio
    async def test_abort_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.abort", {"key": "nonexistent"}, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"


class TestSessionsPatch:
    @pytest.mark.asyncio
    async def test_patch_valid(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.patch",
            {"key": session.session_key, "displayName": "New Name"},
            ctx_with_sessions,
        )
        assert res.ok is True
        assert res.payload["key"] == session.session_key
        assert "displayName" in res.payload["updated"]

    @pytest.mark.asyncio
    async def test_patch_model_to_none_clears_canonical_session_pin(
        self,
        dispatcher,
        ctx_with_sessions,
        session,
    ):
        session.model = "provider/pinned"

        patched = await dispatcher.dispatch(
            "r1",
            "sessions.patch",
            {"key": session.session_key, "model": None},
            ctx_with_sessions,
        )
        resolved = await dispatcher.dispatch(
            "r2",
            "sessions.resolve",
            {"key": session.session_key},
            ctx_with_sessions,
        )

        assert patched.ok is True
        assert "model" in patched.payload["updated"]
        assert resolved.ok is True
        assert resolved.payload["model"] is None

    @pytest.mark.asyncio
    async def test_patch_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.patch",
            {"key": "nonexistent", "displayName": "x"},
            ctx_with_sessions,
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"


class TestSessionsReset:
    @pytest.mark.asyncio
    async def test_reset_valid(self, dispatcher, ctx_with_sessions, session):
        before = session.session_id
        res = await dispatcher.dispatch(
            "r1", "sessions.reset", {"key": session.session_key}, ctx_with_sessions
        )
        assert res.ok is True
        assert res.payload["session_id"] != before
        assert res.payload["previous_session_id"] == before
        assert res.payload["epoch"] == 1

    @pytest.mark.asyncio
    async def test_reset_allowed_for_operator_write_scope(self, dispatcher, session):
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            scopes=["operator.read", "operator.write"],
        )

        res = await dispatcher.dispatch("r1", "sessions.reset", {"key": session.session_key}, ctx)

        assert res.ok is True
        assert ctx.session_manager.applied_intents == [(session.session_key, "reset_same_key")]

    @pytest.mark.asyncio
    async def test_reset_lets_recently_completed_runtime_task_settle(self, dispatcher, session):
        class RuntimeSettlesAfterDoneRace:
            def __init__(self) -> None:
                self.status = "running"
                self.wait_calls: list[str] = []
                self.cancel_calls = 0
                self.cancelled = False

            async def list(self, session_key: str | None = None):
                assert session_key == session.session_key
                return [SimpleNamespace(task_id="task-race", status=self.status)]

            async def wait(self, task_id: str):
                self.wait_calls.append(task_id)
                self.status = "succeeded"
                return SimpleNamespace(task_id=task_id, status=self.status)

            async def cancel(self, session_key: str | None = None):
                self.cancel_calls += 1
                assert session_key == session.session_key
                if self.status in {"queued", "running"}:
                    self.cancelled = True
                    self.status = "cancelled"
                    return 1
                return 0

        runtime = RuntimeSettlesAfterDoneRace()
        ctx = make_ctx(session_manager=FakeSessionManager([session]), task_runtime=runtime)

        res = await dispatcher.dispatch("r1", "sessions.reset", {"key": session.session_key}, ctx)

        assert res.ok is True
        assert runtime.wait_calls == ["task-race"]
        assert runtime.cancel_calls == 1
        assert runtime.cancelled is False

    @pytest.mark.asyncio
    async def test_reset_allows_checkpoint_receipt_when_flush_receipt_is_degraded(
        self, dispatcher, session
    ):
        previous_session_id = session.session_id
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(id=1, content="message to preserve")]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(session, turn_id="cmp-reset", entries=manager.transcript)
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="raw",
                    result_status="parse_failed_archived",
                    flushed_paths=["memory/.raw_fallbacks/raw.md"],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "raw",
                        "result_status": "parse_failed_archived",
                        "flushed_paths": ["memory/.raw_fallbacks/raw.md"],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch("r1", "sessions.reset", {"key": session.session_key}, ctx)

        assert res.ok is True
        assert res.payload["flush_receipt"]["result_status"] == "parse_failed_archived"
        assert manager.applied_intents == [(session.session_key, "reset_same_key")]
        flush_kwargs = flush_service.execute.await_args.kwargs
        correlation = flush_kwargs["provider_request_correlation"]
        assert correlation.session_id == previous_session_id
        assert correlation.turn_id == flush_kwargs["turn_id"]
        assert correlation.execution_id != correlation.turn_id
        assert correlation.call_kind == "auxiliary.session_flush"

    @pytest.mark.asyncio
    async def test_reset_refuses_stale_checkpoint_receipt_for_later_transcript(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [
            SimpleNamespace(id=1, content="checkpointed"),
            SimpleNamespace(id=2, content="not checkpointed"),
        ]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(
                session,
                turn_id="cmp-reset-old",
                entries=manager.transcript[:1],
            )
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="error",
                    result_status="archive_failed",
                    flushed_paths=[],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "error",
                        "result_status": "archive_failed",
                        "flushed_paths": [],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch("r1", "sessions.reset", {"key": session.session_key}, ctx)

        assert res.ok is False
        assert res.error.code == "flush_disk_error"
        assert res.error.details["memory_safety_status"] == "unsafe"
        assert res.error.details["semantic_memory_status"] == "failed"
        assert manager.applied_intents == []

    @pytest.mark.asyncio
    async def test_reset_without_flush_service_allows_covering_checkpoint_receipt(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(id=1, content="message to preserve")]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(session, turn_id="cmp-reset", entries=manager.transcript)
        )
        ctx = make_ctx(session_manager=manager, flush_service=None)

        res = await dispatcher.dispatch("r1", "sessions.reset", {"key": session.session_key}, ctx)

        assert res.ok is True
        assert manager.applied_intents == [(session.session_key, "reset_same_key")]

    @pytest.mark.asyncio
    async def test_reset_skips_flush_when_session_reset_trigger_disabled(self, dispatcher, session):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(id=1, content="message to discard")]
        flush_service = SimpleNamespace(
            execute=AsyncMock(side_effect=AssertionError("reset flush should be disabled"))
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True, "flush_triggers": ["manual"]}),
        )

        res = await dispatcher.dispatch("r1", "sessions.reset", {"key": session.session_key}, ctx)

        assert res.ok is True
        assert "flush_receipt" not in res.payload
        flush_service.execute.assert_not_called()
        assert manager.applied_intents == [(session.session_key, "reset_same_key")]

    @pytest.mark.asyncio
    async def test_reset_without_flush_service_checkpoint_gate_uses_session_lock(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(id=1, content="message to preserve")]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(session, turn_id="cmp-reset", entries=manager.transcript)
        )
        turn_runner = _RecordingTurnRunner()
        lock = turn_runner._get_session_lock(session.session_key)
        await lock.acquire()
        ctx = make_ctx(
            session_manager=manager,
            flush_service=None,
            turn_runner=turn_runner,
        )
        reset_task = asyncio.create_task(
            dispatcher.dispatch(
                "r1",
                "sessions.reset",
                {"key": session.session_key},
                ctx,
            )
        )
        await asyncio.sleep(0)

        assert manager.applied_intents == []
        assert reset_task.done() is False

        lock.release()
        res = await reset_task

        assert res.ok is True
        assert manager.applied_intents == [(session.session_key, "reset_same_key")]

    @pytest.mark.asyncio
    async def test_reset_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.reset", {"key": "nonexistent"}, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"


class TestSessionsDelete:
    @pytest.mark.asyncio
    async def test_delete_valid(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1", "sessions.delete", {"key": session.session_key}, ctx_with_sessions
        )
        assert res.ok is True

    @pytest.mark.asyncio
    async def test_delete_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.delete", {"key": "nonexistent"}, ctx_with_sessions
        )
        # Bulk-delete returns ok=True but populates errors list for missing keys
        assert res.ok is True
        assert res.payload["deleted"] == []
        assert len(res.payload["errors"]) == 1

    @pytest.mark.asyncio
    async def test_delete_waits_for_session_lock(self, dispatcher, session):
        manager = FakeSessionManager([session])
        turn_runner = _RecordingTurnRunner()
        lock = turn_runner._get_session_lock(session.session_key)
        await lock.acquire()
        ctx = make_ctx(session_manager=manager, turn_runner=turn_runner)

        delete_task = asyncio.create_task(
            dispatcher.dispatch(
                "r1",
                "sessions.delete",
                {"key": session.session_key},
                ctx,
            )
        )
        await asyncio.sleep(0)

        assert await manager._storage.get_session(session.session_key) is session
        assert delete_task.done() is False

        lock.release()
        res = await delete_task

        assert res.ok is True
        assert await manager._storage.get_session(session.session_key) is None

    @pytest.mark.asyncio
    async def test_delete_legacy_alias_uses_canonical_session_lock(self, dispatcher):
        session = FakeSession(session_key="agent:main:webchat:default")
        manager = FakeSessionManager([session])
        turn_runner = _RecordingTurnRunner()
        canonical_lock = turn_runner._get_session_lock(session.session_key)
        await canonical_lock.acquire()
        ctx = make_ctx(session_manager=manager, turn_runner=turn_runner)

        delete_task = asyncio.create_task(
            dispatcher.dispatch(
                "r1",
                "sessions.delete",
                {"key": "webchat:default"},
                ctx,
            )
        )
        await asyncio.sleep(0)

        assert delete_task.done() is False
        assert await manager._storage.get_session(session.session_key) is session

        canonical_lock.release()
        res = await delete_task

        assert res.ok is True
        assert res.payload["deleted"] == ["webchat:default"]
        assert await manager._storage.get_session(session.session_key) is None


class TestSessionsCompact:
    @pytest.mark.asyncio
    async def test_compact_valid_uses_summary_compaction(
        self, dispatcher, ctx_with_sessions, session
    ):
        res = await dispatcher.dispatch(
            "r1", "sessions.compact", {"key": session.session_key}, ctx_with_sessions
        )
        assert res.ok is True
        assert res.payload["mode"] == "summary"
        assert res.payload["compacted"] is True
        assert ctx_with_sessions.session_manager.compact_calls[0][:2] == (
            session.session_key,
            ctx_with_sessions.config.context_budget_tokens,
        )
        assert ctx_with_sessions.session_manager.truncate_calls == []

    @pytest.mark.asyncio
    async def test_compact_allowed_for_operator_write_scope(self, dispatcher, session):
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            scopes=["operator.read", "operator.write"],
        )

        res = await dispatcher.dispatch("r1", "sessions.compact", {"key": session.session_key}, ctx)

        assert res.ok is True
        assert ctx.session_manager.compact_calls

    @pytest.mark.asyncio
    async def test_compact_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.compact", {"key": "nonexistent"}, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"


class TestSessionsTruncate:
    @pytest.mark.asyncio
    async def test_truncate_valid_preserves_hard_truncate_semantics(
        self, dispatcher, ctx_with_sessions, session
    ):
        res = await dispatcher.dispatch(
            "r1", "sessions.truncate", {"key": session.session_key}, ctx_with_sessions
        )

        assert res.ok is True
        assert res.payload["mode"] == "truncate"
        assert ctx_with_sessions.session_manager.truncate_calls == [(session.session_key, 20)]
        assert ctx_with_sessions.session_manager.compact_calls == []

    @pytest.mark.asyncio
    async def test_truncate_refuses_degraded_flush_receipt(self, dispatcher, session):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(content="message to preserve")]
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="raw",
                    integrity_ok=True,
                    output_coverage_status="ok",
                    missing_candidate_count=0,
                    invalid_candidate_count=0,
                    obligation_status="ok",
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.truncate", {"key": session.session_key}, ctx
        )

        assert res.ok is False
        assert res.error.code == "CONTEXT_FLUSH_FAILED"
        assert manager.truncate_calls == []

    @pytest.mark.asyncio
    async def test_truncate_allows_checkpoint_receipt_when_flush_receipt_is_degraded(
        self, dispatcher, session
    ):
        previous_session_id = session.session_id
        manager = FakeSessionManager([session])
        manager.transcript = [
            SimpleNamespace(id=1, content="message to remove"),
            SimpleNamespace(id=2, content="message to keep"),
        ]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(
                session,
                turn_id="cmp-truncate",
                entries=manager.transcript[:1],
            )
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="raw",
                    result_status="parse_failed_archived",
                    flushed_paths=["memory/.raw_fallbacks/raw.md"],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "raw",
                        "result_status": "parse_failed_archived",
                        "flushed_paths": ["memory/.raw_fallbacks/raw.md"],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.truncate",
            {"key": session.session_key, "maxMessages": 1},
            ctx,
        )

        assert res.ok is True
        assert res.payload["flush_receipt"]["result_status"] == "parse_failed_archived"
        assert manager.truncate_calls == [(session.session_key, 1)]
        flush_kwargs = flush_service.execute.await_args.kwargs
        correlation = flush_kwargs["provider_request_correlation"]
        assert correlation.session_id == previous_session_id
        assert correlation.turn_id == flush_kwargs["turn_id"]
        assert correlation.execution_id != correlation.turn_id
        assert correlation.call_kind == "auxiliary.session_flush"

    @pytest.mark.asyncio
    async def test_truncate_refuses_stale_checkpoint_for_later_removed_messages(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [
            SimpleNamespace(id=1, content="checkpointed"),
            SimpleNamespace(id=2, content="not checkpointed"),
        ]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(
                session,
                turn_id="cmp-truncate-old",
                entries=manager.transcript[:1],
            )
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="error",
                    result_status="archive_failed",
                    flushed_paths=[],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "error",
                        "result_status": "archive_failed",
                        "flushed_paths": [],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.truncate",
            {"key": session.session_key, "maxMessages": 0},
            ctx,
        )

        assert res.ok is False
        assert res.error.code == "CONTEXT_FLUSH_FAILED"
        assert res.error.details["memory_safety_status"] == "unsafe"
        assert res.error.details["semantic_memory_status"] == "failed"
        assert manager.truncate_calls == []

    @pytest.mark.asyncio
    async def test_truncate_without_flush_service_allows_covering_checkpoint_receipt(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [
            SimpleNamespace(id=1, content="message to remove"),
            SimpleNamespace(id=2, content="message to keep"),
        ]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(
                session,
                turn_id="cmp-truncate",
                entries=manager.transcript[:1],
            )
        )
        ctx = make_ctx(session_manager=manager, flush_service=None)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.truncate",
            {"key": session.session_key, "maxMessages": 1},
            ctx,
        )

        assert res.ok is True
        assert manager.truncate_calls == [(session.session_key, 1)]

    @pytest.mark.asyncio
    async def test_truncate_skips_flush_when_session_reset_trigger_disabled(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [
            SimpleNamespace(id=1, content="message to remove"),
            SimpleNamespace(id=2, content="message to keep"),
        ]
        flush_service = SimpleNamespace(
            execute=AsyncMock(side_effect=AssertionError("truncate flush should be disabled"))
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True, "flush_triggers": ["manual"]}),
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.truncate",
            {"key": session.session_key, "maxMessages": 1},
            ctx,
        )

        assert res.ok is True
        assert "flush_receipt" not in res.payload
        flush_service.execute.assert_not_called()
        assert manager.truncate_calls == [(session.session_key, 1)]

    @pytest.mark.asyncio
    async def test_truncate_refuses_orphaned_checkpoint_receipt(self, dispatcher, session):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(content="message to preserve")]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(
                session,
                turn_id="cmp-orphaned",
                entries=manager.transcript,
                status="receipt_orphaned",
            )
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="error",
                    result_status="archive_failed",
                    flushed_paths=[],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "error",
                        "result_status": "archive_failed",
                        "flushed_paths": [],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.truncate", {"key": session.session_key}, ctx
        )

        assert res.ok is False
        assert res.error.code == "CONTEXT_FLUSH_FAILED"
        assert res.error.details["memory_safety_status"] == "unsafe"
        assert res.error.details["semantic_memory_status"] == "failed"
        assert manager.truncate_calls == []


class TestSessionsContextCompact:
    @pytest.mark.asyncio
    async def test_context_compact_summarizes_instead_of_truncating(
        self, dispatcher, ctx_with_sessions, session
    ):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": session.session_key, "contextWindowTokens": 1234},
            ctx_with_sessions,
        )

        assert res.ok is True
        assert res.payload["key"] == session.session_key
        assert res.payload["compacted"] is True
        assert res.payload["applied"] is True
        assert res.payload["durability"] == "durable"
        assert res.payload["user_visible"] is True
        assert res.payload["mode"] == "summary"
        assert res.payload["summary_len"] == len(ctx_with_sessions.session_manager.compact_summary)
        assert res.payload["context_window_tokens"] == 1234
        compact_call = ctx_with_sessions.session_manager.compact_calls[0]
        assert compact_call[:2] == (session.session_key, 1234)
        assert ctx_with_sessions.session_manager.truncate_calls == []
        assert res.payload["tokens_before"] == 1200
        assert res.payload["tokens_after"] == 400
        assert res.payload["remaining_budget_tokens"] == 834
        assert res.payload["removed_count"] == 1
        assert res.payload["kept_count"] == 0
        correlation = ctx_with_sessions.session_manager.compact_kwargs[0][
            "provider_request_correlation"
        ]
        assert correlation.session_id == session.session_id
        assert correlation.turn_id.startswith("cmp_")
        assert correlation.execution_id
        assert correlation.call_kind == "auxiliary.compaction"

    @pytest.mark.asyncio
    async def test_context_compact_emits_started_and_completed_events(
        self,
        dispatcher,
        ctx_with_sessions,
        session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            rpc_sessions,
            "notify_compaction",
            lambda session_key, **payload: events.append((session_key, payload)),
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": session.session_key, "contextWindowTokens": 1234},
            ctx_with_sessions,
        )

        assert res.ok is True
        assert [(key, payload["status"]) for key, payload in events] == [
            (session.session_key, "started"),
            (session.session_key, "observed"),
            (session.session_key, "observed"),
            (session.session_key, "completed"),
        ]
        assert all(payload["source"] == "manual" for _, payload in events)
        assert all(payload["phase"] == "manual" for _, payload in events)
        compaction_ids = {payload.get("compaction_id") for _, payload in events}
        assert len(compaction_ids) == 1
        assert None not in compaction_ids
        assert [payload["event"] for _, payload in events] == [
            "compaction.triggered",
            "compaction.chunk_summarized",
            "compaction.summary_verified",
            "compaction.persisted",
        ]

    @pytest.mark.asyncio
    async def test_context_compact_emits_started_while_slow_compaction_is_running(
        self,
        dispatcher,
        session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        manager = SlowCompactionSessionManager([session])
        ctx = make_ctx(session_manager=manager)
        events: list[tuple[str, dict[str, Any]]] = []
        emitted = _capture_compaction_emits(monkeypatch)
        monkeypatch.setattr(
            rpc_sessions,
            "notify_compaction",
            lambda session_key, **payload: events.append((session_key, payload)),
        )

        task = asyncio.create_task(
            dispatcher.dispatch(
                "r1",
                "sessions.contextCompact",
                {"key": session.session_key, "contextWindowTokens": 1234},
                ctx,
            )
        )

        await asyncio.wait_for(manager.started.wait(), timeout=1.0)
        assert [payload["status"] for _, payload in events] == ["started"]
        assert [(key, event, payload["status"]) for key, event, payload in emitted] == [
            (session.session_key, "session.event.compaction", "started")
        ]
        assert task.done() is False

        manager.release.set()
        res = await asyncio.wait_for(task, timeout=1.0)

        assert res.ok is True
        assert [payload["status"] for _, payload in events] == [
            "started",
            "observed",
            "observed",
            "completed",
        ]
        assert [payload["status"] for _, _, payload in emitted] == [
            "started",
            "observed",
            "observed",
            "completed",
        ]

    @pytest.mark.asyncio
    async def test_context_compact_emits_cancelled_when_slow_compaction_is_cancelled(
        self,
        dispatcher,
        session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        manager = SlowCompactionSessionManager([session])
        ctx = make_ctx(session_manager=manager)
        events: list[tuple[str, dict[str, Any]]] = []
        emitted = _capture_compaction_emits(monkeypatch)
        monkeypatch.setattr(
            rpc_sessions,
            "notify_compaction",
            lambda session_key, **payload: events.append((session_key, payload)),
        )

        task = asyncio.create_task(
            dispatcher.dispatch(
                "r1",
                "sessions.contextCompact",
                {"key": session.session_key, "contextWindowTokens": 1234},
                ctx,
            )
        )
        await asyncio.wait_for(manager.started.wait(), timeout=1.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert [payload["status"] for _, payload in events] == [
            "started",
            "cancelled",
        ]
        assert [payload["status"] for _, _, payload in emitted] == [
            "started",
            "cancelled",
        ]
        assert manager.compact_calls == []

    @pytest.mark.asyncio
    async def test_context_compact_emits_skipped_when_nothing_removed(
        self,
        dispatcher,
        session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        manager = FakeSessionManager([session])
        manager.compact_summary = ""
        ctx = make_ctx(session_manager=manager)
        events: list[tuple[str, dict[str, Any]]] = []
        emitted = _capture_compaction_emits(monkeypatch)
        monkeypatch.setattr(
            rpc_sessions,
            "notify_compaction",
            lambda session_key, **payload: events.append((session_key, payload)),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is True
        assert res.payload["compacted"] is False
        assert res.payload["applied"] is False
        assert res.payload["durability"] == "none"
        assert res.payload["skip_reason"] == "empty_summary"
        assert res.payload["user_visible"] is True
        assert [payload["status"] for _, payload in events] == ["started", "skipped"]
        assert events[-1][1]["applied"] is False
        assert events[-1][1]["durability"] == "none"
        assert events[-1][1]["skip_reason"] == "empty_summary"
        assert [payload["status"] for _, _, payload in emitted] == [
            "started",
            "skipped",
        ]

    @pytest.mark.asyncio
    async def test_context_compact_emits_failed_when_compaction_raises(
        self,
        dispatcher,
        session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        manager = FakeSessionManager([session])

        async def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("compact boom")

        manager.compact_with_result = _boom  # type: ignore[method-assign]
        ctx = make_ctx(session_manager=manager)
        events: list[tuple[str, dict[str, Any]]] = []
        emitted = _capture_compaction_emits(monkeypatch)
        monkeypatch.setattr(
            rpc_sessions,
            "notify_compaction",
            lambda session_key, **payload: events.append((session_key, payload)),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is False
        assert [payload["status"] for _, payload in events] == ["started", "failed"]
        assert [payload["status"] for _, _, payload in emitted] == [
            "started",
            "failed",
        ]
        assert "compact boom" in events[-1][1]["message"]

    @pytest.mark.asyncio
    async def test_context_compact_passes_custom_instructions(
        self, dispatcher, ctx_with_sessions, session
    ):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.contextCompact",
            {
                "key": session.session_key,
                "contextWindowTokens": 1234,
                "instructions": "Preserve architecture decisions.",
            },
            ctx_with_sessions,
        )

        assert res.ok is True
        assert ctx_with_sessions.session_manager.compact_instructions == [
            "Preserve architecture decisions."
        ]

    @pytest.mark.asyncio
    async def test_context_compact_missing_flush_service_does_not_block_compaction(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(content="message to preserve")]
        ctx = make_ctx(
            session_manager=manager,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is True
        assert len(manager.compact_calls) == 1
        assert manager.compact_calls[0][:2] == (session.session_key, 100000)

    @pytest.mark.asyncio
    async def test_context_compact_degraded_flush_receipt_does_not_block_compaction(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(content="message to preserve")]
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="raw",
                    result_status="parse_failed_archived",
                    flushed_paths=["memory/.raw_fallbacks/raw.md"],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "raw",
                        "result_status": "parse_failed_archived",
                        "flushed_paths": ["memory/.raw_fallbacks/raw.md"],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is True
        assert len(manager.compact_calls) == 1
        assert manager.compact_calls[0][:2] == (session.session_key, 100000)
        assert manager.compact_kwargs[0]["flush_receipt_status"] == "degraded_forensic"
        assert res.payload["flush_receipt_status"] == "degraded_forensic"
        flush_correlation = flush_service.execute.await_args.kwargs[
            "provider_request_correlation"
        ]
        compact_correlation = manager.compact_kwargs[0][
            "provider_request_correlation"
        ]
        assert isinstance(flush_correlation, ProviderRequestCorrelation)
        assert isinstance(compact_correlation, ProviderRequestCorrelation)
        assert flush_correlation.session_id == compact_correlation.session_id
        assert flush_correlation.turn_id == compact_correlation.turn_id
        assert flush_correlation.execution_id != compact_correlation.execution_id
        assert flush_correlation.call_kind == "auxiliary.session_flush"
        assert compact_correlation.call_kind == "auxiliary.compaction"

    @pytest.mark.asyncio
    async def test_context_compact_block_mode_allows_checkpoint_receipt(self, dispatcher, session):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(id=1, content="message to preserve")]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(session, turn_id="cmp-compact", entries=manager.transcript)
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="raw",
                    result_status="parse_failed_archived",
                    flushed_paths=["memory/.raw_fallbacks/raw.md"],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "raw",
                        "result_status": "parse_failed_archived",
                        "flushed_paths": ["memory/.raw_fallbacks/raw.md"],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(
                memory={
                    "flush_enabled": True,
                    "flush_triggers": ["manual"],
                    "flush_compaction_safety_mode": "block",
                }
            ),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is True
        assert res.payload["flush_receipt"]["result_status"] == "parse_failed_archived"
        assert res.payload["flush_receipt_status"] == "unsafe"
        assert manager.compact_calls[0][:2] == (session.session_key, 100000)

    @pytest.mark.asyncio
    async def test_context_compact_block_mode_refuses_stale_checkpoint_receipt(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [
            SimpleNamespace(id=1, content="checkpointed"),
            SimpleNamespace(id=2, content="not checkpointed"),
        ]
        manager._storage.memory_durable_receipts.append(
            _checkpoint_receipt(
                session,
                turn_id="cmp-compact-old",
                entries=manager.transcript[:1],
            )
        )
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="error",
                    result_status="archive_failed",
                    flushed_paths=[],
                    content_hash="h1",
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverified",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverified",
                    obligation_missing_ids=[],
                    to_dict=lambda: {
                        "mode": "error",
                        "result_status": "archive_failed",
                        "flushed_paths": [],
                        "content_hash": "h1",
                    },
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(
                memory={
                    "flush_enabled": True,
                    "flush_triggers": ["manual"],
                    "flush_compaction_safety_mode": "block",
                }
            ),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is False
        assert res.error.code == "CONTEXT_FLUSH_FAILED"
        assert res.error.details["memory_safety_status"] == "unsafe"
        assert res.error.details["semantic_memory_status"] == "failed"
        assert manager.compact_calls == []

    @pytest.mark.asyncio
    async def test_context_compact_block_mode_refuses_without_checkpoint_receipt(
        self, dispatcher, session
    ):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(content="message to preserve")]
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="raw",
                    integrity_ok=True,
                    output_coverage_status="ok",
                    missing_candidate_count=0,
                    invalid_candidate_count=0,
                    obligation_status="ok",
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(
                memory={
                    "flush_enabled": True,
                    "flush_triggers": ["manual"],
                    "flush_compaction_safety_mode": "block",
                }
            ),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is False
        assert res.error.code == "CONTEXT_FLUSH_FAILED"
        assert manager.compact_calls == []

    @pytest.mark.asyncio
    async def test_context_compact_persists_noop_flush_receipt_status(self, dispatcher, session):
        manager = FakeSessionManager([session])
        manager.transcript = [SimpleNamespace(content="message to preserve")]
        flush_service = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    mode="llm",
                    result_status="ok_noop_no_memory",
                    flushed_paths=[],
                    raw_reason=None,
                    error=None,
                    indexed_chunk_count=0,
                    integrity_status="unverified",
                    output_coverage_status="unverifiable",
                    invalid_candidate_count=0,
                    candidate_missing_ids=[],
                    obligation_status="unverifiable",
                    obligation_missing_ids=[],
                )
            )
        )
        ctx = make_ctx(
            session_manager=manager,
            flush_service=flush_service,
            config=GatewayConfig(memory={"flush_enabled": True}),
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is True
        assert len(manager.compact_calls) == 1
        assert manager.compact_kwargs[0]["flush_receipt_status"] == "noop_no_memory"
        assert res.payload["flush_receipt_status"] == "noop_no_memory"

    @pytest.mark.asyncio
    async def test_context_compact_allowed_for_operator_write_scope(self, dispatcher, session):
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            scopes=["operator.read", "operator.write"],
        )

        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": session.session_key}, ctx
        )

        assert res.ok is True
        assert ctx.session_manager.compact_calls[0][:2] == (
            session.session_key,
            ctx.config.context_budget_tokens,
        )

    @pytest.mark.asyncio
    async def test_context_compact_passes_provider_config_without_flush_receipt(self, dispatcher):
        session = FakeSession(session_key="agent:main:abc123", model="session/model")
        manager = FakeSessionManager([session])
        selector = _FakeProviderSelector()
        flush_service = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no flush")))
        ctx = make_ctx(
            session_manager=manager,
            provider_selector=selector,
            flush_service=flush_service,
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": session.session_key, "contextWindowTokens": 1234},
            ctx,
        )

        assert res.ok is True
        assert "flush_receipt" not in res.payload
        assert res.payload["summary_source"] == "fallback"
        flush_service.execute.assert_not_called()
        config = manager.compact_calls[0][2]
        assert isinstance(config, CompactionConfig)
        assert config.api_key == "provider-key"
        assert config.model == "session/model"
        assert config.base_url == "https://openrouter.ai/api/v1"

    @pytest.mark.asyncio
    async def test_context_compact_uses_model_override_on_clone_only(self, dispatcher):
        session = FakeSession(
            session_key="agent:main:abc123",
            model="session/model",
            model_override="routed/model",
        )
        manager = FakeSessionManager([session])
        selector = _FakeProviderSelector()
        ctx = make_ctx(session_manager=manager, provider_selector=selector)

        res = await dispatcher.dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": session.session_key, "contextWindowTokens": 1234},
            ctx,
        )

        assert res.ok is True
        config = manager.compact_calls[0][2]
        assert isinstance(config, CompactionConfig)
        assert config.model == "routed/model"
        assert selector.override_calls == []
        assert selector.clone_instance.override_calls == ["routed/model"]

    @pytest.mark.asyncio
    async def test_context_compact_legacy_manager_reports_unknown_source(self, dispatcher):
        session = FakeSession(session_key="agent:main:abc123")
        manager = _LegacyCompactManager(session)
        ctx = make_ctx(session_manager=manager, provider_selector=_FakeProviderSelector())

        res = await dispatcher.dispatch(
            "r1",
            "sessions.contextCompact",
            {"key": session.session_key, "contextWindowTokens": 1234},
            ctx,
        )

        assert res.ok is True
        assert res.payload["summary_source"] == "unknown"
        assert manager.compact_calls == [(session.session_key, 1234)]

    @pytest.mark.asyncio
    async def test_context_compact_missing_ephemeral_webchat_session_skips(
        self,
        dispatcher,
        ctx_with_sessions,
        monkeypatch: pytest.MonkeyPatch,
    ):
        events: list[tuple[str, dict[str, Any]]] = []
        emitted = _capture_compaction_emits(monkeypatch)
        monkeypatch.setattr(
            rpc_sessions,
            "notify_compaction",
            lambda session_key, **payload: events.append((session_key, payload)),
        )

        key = "agent:main:webchat:58x01oc0"
        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": key}, ctx_with_sessions
        )

        assert res.ok is True
        assert res.payload["key"] == key
        assert res.payload["compacted"] is False
        assert res.payload["status"] == "skipped"
        assert res.payload["reason"] == "empty_ephemeral_webchat_session"
        assert ctx_with_sessions.session_manager.compact_calls == []
        assert [(event_key, payload["status"]) for event_key, payload in events] == [
            (key, "started"),
            (key, "skipped"),
        ]
        assert [(event_key, payload["status"]) for event_key, _, payload in emitted] == [
            (key, "started"),
            (key, "skipped"),
        ]

    @pytest.mark.asyncio
    async def test_context_compact_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.contextCompact", {"key": "nonexistent"}, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"


class TestSessionsSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch("r1", "sessions.subscribe", None, ctx_with_sessions)
        assert res.ok is True

    @pytest.mark.asyncio
    async def test_unsubscribe(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch("r1", "sessions.unsubscribe", None, ctx_with_sessions)
        assert res.ok is True


class TestSessionsMessagesSubscribe:
    @pytest.mark.asyncio
    async def test_messages_subscribe(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.messages.subscribe",
            {"key": session.session_key},
            ctx_with_sessions,
        )
        assert res.ok is True
        assert res.payload["subscribed"] is False
        assert res.payload["key"] == session.session_key
        assert isinstance(res.payload["current_stream_seq"], int)
        assert res.payload["replay_complete"] is True
        assert res.payload["replayed_count"] == 0

    @pytest.mark.asyncio
    async def test_messages_subscribe_reports_authoritative_active_task_groups(
        self, dispatcher, ctx_with_sessions, session, monkeypatch
    ):
        async def _active_group_ids(key: str) -> list[str]:
            assert key == session.session_key
            return ["group-live"]

        monkeypatch.setattr(
            "opensquilla.gateway.subagent_announce.active_background_completion_group_ids",
            _active_group_ids,
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.messages.subscribe",
            {"key": session.session_key},
            ctx_with_sessions,
        )

        assert res.ok is True
        assert res.payload["active_task_group_ids"] == ["group-live"]

    @pytest.mark.asyncio
    async def test_messages_subscribe_replays_buffered_events_after_cursor(self, dispatcher):
        key = "agent:main:replay-test"
        stream_registry = get_session_streams()
        first = stream_registry.record(key, "session.event.text_delta", {"text": "old"})
        second = stream_registry.record(key, "session.event.done", {"reason": "stop"})

        conn_id = "replay-test-conn"
        conn = _ReplayConn(conn_id)
        registry = get_registry()
        registry.register(conn)
        try:
            ctx = make_ctx(
                session_manager=FakeSessionManager([FakeSession(session_key=key)]),
                conn_id=conn_id,
                subscription_manager=SubscriptionManager(),
            )

            res = await dispatcher.dispatch(
                "r1",
                "sessions.messages.subscribe",
                {"key": key, "since_stream_seq": first["stream_seq"]},
                ctx,
            )
        finally:
            registry.unregister(conn_id)

        assert res.ok is True
        assert res.payload["subscribed"] is True
        assert res.payload["current_stream_seq"] == second["stream_seq"]
        assert res.payload["replay_complete"] is True
        assert res.payload["replayed_count"] == 1
        assert conn.events == [("session.event.done", second, {"replayed": True})]

    @pytest.mark.asyncio
    async def test_messages_subscribe_replays_task_group_events(self, dispatcher):
        key = "agent:main:task-group-replay-test"
        stream_registry = get_session_streams()
        waiting = stream_registry.record(
            key,
            "session.event.task_group.waiting",
            {"group_id": "group-1", "parent_task_id": "task-parent", "status": "waiting"},
        )
        done = stream_registry.record(
            key,
            "session.event.task_group.done",
            {
                "group_id": "group-1",
                "parent_task_id": "task-parent",
                "status": "done",
                "delivery_status": "sent",
            },
        )

        conn_id = "task-group-replay-test-conn"
        conn = _ReplayConn(conn_id)
        registry = get_registry()
        registry.register(conn)
        try:
            ctx = make_ctx(
                session_manager=FakeSessionManager([FakeSession(session_key=key)]),
                conn_id=conn_id,
                subscription_manager=SubscriptionManager(),
            )

            res = await dispatcher.dispatch(
                "r1",
                "sessions.messages.subscribe",
                {"key": key, "since_stream_seq": waiting["stream_seq"]},
                ctx,
            )
        finally:
            registry.unregister(conn_id)

        assert res.ok is True
        assert res.payload["replayed_count"] == 1
        assert conn.events == [("session.event.task_group.done", done, {"replayed": True})]

    @pytest.mark.asyncio
    async def test_messages_subscribe_reports_persisted_task_state_and_replay_gap(self, dispatcher):
        key = "agent:main:webchat:restarted"
        session = FakeSession(session_key=key)
        manager = FakeSessionManager([session])
        manager._storage._agent_tasks[key] = [
            SimpleNamespace(
                task_id="task-abandoned",
                status="abandoned",
                queue_mode="followup",
                run_kind="web_turn",
                source_kind="webui",
                created_at=100,
                started_at=110,
                finished_at=120,
                terminal_reason="process_restart",
            )
        ]
        ctx = make_ctx(session_manager=manager, subscription_manager=SubscriptionManager())

        res = await dispatcher.dispatch(
            "r1",
            "sessions.messages.subscribe",
            {"key": key, "since_stream_seq": 7},
            ctx,
        )

        assert res.ok is True
        assert res.payload["replay_complete"] is False
        assert res.payload["replay_gap_reason"] == "stream_buffer_reset"
        assert res.payload["last_task"]["task_id"] == "task-abandoned"
        assert res.payload["run_status"] == "interrupted"

    @pytest.mark.asyncio
    async def test_messages_subscribe_missing_key(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.messages.subscribe", None, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_messages_unsubscribe(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.messages.unsubscribe",
            {"key": session.session_key},
            ctx_with_sessions,
        )
        assert res.ok is True


class _SearchStorage(FakeStorage):
    """FakeStorage plus the FTS hook that sessions.search wraps."""

    def __init__(self, sessions=None, transcript_rows=None):
        super().__init__(sessions)
        self._search_rows = transcript_rows or []
        self.search_calls: list[tuple[str, str | None, int]] = []

    async def search_transcript(self, query, session_id=None, limit=20):
        self.search_calls.append((query, session_id, limit))
        return list(self._search_rows)[:limit]


class _SearchManager(FakeSessionManager):
    def __init__(self, sessions=None, transcript_rows=None):
        super().__init__(sessions)
        self._storage = _SearchStorage(sessions, transcript_rows)


class TestSessionsSearchRpc:
    @staticmethod
    def _sessions():
        return [
            FakeSession(
                session_key="agent:main:s1",
                session_id="s1",
                display_name="Deploy planning",
                updated_at=2000,
            ),
            FakeSession(
                session_key="agent:main:s2",
                session_id="s2",
                display_name="Grocery list",
                updated_at=3000,
            ),
        ]

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, dispatcher):
        ctx = make_ctx(session_manager=_SearchManager(self._sessions()))
        res = await dispatcher.dispatch("r1", "sessions.search", {"query": "   "}, ctx)
        assert res.ok is True
        assert res.payload["sessions"] == []
        assert res.payload["messages"] == []

    @pytest.mark.asyncio
    async def test_no_manager_returns_empty(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch(
            "r1", "sessions.search", {"query": "deploy"}, ctx_no_manager
        )
        assert res.ok is True
        assert res.payload["sessions"] == []
        assert res.payload["messages"] == []

    @pytest.mark.asyncio
    async def test_title_hit_matches_one_session(self, dispatcher):
        ctx = make_ctx(session_manager=_SearchManager(self._sessions()))
        res = await dispatcher.dispatch("r1", "sessions.search", {"query": "deploy"}, ctx)
        assert res.ok is True
        keys = [row["key"] for row in res.payload["sessions"]]
        assert keys == ["agent:main:s1"]
        assert res.payload["sessions"][0]["title"] == "Deploy planning"
        # No transcript rows configured -> no content hits.
        assert res.payload["messages"] == []

    @pytest.mark.asyncio
    async def test_content_hit_is_enriched_with_session_title(self, dispatcher):
        rows = [
            {
                "id": 10,
                "session_key": "agent:main:s2",
                "role": "user",
                "snippet": "buy >>>milk<<< today",
                "created_at": 1234,
            }
        ]
        manager = _SearchManager(self._sessions(), transcript_rows=rows)
        ctx = make_ctx(session_manager=manager)
        res = await dispatcher.dispatch("r1", "sessions.search", {"query": "milk", "limit": 5}, ctx)
        assert res.ok is True
        messages = res.payload["messages"]
        assert len(messages) == 1
        hit = messages[0]
        assert hit["key"] == "agent:main:s2"
        assert hit["title"] == "Grocery list"  # joined from the session metadata
        assert hit["snippet"] == "buy >>>milk<<< today"
        assert hit["role"] == "user"
        # The FTS hook received the raw query and the clamped limit.
        assert manager._storage.search_calls == [("milk", None, 5)]

    @pytest.mark.asyncio
    async def test_read_scope_is_sufficient(self, dispatcher):
        ctx = make_ctx(
            scopes=["operator.read"],
            session_manager=_SearchManager(self._sessions()),
        )
        res = await dispatcher.dispatch("r1", "sessions.search", {"query": "deploy"}, ctx)
        assert res.ok is True

    @pytest.mark.asyncio
    async def test_real_storage_fts_end_to_end(self, dispatcher):
        """Drive the handler against a real SQLite FTS store (not the fake).

        Exercises the real list_sessions + transcript FTS + title derivation that
        the other tests stub, so a schema/SQL drift in search_transcript is caught
        here rather than only in a live gateway.
        """
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from opensquilla.session.models import SessionNode, TranscriptEntry
        from opensquilla.session.storage import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStorage(str(Path(tmpdir) / "s.db"))
            await store.connect()
            try:

                async def seed(sid: str, name: str, text: str) -> None:
                    await store.upsert_session(
                        SessionNode(
                            session_key=f"agent:main:{sid}",
                            session_id=sid,
                            agent_id="main",
                            status="idle",
                            created_at=1,
                            updated_at=1,
                            display_name=name,
                        )
                    )
                    await store.append_transcript_entry(
                        TranscriptEntry(
                            session_id=sid,
                            session_key=f"agent:main:{sid}",
                            message_id=f"{sid}-m0",
                            role="user",
                            content=text,
                            created_at=1,
                        )
                    )

                await seed("d1", "Deploy planning", "we should deploy the gateway")
                await seed("g1", "Grocery list", "remember to buy milk today")

                ctx = make_ctx(session_manager=SimpleNamespace(_storage=store))

                # Content hit via the real FTS index.
                res = await dispatcher.dispatch("r1", "sessions.search", {"query": "milk"}, ctx)
                assert res.ok is True
                messages = res.payload["messages"]
                assert [m["key"] for m in messages] == ["agent:main:g1"]
                assert "milk" in messages[0]["snippet"].lower()
                assert messages[0]["title"] == "Grocery list"

                # Title hit (display_name) for a different term.
                res2 = await dispatcher.dispatch("r1", "sessions.search", {"query": "deploy"}, ctx)
                assert res2.ok is True
                assert "agent:main:d1" in [s["key"] for s in res2.payload["sessions"]]
            finally:
                await store.close()

    @pytest.mark.asyncio
    async def test_cjk_content_search_real_storage(self, dispatcher):
        """Chinese (non-ASCII) message content is searchable via the LIKE path,
        which the FTS sanitizer would otherwise strip to nothing."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from opensquilla.session.models import SessionNode, TranscriptEntry
        from opensquilla.session.storage import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStorage(str(Path(tmpdir) / "s.db"))
            await store.connect()
            try:
                await store.upsert_session(
                    SessionNode(
                        session_key="agent:main:c1",
                        session_id="c1",
                        agent_id="main",
                        status="idle",
                        created_at=1,
                        updated_at=1,
                        display_name="部署讨论",
                    )
                )
                await store.append_transcript_entry(
                    TranscriptEntry(
                        session_id="c1",
                        session_key="agent:main:c1",
                        message_id="c1-m0",
                        role="user",
                        content="我们需要尽快完成部署计划并通知团队",
                        created_at=1,
                    )
                )
                # Baseline: the FTS index alone cannot find the Chinese term.
                assert await store.search_transcript("部署") == []

                ctx = make_ctx(session_manager=SimpleNamespace(_storage=store))
                # A content-only Chinese phrase (absent from any title) must come
                # back as a message hit with a highlighted snippet.
                res = await dispatcher.dispatch("r1", "sessions.search", {"query": "通知团队"}, ctx)
                assert res.ok is True
                assert [m["key"] for m in res.payload["messages"]] == ["agent:main:c1"]
                snippet = res.payload["messages"][0]["snippet"]
                assert ">>>" in snippet and "通知团队" in snippet
            finally:
                await store.close()

    @pytest.mark.asyncio
    async def test_title_search_scans_beyond_200_sessions(self, dispatcher):
        """Title search is global -- an old conversation past any recent window
        is still findable by name (no silent 200-session cap)."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from opensquilla.session.models import SessionNode
        from opensquilla.session.storage import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStorage(str(Path(tmpdir) / "s.db"))
            await store.connect()
            try:
                # Target is the OLDEST row; 220 newer noise rows bury it well past
                # any recent-200 page.
                await store.upsert_session(
                    SessionNode(
                        session_key="agent:main:old",
                        session_id="old",
                        agent_id="main",
                        status="idle",
                        created_at=1,
                        updated_at=1,
                        display_name="Zephyr migration notes",
                    )
                )
                for i in range(220):
                    await store.upsert_session(
                        SessionNode(
                            session_key=f"agent:main:n{i}",
                            session_id=f"n{i}",
                            agent_id="main",
                            status="idle",
                            created_at=1000 + i,
                            updated_at=1000 + i,
                            display_name=f"noise {i}",
                        )
                    )
                ctx = make_ctx(session_manager=SimpleNamespace(_storage=store))
                res = await dispatcher.dispatch("r1", "sessions.search", {"query": "zephyr"}, ctx)
                assert res.ok is True
                assert [s["key"] for s in res.payload["sessions"]] == ["agent:main:old"]
            finally:
                await store.close()

    @pytest.mark.asyncio
    async def test_message_hits_deduped_and_exclude_title_hits(self, dispatcher):
        """Many matches in one session collapse to a single message row, and a
        session already shown as a title hit is not repeated under messages."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from opensquilla.session.models import SessionNode, TranscriptEntry
        from opensquilla.session.storage import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStorage(str(Path(tmpdir) / "s.db"))
            await store.connect()
            try:
                # Session A: three messages all matching -> one message row.
                await store.upsert_session(
                    SessionNode(
                        session_key="agent:main:a",
                        session_id="a",
                        agent_id="main",
                        status="idle",
                        created_at=1,
                        updated_at=2,
                        display_name="Daily standup",
                    )
                )
                for i in range(3):
                    await store.append_transcript_entry(
                        TranscriptEntry(
                            session_id="a",
                            session_key="agent:main:a",
                            message_id=f"a-m{i}",
                            role="user",
                            content=f"the report number {i}",
                            created_at=10 + i,
                        )
                    )
                # Session B: title AND content match -> appears as a title hit
                # only, never duplicated under messages.
                await store.upsert_session(
                    SessionNode(
                        session_key="agent:main:b",
                        session_id="b",
                        agent_id="main",
                        status="idle",
                        created_at=1,
                        updated_at=3,
                        display_name="Quarterly report",
                    )
                )
                await store.append_transcript_entry(
                    TranscriptEntry(
                        session_id="b",
                        session_key="agent:main:b",
                        message_id="b-m0",
                        role="user",
                        content="the report is attached",
                        created_at=20,
                    )
                )
                ctx = make_ctx(session_manager=SimpleNamespace(_storage=store))
                res = await dispatcher.dispatch("r1", "sessions.search", {"query": "report"}, ctx)
                assert res.ok is True
                msg_keys = [m["key"] for m in res.payload["messages"]]
                sess_keys = [s["key"] for s in res.payload["sessions"]]
                assert "agent:main:b" in sess_keys
                assert msg_keys.count("agent:main:a") == 1
                assert "agent:main:b" not in msg_keys
            finally:
                await store.close()

    @pytest.mark.asyncio
    async def test_mixed_ascii_cjk_query_ands_terms(self, dispatcher):
        """A mixed query ("deploy 部署") must match a transcript containing both
        terms even when they are not adjacent, and must NOT match when a term is
        absent -- i.e. terms are AND-ed, not matched as one contiguous substring."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from opensquilla.session.models import SessionNode, TranscriptEntry
        from opensquilla.session.storage import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStorage(str(Path(tmpdir) / "s.db"))
            await store.connect()
            try:
                await store.upsert_session(
                    SessionNode(
                        session_key="agent:main:m1",
                        session_id="m1",
                        agent_id="main",
                        status="idle",
                        created_at=1,
                        updated_at=1,
                        display_name="Ops chat",
                    )
                )
                await store.append_transcript_entry(
                    TranscriptEntry(
                        session_id="m1",
                        session_key="agent:main:m1",
                        message_id="m1-m0",
                        role="user",
                        # "deploy" and "部署" present but NOT adjacent.
                        content="please deploy the service, the 部署 will finish soon",
                        created_at=1,
                    )
                )
                ctx = make_ctx(session_manager=SimpleNamespace(_storage=store))

                hit = await dispatcher.dispatch(
                    "r1", "sessions.search", {"query": "deploy 部署"}, ctx
                )
                assert hit.ok is True
                assert [m["key"] for m in hit.payload["messages"]] == ["agent:main:m1"]

                # A term that is absent ("缓存") must exclude the row.
                miss = await dispatcher.dispatch(
                    "r1", "sessions.search", {"query": "deploy 缓存"}, ctx
                )
                assert miss.ok is True
                assert miss.payload["messages"] == []
            finally:
                await store.close()

    @pytest.mark.asyncio
    async def test_non_ascii_title_search_is_case_insensitive(self, dispatcher):
        """Cased non-Latin scripts (e.g. Cyrillic) fold case in title search --
        a lowercase query finds an upper-cased title."""
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from opensquilla.session.models import SessionNode
        from opensquilla.session.storage import SessionStorage

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStorage(str(Path(tmpdir) / "s.db"))
            await store.connect()
            try:
                await store.upsert_session(
                    SessionNode(
                        session_key="agent:main:ru",
                        session_id="ru",
                        agent_id="main",
                        status="idle",
                        created_at=1,
                        updated_at=1,
                        display_name="ПРИВЕТ Команда",
                    )
                )
                ctx = make_ctx(session_manager=SimpleNamespace(_storage=store))
                res = await dispatcher.dispatch("r1", "sessions.search", {"query": "привет"}, ctx)
                assert res.ok is True
                assert [s["key"] for s in res.payload["sessions"]] == ["agent:main:ru"]
            finally:
                await store.close()


class TestSessionsPreview:
    @pytest.mark.asyncio
    async def test_preview_all(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch("r1", "sessions.preview", None, ctx_with_sessions)
        assert res.ok is True
        assert "ts" in res.payload
        assert "previews" in res.payload
        assert len(res.payload["previews"]) == 1

    @pytest.mark.asyncio
    async def test_preview_by_keys(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.preview",
            {"keys": [session.session_key]},
            ctx_with_sessions,
        )
        assert res.ok is True
        assert len(res.payload["previews"]) == 1

    @pytest.mark.asyncio
    async def test_preview_no_manager(self, dispatcher, ctx_no_manager):
        res = await dispatcher.dispatch("r1", "sessions.preview", None, ctx_no_manager)
        assert res.ok is True
        assert res.payload["previews"] == []


class TestSessionsResolve:
    @pytest.mark.asyncio
    async def test_resolve_valid(self, dispatcher, ctx_with_sessions, session):
        res = await dispatcher.dispatch(
            "r1",
            "sessions.resolve",
            {"key": session.session_key},
            ctx_with_sessions,
        )
        assert res.ok is True
        assert res.payload["session_key"] == session.session_key

    @pytest.mark.asyncio
    async def test_resolve_by_session_id(self, dispatcher):
        session = FakeSession(session_key="agent:default:abc123", session_id="abc123")
        ctx = make_ctx(session_manager=FakeSessionManager([session]))

        res = await dispatcher.dispatch(
            "r1",
            "sessions.resolve",
            {"key": "abc123"},
            ctx,
        )

        assert res.ok is True
        assert res.payload["session_key"] == "agent:default:abc123"

    @pytest.mark.asyncio
    async def test_resolve_by_unique_short_prefix(self, dispatcher):
        session = FakeSession(session_key="agent:default:abc123", session_id="abc123")
        other = FakeSession(session_key="agent:default:def456", session_id="def456")
        ctx = make_ctx(session_manager=FakeSessionManager([session, other]))

        res = await dispatcher.dispatch(
            "r1",
            "sessions.resolve",
            {"key": "abc"},
            ctx,
        )

        assert res.ok is True
        assert res.payload["session_key"] == "agent:default:abc123"

    @pytest.mark.asyncio
    async def test_resolve_rejects_ambiguous_prefix(self, dispatcher):
        one = FakeSession(session_key="agent:default:abc123", session_id="abc123")
        two = FakeSession(session_key="agent:bench:abc999", session_id="abc999")
        ctx = make_ctx(session_manager=FakeSessionManager([one, two]))

        res = await dispatcher.dispatch(
            "r1",
            "sessions.resolve",
            {"key": "abc"},
            ctx,
        )

        assert res.ok is False
        assert res.error.code == "INVALID_REQUEST"
        assert "Ambiguous session id" in res.error.message

    @pytest.mark.asyncio
    async def test_resolve_not_found(self, dispatcher, ctx_with_sessions):
        res = await dispatcher.dispatch(
            "r1", "sessions.resolve", {"key": "nonexistent"}, ctx_with_sessions
        )
        assert res.ok is False
        assert res.error.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_scope_enforcement(self, dispatcher, session):
        """sessions.create requires operator.write."""
        ctx = make_ctx(
            scopes=["operator.read"],
            session_manager=FakeSessionManager([session]),
        )
        res = await dispatcher.dispatch("r1", "sessions.create", {"agentId": "test"}, ctx)
        assert res.ok is False
        assert res.error.code == "UNAUTHORIZED"


class TestSessionsBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_composes_history_tasks_epoch_and_stream_cursor(
        self, dispatcher, tmp_path
    ):
        key = "agent:main:webchat:bootstrap-contract"
        session = FakeSession(
            session_key=key,
            session_id="bootstrap-contract",
            status="running",
            model="provider/model",
            epoch=3,
        )
        manager = FakeSessionManager([session])
        manager.transcript = [
            TranscriptEntry(
                id=1,
                session_id=session.session_id,
                session_key=key,
                role="user",
                content="hello",
                created_at=100,
                message_id="msg-1",
            )
        ]
        manager._storage._agent_tasks[key] = [
            SimpleNamespace(
                task_id="task-1",
                status="queued",
                queue_mode="followup",
                run_kind="session_turn",
                source_kind="cli",
                created_at=110,
                started_at=None,
                details={
                    "turn_id": "task-1",
                    "client_message_id": "msg-1",
                    "user_message_id": "durable-msg-1",
                    "surface_id": "cli:chat",
                    "session_id": session.session_id,
                },
            )
        ]
        stream = get_session_streams().record(
            key,
            "session.event.text_delta",
            {"text": "partial"},
        )
        workspace = tmp_path / "workspace"
        ctx = make_ctx(
            session_manager=manager,
            task_runtime=None,
            config=GatewayConfig(workspace_dir=str(workspace)),
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.bootstrap",
            {"key": key, "limit": 25},
            ctx,
        )

        assert res.ok is True
        assert res.payload["session"]["session_key"] == key
        assert res.payload["session"]["session_id"] == session.session_id
        assert res.payload["session"]["model"] == "provider/model"
        assert res.payload["session"]["effective_model"] == "provider/model"
        assert res.payload["session"]["workspace"] == str(workspace)
        assert res.payload["agent_identity"] == {
            "agent_id": "main",
            "name": "main",
            "emoji": None,
            "theme": None,
        }
        assert res.payload["history"]["messages"][0]["message_id"] == "msg-1"
        assert res.payload["history"]["history_scope"] == "complete"
        assert res.payload["active_task"]["turn_id"] == "task-1"
        assert res.payload["active_task"]["user_message_id"] == "durable-msg-1"
        assert res.payload["queue"] == {
            "mode": "followup",
            "queued_count": 1,
            "running_count": 0,
        }
        assert res.payload["runtime"]["model_routing"]["mode"] == "router"
        assert res.payload["epoch"] == 3
        assert res.payload["stream_cursor"] == stream["stream_seq"]

    @pytest.mark.asyncio
    async def test_bootstrap_includes_only_sanitized_agent_identity_display_fields(
        self, dispatcher, session, tmp_path
    ):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "IDENTITY.md").write_text(
            "\n".join(
                [
                    "Name:  Mira\x1b[31m\tOperator  ",
                    "Emoji:  🦐  ",
                    "Theme:  ember\t dark  ",
                    "Avatar: /private/agent.png",
                    "Soul: must not cross bootstrap",
                ]
            ),
            encoding="utf-8",
        )
        config = GatewayConfig(workspace_dir=str(workspace))
        registry = AgentRegistry(config, persist_changes=False)
        ctx = make_ctx(
            session_manager=FakeSessionManager([session]),
            config=config,
            agent_registry=registry,
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.bootstrap",
            {"key": session.session_key},
            ctx,
        )

        assert res.ok is True
        assert res.payload["agent_identity"] == {
            "agent_id": "main",
            "name": "Mira Operator",
            "emoji": "🦐",
            "theme": "ember dark",
        }
        assert "avatar" not in res.payload["agent_identity"]
        assert "soul" not in res.payload["agent_identity"]

    @pytest.mark.asyncio
    async def test_bootstrap_is_read_scoped(self, dispatcher, session):
        ctx = make_ctx(
            scopes=[],
            session_manager=FakeSessionManager([session]),
        )

        res = await dispatcher.dispatch(
            "r1",
            "sessions.bootstrap",
            {"key": session.session_key},
            ctx,
        )

        assert res.ok is False
        assert res.error.code == "UNAUTHORIZED"


def test_session_view_plugin_channel_type_degrades_to_unknown_surface():
    # docs/session-view-contract.md pins `surface` as a closed union: a
    # configured name mapping to an out-of-enum plugin type (entry-point
    # adapters) must degrade to "unknown", never widen the contract.
    from opensquilla.gateway.session_view import build_session_view_item

    session = FakeSession(
        session_key="agent:main:whats-bot:direct:u-1",
        last_channel="whats-bot",
        last_to="u-1",
    )
    view = build_session_view_item(
        session,
        entry_count=0,
        task_rows=[],
        now_ms=0,
        channel_types={"whats-bot": "whatsapp"},
    )
    assert view["surface"] == "unknown"

    # A configured builtin type keeps resolving through the same map.
    feishu_session = FakeSession(
        session_key="agent:main:飞书:direct:u-1",
        last_channel="飞书",
        last_to="u-1",
    )
    feishu_view = build_session_view_item(
        feishu_session,
        entry_count=0,
        task_rows=[],
        now_ms=0,
        channel_types={"飞书": "feishu"},
    )
    assert feishu_view["surface"] == "feishu"
    assert feishu_view["sessionKind"] == "channel"


@pytest.mark.asyncio
async def test_search_classifies_custom_named_channel_sessions(dispatcher):
    # sessions.search must thread the configured name->type map exactly like
    # sessions.list: a custom-named channel session's title hit carries the
    # platform surface, not "unknown".
    session = FakeSession(
        session_key="agent:main:飞书:direct:ou_demo_user",
        session_id="s-feishu",
        display_name="Deploy planning",
        last_channel="飞书",
        last_to="ou_demo_user",
        updated_at=2000,
    )
    config = GatewayConfig(
        memory={"flush_enabled": False},
        channels={
            "channels": [
                {
                    "type": "feishu",
                    "name": "飞书",
                    "app_id": "cli_dummy",
                    "app_secret": "dummy",
                }
            ]
        },
    )
    ctx = make_ctx(session_manager=_SearchManager([session]), config=config)

    res = await dispatcher.dispatch("r1", "sessions.search", {"query": "deploy"}, ctx)

    assert res.ok is True
    hits = res.payload["sessions"]
    assert [row["key"] for row in hits] == ["agent:main:飞书:direct:ou_demo_user"]
    assert hits[0]["surface"] == "feishu"
