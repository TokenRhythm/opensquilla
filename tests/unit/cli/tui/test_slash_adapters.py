"""Behavioral coverage for the TUI slash adapters and shared helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.chat.turn import TurnResult
from opensquilla.cli.tui.adapters import slash_bridge as _slash_bridge
from opensquilla.cli.tui.adapters import slash_gateway as _slash_gateway
from opensquilla.cli.tui.adapters import slash_standalone as _slash_standalone
from opensquilla.cli.tui.adapters.commands import is_exit_command
from opensquilla.cli.tui.adapters.slash_common import (
    record_turn,
    registry_handler_words,
    resolve_transcript_target,
    transcript_messages_to_markdown,
)
from opensquilla.cli.tui.adapters.slash_gateway import (
    GATEWAY_SLASH_HANDLER_WORDS,
    GatewaySlashContext,
    handle_gateway_slash_command,
)
from opensquilla.cli.tui.adapters.slash_policy import SlashCategory, classify
from opensquilla.cli.tui.adapters.slash_standalone import (
    STANDALONE_SLASH_HANDLER_WORDS,
    StandaloneSlashContext,
    StandaloneSlashServices,
    handle_standalone_slash_command,
)
from opensquilla.engine.commands import Surface

# Exit words are intercepted by the runtime loops before slash dispatch, so
# neither handler chain owns them.
_RUNTIME_OWNED_WORDS = frozenset({"/exit", "/quit"})


class _RecordingConsole:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.entries.append(args[0] if args else "")

    def text(self) -> str:
        return "\n".join(str(entry) for entry in self.entries)


def _fake_error_panel(message: str, *, title: str = "Error") -> str:
    return f"[panel:{title}] {message}"


def _patch_gateway_io(monkeypatch: pytest.MonkeyPatch) -> _RecordingConsole:
    recorder = _RecordingConsole()
    monkeypatch.setattr(_slash_gateway, "console", recorder)
    monkeypatch.setattr(_slash_gateway, "error_panel", _fake_error_panel)
    return recorder


def _patch_standalone_io(monkeypatch: pytest.MonkeyPatch) -> _RecordingConsole:
    recorder = _RecordingConsole()
    monkeypatch.setattr(_slash_standalone, "console", recorder)
    monkeypatch.setattr(_slash_standalone, "error_panel", _fake_error_panel)
    return recorder


class _StubGatewayClient:
    """Protocol-shaped double covering every method the adapter dispatches."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.resolve_payloads: dict[str, dict[str, Any]] = {}
        self.bootstrap_payloads: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.history_pages: dict[str | None, dict[str, Any]] = {}
        self.raise_map: dict[str, Exception] = {}
        self.session_rows: list[dict[str, Any]] = []
        self.model_rows: list[dict[str, Any]] = []
        self._counter = 0
        self.model_routing: dict[str, Any] = {
            "mode": "direct",
            "router_enabled": False,
            "ensemble_enabled": False,
            "rollout_phase": "observe",
            "selection_mode": "router_dynamic",
            "applies_to": "next_accepted_turn",
        }

    def _maybe_raise(self, method: str) -> None:
        exc = self.raise_map.get(method)
        if exc is not None:
            raise exc

    async def call(self, method: str, params: dict | None = None) -> Any:
        self.calls.append(("call", (method, params)))
        self._maybe_raise("call")
        if method == "meta.list":
            return {"skills": []}
        return {"ok": True}

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
    ) -> str:
        self._maybe_raise("create_session")
        self._counter += 1
        key = f"agent:main:test:{self._counter}"
        self.created.append({"key": key, "model": model, "display_name": display_name})
        return key

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self._maybe_raise("list_sessions")
        return {"sessions": list(self.session_rows[:limit])}

    async def resolve_session(self, key: str) -> dict[str, Any]:
        self._maybe_raise("resolve_session")
        payload = self.resolve_payloads.get(key)
        if payload is not None:
            return dict(payload)
        return {"session_key": key, "model": None}

    async def bootstrap_session(
        self,
        key: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        self._maybe_raise("bootstrap_session")
        self.calls.append(("bootstrap_session", (key, limit)))
        if key in self.bootstrap_payloads:
            return dict(self.bootstrap_payloads[key])
        resolved = dict(self.resolve_payloads.get(key) or {})
        created = next((item for item in self.created if item["key"] == key), None)
        return {
            "session": {
                "session_key": resolved.get("session_key") or resolved.get("key") or key,
                "model": resolved.get("model")
                if "model" in resolved
                else (created.get("model") if created is not None else None),
            },
            "history": {
                "messages": list(self.history),
                "history_scope": "complete",
                "loaded_count": len(self.history),
                "has_more": False,
                "canonical_available": True,
                "compaction_summaries": [],
            },
        }

    async def delete_sessions(self, keys: list[str]) -> dict[str, Any]:
        self._maybe_raise("delete_sessions")
        self.calls.append(("delete_sessions", tuple(keys)))
        return {"deleted": list(keys), "errors": []}

    async def reset_session(self, key: str) -> dict[str, Any]:
        self._maybe_raise("reset_session")
        return {"reset": True, "key": key}

    async def compact_session(self, key: str) -> dict[str, Any]:
        self._maybe_raise("compact_session")
        return {"compacted": False}

    async def list_models(
        self,
        provider: str | None = None,
        capabilities: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._maybe_raise("list_models")
        return list(self.model_rows)

    async def patch_session(self, key: str, **fields: Any) -> dict[str, Any]:
        self._maybe_raise("patch_session")
        self.calls.append(("patch_session", (key, fields)))
        return {"ok": True}

    async def usage_status(self) -> dict[str, Any]:
        self._maybe_raise("usage_status")
        return {"totalTokens": 0, "totalCostUsd": 0.0}

    async def upload_file(self, path: Path, mime: str, name: str) -> str:
        return "file-1"

    def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async def _events() -> AsyncIterator[dict[str, Any]]:
            yield {}

        return _events()

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any:
        return {"ok": True}

    async def abort_session(self, key: str) -> dict[str, Any]:
        return {"ok": True}

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]:
        self._maybe_raise("session_history")
        self.calls.append(
            (
                "session_history",
                {
                    "session_key": session_key,
                    "limit": limit,
                    "before": before,
                    "after": after,
                    "include_canonical": include_canonical,
                    "include_summaries": include_summaries,
                },
            )
        )
        if self.history_pages:
            return dict(self.history_pages[before])
        return {"messages": list(self.history), "has_more": False}

    async def forget_approvals(self, target: str | None = None) -> dict[str, Any]:
        self.calls.append(("forget_approvals", target))
        return {"ok": True}

    async def approvals_snapshot(self) -> dict[str, Any]:
        return {"mode": "prompt"}

    async def set_approval_mode(self, mode: str) -> dict[str, Any]:
        self.calls.append(("set_approval_mode", mode))
        return {"ok": True}

    async def get_model_routing(self) -> dict[str, Any]:
        self._maybe_raise("get_model_routing")
        self.calls.append(("get_model_routing", None))
        return dict(self.model_routing)

    async def set_model_routing(self, mode: str) -> dict[str, Any]:
        self._maybe_raise("set_model_routing")
        self.calls.append(("set_model_routing", mode))
        self.model_routing.update(
            mode=mode,
            router_enabled=mode == "router",
            ensemble_enabled=mode == "ensemble",
            rollout_phase="full" if mode != "direct" else "observe",
        )
        return dict(self.model_routing)


def _gateway_context(
    client: _StubGatewayClient | None = None,
    *,
    model: str | None = "openai/test",
    requested_model: str | None = None,
    tui_output: Any | None = None,
    stream_response: Any | None = None,
) -> GatewaySlashContext:
    return GatewaySlashContext(
        state=ChatSessionState(session_key="agent:main:test:0", model=model),
        client=client or _StubGatewayClient(),
        elevated_state={"mode": None},
        requested_model=requested_model,
        tui_output=tui_output,
        stream_response=stream_response,
    )


class _StructuredOutput:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.attachment_events: list[tuple[str, dict[str, Any]]] = []
        self._attachment_seq = 0

    async def send_message(self, message_type: str, payload: dict[str, Any]) -> None:
        self.messages.append((message_type, payload))

    @property
    def supports_send_message(self) -> bool:
        return True

    async def add_attachment(
        self,
        *,
        kind: str,
        label: str,
        status: str,
    ) -> str:
        self._attachment_seq += 1
        attachment_id = f"attachment-{self._attachment_seq}"
        self.attachment_events.append(
            (
                "add",
                {
                    "id": attachment_id,
                    "kind": kind,
                    "label": label,
                    "status": status,
                },
            )
        )
        return attachment_id

    async def update_attachment(
        self,
        attachment_id: str,
        *,
        status: str,
        message: str = "",
    ) -> bool:
        self.attachment_events.append(
            (
                "update",
                {"id": attachment_id, "status": status, "message": message},
            )
        )
        return True

    async def clear_attachments(self, *, status: str | None = None) -> int:
        self.attachment_events.append(("clear", {"status": status}))
        return 1


class _StandaloneHarness:
    def __init__(self) -> None:
        self.transcripts: dict[str, list[Any]] = {}
        self.read_errors: dict[str, Exception] = {}

    async def create_session(self, session_key: str, *, agent_id: str = "main") -> object:
        return SimpleNamespace(session_key=session_key, agent_id=agent_id)

    async def read_transcript(self, session_key: str) -> list[Any]:
        exc = self.read_errors.get(session_key)
        if exc is not None:
            raise exc
        return list(self.transcripts.get(session_key, []))

    async def truncate_session(self, session_key: str, *, max_messages: int = 0) -> None:
        self.transcripts[session_key] = []

    async def compact_session(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
    ) -> str:
        return "summary"

    async def flush_transcript(
        self,
        transcript: object,
        session_key: str,
        **kwargs: object,
    ) -> object:
        return SimpleNamespace(
            mode="llm",
            error=None,
            indexed_chunk_count=1,
            integrity_status="ok",
            output_coverage_status="ok",
            invalid_candidate_count=0,
            candidate_missing_ids=[],
            obligation_status="ok",
            obligation_missing_ids=[],
        )


def _standalone_context(
    harness: _StandaloneHarness | None = None,
    *,
    session_key: str = "agent:main:standalone:test",
    model: str | None = "openai/test",
) -> StandaloneSlashContext:
    harness = harness or _StandaloneHarness()
    state = ChatSessionState(session_key=session_key, model=model)
    return StandaloneSlashContext(
        state=state,
        session_key=session_key,
        model=model,
        tool_ctx=object(),
        slash_services=StandaloneSlashServices(
            create_session=harness.create_session,
            read_transcript=harness.read_transcript,
            truncate_session=harness.truncate_session,
            compact_session=harness.compact_session,
            flush_transcript=harness.flush_transcript,
        ),
        turn_runner=object(),
        build_tool_ctx=lambda _session_key: object(),
        replace_session=lambda **_updates: None,
    )


# --------------------------------------------------------------------------- #
# Word sets derive from the engine registry and the chains cover them          #
# --------------------------------------------------------------------------- #


def test_handler_word_sets_derive_from_engine_registry() -> None:
    assert GATEWAY_SLASH_HANDLER_WORDS == registry_handler_words(Surface.CLI_GATEWAY)
    assert STANDALONE_SLASH_HANDLER_WORDS == registry_handler_words(Surface.CLI_STANDALONE)
    assert "/meta" in GATEWAY_SLASH_HANDLER_WORDS
    assert "/usage" not in STANDALONE_SLASH_HANDLER_WORDS


async def test_gateway_handler_chain_covers_every_registry_word(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_gateway_io(monkeypatch)
    for word in sorted(GATEWAY_SLASH_HANDLER_WORDS - _RUNTIME_OWNED_WORDS):
        handled = await handle_gateway_slash_command(word, _gateway_context())
        assert handled is True, f"gateway handler chain does not dispatch {word}"


async def test_standalone_handler_chain_covers_every_registry_word(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    recorder = _patch_standalone_io(monkeypatch)
    for word in sorted(STANDALONE_SLASH_HANDLER_WORDS - _RUNTIME_OWNED_WORDS):
        recorder.entries.clear()
        handled = await handle_standalone_slash_command(word, _standalone_context())
        assert handled is True, f"standalone handler chain does not dispatch {word}"
        assert "Unknown command" not in recorder.text(), f"{word} fell through as unknown"


# --------------------------------------------------------------------------- #
# Twin return contracts                                                        #
# --------------------------------------------------------------------------- #


async def test_gateway_unknown_command_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    handled = await handle_gateway_slash_command("/definitely-unknown", _gateway_context())
    assert handled is False


async def test_standalone_unknown_command_prints_notice_and_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_standalone_io(monkeypatch)
    handled = await handle_standalone_slash_command("/definitely-unknown", _standalone_context())
    assert handled is True
    assert "Unknown command" in recorder.text()


# --------------------------------------------------------------------------- #
# Connection loss keeps the REPL alive                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cmd", "method"),
    [
        ("/sessions", "list_sessions"),
        ("/clear", "reset_session"),
        ("/usage", "usage_status"),
        ("/new", "create_session"),
    ],
)
async def test_gateway_connection_loss_renders_reconnect_hint(
    monkeypatch: pytest.MonkeyPatch,
    cmd: str,
    method: str,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.raise_map[method] = ConnectionError(
        "Gateway connection lost; restart chat or reconnect before sending another command."
    )

    handled = await handle_gateway_slash_command(cmd, _gateway_context(client))

    assert handled is True
    output = recorder.text()
    assert "Gateway command failed" in output
    assert "Gateway connection lost" in output
    assert "opensquilla gateway" in output


async def test_gateway_os_error_from_rpc_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.raise_map["list_models"] = OSError("socket closed")

    handled = await handle_gateway_slash_command("/models", _gateway_context(client))

    assert handled is True
    assert "socket closed" in recorder.text()


# --------------------------------------------------------------------------- #
# /save error mapping and durable precedence                                   #
# --------------------------------------------------------------------------- #


async def test_gateway_save_bad_path_renders_error_panel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.history = [{"role": "user", "text": "hello"}]
    target = tmp_path / "missing-dir" / "out.md"

    handled = await handle_gateway_slash_command(f"/save {target}", _gateway_context(client))

    assert handled is True
    assert "Could not save transcript" in recorder.text()
    assert not target.exists()


async def test_gateway_save_reads_all_canonical_history_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.history_pages = {
        None: {
            "messages": [
                {"message_id": "m3", "role": "user", "text": "later question"},
                {"message_id": "m4", "role": "assistant", "text": "later answer"},
            ],
            "has_more": True,
            "oldest_cursor": "3|3",
            "newest_cursor": "4|4",
        },
        "3|3": {
            "messages": [
                {"message_id": "m1", "role": "user", "text": "earliest question"},
                {"message_id": "m2", "role": "assistant", "text": "earliest answer"},
            ],
            "has_more": False,
            "oldest_cursor": "1|1",
            "newest_cursor": "2|2",
        },
    }
    target = tmp_path / "all-history.md"

    handled = await handle_gateway_slash_command(f"/save {target}", _gateway_context(client))

    assert handled is True
    saved = target.read_text(encoding="utf-8")
    assert saved.index("earliest question") < saved.index("later question")
    history_calls = [call for call in client.calls if call[0] == "session_history"]
    assert [call[1]["before"] for call in history_calls] == [None, "3|3"]
    assert all(call[1]["include_canonical"] is True for call in history_calls)
    assert all(call[1]["include_summaries"] is False for call in history_calls)


async def test_gateway_save_does_not_write_partial_file_when_cursor_stalls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    stalled_page = {
        "messages": [{"message_id": "m1", "role": "user", "text": "partial"}],
        "has_more": True,
        "oldest_cursor": "1|1",
        "newest_cursor": "1|1",
    }
    client.history_pages = {None: stalled_page, "1|1": stalled_page}
    target = tmp_path / "partial.md"

    handled = await handle_gateway_slash_command(f"/save {target}", _gateway_context(client))

    assert handled is True
    assert not target.exists()
    assert "cursor did not advance" in recorder.text()


async def test_standalone_save_bad_path_renders_error_panel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _patch_standalone_io(monkeypatch)
    context = _standalone_context()
    context.state.transcript.add("user", "hello")
    target = tmp_path / "missing-dir" / "out.md"

    handled = await handle_standalone_slash_command(f"/save {target}", context)

    assert handled is True
    assert "Could not save transcript" in recorder.text()
    assert not target.exists()


async def test_standalone_save_exports_durable_history_after_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = _patch_standalone_io(monkeypatch)
    harness = _StandaloneHarness()
    session_key = "agent:main:standalone:resumed"
    harness.transcripts[session_key] = [
        SimpleNamespace(role="user", content="persisted question"),
        SimpleNamespace(role="assistant", content="persisted answer"),
    ]
    context = _standalone_context(harness, session_key=session_key)
    target = tmp_path / "resumed.md"

    handled = await handle_standalone_slash_command(f"/save {target}", context)

    assert handled is True
    saved = target.read_text(encoding="utf-8")
    assert "persisted question" in saved
    assert "persisted answer" in saved
    assert "Saved transcript" in recorder.text()


async def test_standalone_save_falls_back_to_memory_when_durable_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_standalone_io(monkeypatch)
    harness = _StandaloneHarness()
    session_key = "agent:main:standalone:test"
    harness.read_errors[session_key] = RuntimeError("storage offline")
    context = _standalone_context(harness, session_key=session_key)
    context.state.transcript.add("user", "in-memory only")
    target = tmp_path / "fallback.md"

    handled = await handle_standalone_slash_command(f"/save {target}", context)

    assert handled is True
    assert "in-memory only" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Requested-vs-routed model separation                                         #
# --------------------------------------------------------------------------- #


async def test_gateway_new_does_not_pin_routed_display_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    # A router-default session: the stored session model is None even though
    # the display model shows the router's last pick.
    client.resolve_payloads["agent:main:test:0"] = {"model": None}
    context = _gateway_context(client, model="router/last-pick")

    handled = await handle_gateway_slash_command("/new", context)

    assert handled is True
    assert client.created[0]["model"] is None


async def test_gateway_new_prefers_explicit_requested_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    context = _gateway_context(client, model="router/last-pick", requested_model="openai/explicit")

    await handle_gateway_slash_command("/new", context)

    assert client.created[0]["model"] == "openai/explicit"


async def test_gateway_new_requested_model_survives_pin_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erroring resolve must never override an explicitly requested model."""
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.raise_map["resolve_session"] = RuntimeError("gateway busy")
    context = _gateway_context(client, model="router/last-pick", requested_model="openai/explicit")

    handled = await handle_gateway_slash_command("/new", context)

    assert handled is True
    assert client.created[0]["model"] == "openai/explicit"
    assert "Could not read" not in recorder.text()


async def test_gateway_new_inherits_stored_session_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:test:0"] = {"model": "openai/pinned"}
    context = _gateway_context(client, model="router/last-pick")

    await handle_gateway_slash_command("/new", context)

    assert client.created[0]["model"] == "openai/pinned"


async def test_gateway_new_warns_when_pin_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow/erroring resolve while creating a new session must not silently
    drop the pin: warn the user that the router default applies instead."""
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.raise_map["resolve_session"] = RuntimeError("gateway busy")
    context = _gateway_context(client, model="router/last-pick")

    handled = await handle_gateway_slash_command("/new", context)

    assert handled is True
    # No explicit pin could be read, so the new session is created unpinned...
    assert client.created[0]["model"] is None
    # ...but the user is told, rather than left assuming the pin carried over.
    output = recorder.text()
    assert "Could not read the current session's model pin" in output
    assert "/model" in output


async def test_gateway_resume_hydrates_canonical_history_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    output = _StructuredOutput()
    target = "agent:main:resumed"
    client.bootstrap_payloads[target] = {
        "session": {"session_key": target, "model": "openai/resumed"},
        "history": {
            "messages": [
                {"message_id": "m1", "role": "user", "text": "old question"},
                {"message_id": "m2", "role": "assistant", "text": "old answer"},
            ],
            "history_scope": "latest_window",
            "has_more": True,
            "loaded_count": 2,
            "canonical_available": True,
            "compaction_summaries": [],
        },
    }
    context = _gateway_context(client, tui_output=output)
    context.state.transcript.add("user", "stale")

    handled = await handle_gateway_slash_command(f"/resume {target}", context)

    assert handled is True
    assert context.state.session_key == target
    assert context.state.model == "openai/resumed"
    assert [turn.content for turn in context.state.transcript.turns] == [
        "old question",
        "old answer",
    ]
    assert [message_type for message_type, _payload in output.messages] == [
        "composer.set",
        "history.replace",
        "context.update",
        "composer.set",
    ]
    context_payload = output.messages[2][1]
    assert context_payload["task"] == "Session"
    assert context_payload["model"] == "openai/resumed"
    assert target not in repr(context_payload)
    assert output.messages[1][1]["history_scope"] == "latest_window"
    assert [item["id"] for item in output.messages[1][1]["messages"]] == ["m1", "m2"]


async def test_gateway_resume_without_id_opens_searchable_session_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.session_rows = [
        {
            "key": "agent:main:one",
            "display_name": "Daily coding",
            "model": "router/default",
            "status": "running",
            "message_count": 12,
        },
        {
            "session_key": "agent:main:malformed-count",
            "title": "Recoverable row",
            "entry_count": "unknown",
        },
        {"title": "missing key is ignored"},
    ]
    output = _StructuredOutput()
    context = _gateway_context(client, tui_output=output)

    assert await handle_gateway_slash_command("/resume", context) is True
    assert output.messages == [
        (
            "session.pick",
            {
                "current_key": "agent:main:test:0",
                "sessions": [
                    {
                        "key": "agent:main:one",
                        "title": "Daily coding",
                        "status": "running",
                        "model": "router/default",
                        "message_count": 12,
                    },
                    {
                        "key": "agent:main:malformed-count",
                        "title": "Recoverable row",
                        "status": "",
                        "model": "",
                        "message_count": 0,
                    },
                ],
            },
        )
    ]


async def test_gateway_reset_replaces_transcript_with_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    output = _StructuredOutput()
    context = _gateway_context(client, tui_output=output)
    context.state.transcript.add("user", "stale")

    handled = await handle_gateway_slash_command("/reset", context)

    assert handled is True
    assert context.state.transcript.turns == []
    assert output.messages[1][0] == "history.replace"
    assert output.messages[1][1]["messages"] == ()


async def test_gateway_file_attachment_reports_upload_progress_and_clears_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    output = _StructuredOutput()

    async def fake_prepare(
        command: str,
        *,
        upload_callable: Any,
    ) -> tuple[str, list[dict[str, Any]]]:
        assert command == "/file /private/brief.pdf summarize"
        file_uuid = await upload_callable(
            Path("/private/brief.pdf"),
            "application/pdf",
            "brief.pdf",
        )
        return "summarize", [{"type": "application/pdf", "file_uuid": file_uuid}]

    async def fake_stream(*_args: Any, **_kwargs: Any) -> TurnResult:
        return TurnResult(text="done")

    monkeypatch.setattr(_slash_gateway, "_async_file_prompt_and_attachments", fake_prepare)
    context = _gateway_context(
        client,
        tui_output=output,
        stream_response=fake_stream,
    )

    handled = await handle_gateway_slash_command(
        "/file /private/brief.pdf summarize",
        context,
    )

    assert handled is True
    assert [(kind, event.get("status")) for kind, event in output.attachment_events] == [
        ("add", "reading"),
        ("update", "uploading"),
        ("update", "ready"),
        ("clear", "ready"),
    ]
    assert output.attachment_events[0][1]["label"] == "brief.pdf"
    assert "/private" not in str(output.attachment_events)


async def test_gateway_image_and_path_attachments_show_ready_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.is_local_gateway = True
    output = _StructuredOutput()

    monkeypatch.setattr(
        _slash_gateway,
        "_image_prompt_and_attachments",
        lambda _cmd: ("describe", [{"type": "image/png", "data": "hidden"}]),
    )
    monkeypatch.setattr(
        _slash_gateway,
        "path_prompt_and_attachments",
        lambda _cmd: ("inspect", []),
    )

    async def fake_stream(*_args: Any, **_kwargs: Any) -> TurnResult:
        return TurnResult(text="done")

    context = _gateway_context(
        client,
        tui_output=output,
        stream_response=fake_stream,
    )

    assert await handle_gateway_slash_command(
        "/image /private/chart.png describe",
        context,
    )
    assert await handle_gateway_slash_command(
        "/path /private/report.md inspect",
        context,
    )

    add_events = [event for kind, event in output.attachment_events if kind == "add"]
    assert [(event["kind"], event["label"]) for event in add_events] == [
        ("image", "chart.png"),
        ("path", "report.md"),
    ]
    updates = [event.get("status") for kind, event in output.attachment_events if kind == "update"]
    assert updates == [
        "ready",
        "ready",
    ]
    assert "/private" not in str(output.attachment_events)


async def test_gateway_attachment_failure_stays_visible_without_path_or_size_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    output = _StructuredOutput()

    def fail_prepare(_command: str) -> tuple[str, list[dict[str, Any]]]:
        raise ValueError("File not found: /private/tmp/redaction-fixture/private.png (12345 bytes)")

    monkeypatch.setattr(_slash_gateway, "_image_prompt_and_attachments", fail_prepare)
    context = _gateway_context(tui_output=output)

    handled = await handle_gateway_slash_command(
        "/image /private/tmp/redaction-fixture/private.png describe",
        context,
    )

    assert handled is True
    assert [kind for kind, _event in output.attachment_events] == ["add", "update"]
    failure = output.attachment_events[-1][1]
    assert failure["status"] == "failed"
    assert failure["message"] == ("Could not prepare private.png; check the file and retry /image.")
    combined = f"{output.attachment_events}\n{recorder.text()}"
    assert "/private/tmp/redaction-fixture" not in combined
    assert "12345" not in combined


async def test_gateway_model_records_explicit_request_on_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    context = _gateway_context(client)

    handled = await handle_gateway_slash_command("/model openai/chosen", context)

    assert handled is True
    assert context.requested_model == "openai/chosen"
    assert context.state.model == "openai/chosen"
    assert ("patch_session", ("agent:main:test:0", {"model": "openai/chosen"})) in client.calls


async def test_gateway_model_bare_command_opens_model_picker() -> None:
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:test:0"] = {
        "model": "openai/chosen",
        "effective_model": "router/last-pick",
    }
    client.model_rows = [
        {
            "id": "openai/chosen",
            "provider": "openai",
            "contextWindow": 128_000,
        }
    ]
    output = _StructuredOutput()

    handled = await handle_gateway_slash_command(
        "/model",
        _gateway_context(client, model="router/last-pick", requested_model=None, tui_output=output),
    )

    assert handled is True
    assert output.messages == [
        (
            "model.picker",
            {
                "current": "openai/chosen",
                "options": [
                    {
                        "id": "openai/chosen",
                        "provider": "openai",
                        "context_window": 128_000,
                    }
                ],
            },
        )
    ]


async def test_gateway_model_bare_command_plain_fallback_lists_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    listed: list[dict[str, Any]] = []
    monkeypatch.setattr(_slash_gateway, "_print_models_table", listed.extend)
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:test:0"] = {"model": None}
    client.model_rows = [
        {
            "id": "openai/example",
            "provider": "openai",
            "contextWindow": 128_000,
            "capabilities": ["text"],
        }
    ]

    handled = await handle_gateway_slash_command("/model", _gateway_context(client))

    assert handled is True
    assert "model pin" in recorder.text()
    assert "auto" in recorder.text()
    assert listed == client.model_rows


async def test_gateway_model_status_reads_canonical_pin_not_routed_display_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:test:0"] = {
        "model": "openai/pinned",
        "effective_model": "router/last-pick",
    }

    handled = await handle_gateway_slash_command(
        "/model status",
        _gateway_context(client, model="router/last-pick", requested_model=None),
    )

    assert handled is True
    assert "openai/pinned" in recorder.text()
    assert "router/last-pick" not in recorder.text()


async def test_gateway_resume_refreshes_in_context_model_pin_from_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    target = "agent:main:resumed"
    client.bootstrap_payloads[target] = {
        "session": {
            "session_key": target,
            "model": "openai/resumed-pin",
            "effective_model": "router/resumed-pick",
        },
        "history": {"messages": [], "history_scope": "complete"},
    }
    context = _gateway_context(
        client,
        requested_model="openai/old-pin",
        tui_output=_StructuredOutput(),
    )

    handled = await handle_gateway_slash_command(f"/resume {target}", context)

    assert handled is True
    assert context.requested_model == "openai/resumed-pin"
    assert context.state.model == "router/resumed-pick"


async def test_gateway_model_auto_clears_explicit_session_pin() -> None:
    client = _StubGatewayClient()
    output = _StructuredOutput()
    context = _gateway_context(
        client,
        model="openai/chosen",
        requested_model="openai/chosen",
        tui_output=output,
    )

    handled = await handle_gateway_slash_command("/model auto", context)

    assert handled is True
    assert context.requested_model is None
    assert context.state.model is None
    assert ("patch_session", ("agent:main:test:0", {"model": None})) in client.calls
    assert output.messages[-1] == ("context.update", {"model": "default"})


# --------------------------------------------------------------------------- #
# /delete of the active session                                                #
# --------------------------------------------------------------------------- #


async def test_gateway_delete_active_session_switches_to_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:test:0"] = {
        "session_key": "agent:main:test:0",
        "model": None,
    }
    context = _gateway_context(client)
    context.state.transcript.add("user", "hello")

    handled = await handle_gateway_slash_command("/delete agent:main:test:0", context)

    assert handled is True
    assert len(client.created) == 1
    assert context.state.session_key == client.created[0]["key"]
    assert context.state.transcript.turns == []
    assert "switched to a new session" in recorder.text()


async def test_gateway_delete_other_session_keeps_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:other"] = {
        "session_key": "agent:main:other",
        "model": None,
    }
    context = _gateway_context(client)

    handled = await handle_gateway_slash_command("/delete agent:main:other", context)

    assert handled is True
    assert client.created == []
    assert context.state.session_key == "agent:main:test:0"
    assert "Deleted session" in recorder.text()


async def test_gateway_delete_active_session_refreshes_display_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the active session must refresh state.model to the replacement
    session's pin, like /new does — not leave the deleted session's stale
    display model showing in /status and the HUD."""
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.resolve_payloads["agent:main:test:0"] = {
        "session_key": "agent:main:test:0",
        "model": "openai/replacement-pin",
    }
    # The replacement session (next created key) resolves to its own pin.
    client.resolve_payloads["agent:main:test:1"] = {"model": "openai/replacement-pin"}
    context = _gateway_context(client, model="openai/stale-display")

    handled = await handle_gateway_slash_command("/delete agent:main:test:0", context)

    assert handled is True
    assert client.created[0]["model"] == "openai/replacement-pin"
    assert context.state.model == "openai/replacement-pin"


async def test_gateway_delete_active_session_model_falls_back_on_resolve_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the post-create resolve fails, the display model still reflects the
    replacement pin rather than the deleted session's stale value."""
    _patch_gateway_io(monkeypatch)

    class _DeleteResolveOnceClient(_StubGatewayClient):
        def __init__(self) -> None:
            super().__init__()
            self._resolves = 0

        async def resolve_session(self, key: str) -> dict[str, Any]:
            self._resolves += 1
            # First resolve (the /delete target lookup) succeeds; the
            # post-create refresh resolve fails.
            if self._resolves >= 2:
                raise RuntimeError("gateway busy")
            return {"session_key": key, "model": "openai/replacement-pin"}

    client = _DeleteResolveOnceClient()
    context = _gateway_context(client, model="openai/stale-display")

    handled = await handle_gateway_slash_command("/delete agent:main:test:0", context)

    assert handled is True
    assert client.created[0]["model"] == "openai/replacement-pin"
    assert context.state.model == "openai/replacement-pin"


# --------------------------------------------------------------------------- #
# Destructive / exit classification matches dispatch                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("command", ["/clear", "  /clear  ", "/reset", "/compact", "/cmp"])
def test_classify_destructive_exact_bare_word(command: str) -> None:
    assert classify(command) is SlashCategory.DESTRUCTIVE


@pytest.mark.parametrize("command", ["/CLEAR", "/Clear", "/clear now", "/reset trailing-junk"])
def test_classify_never_purges_for_inputs_dispatch_rejects(command: str) -> None:
    category = classify(command)
    assert category is not SlashCategory.DESTRUCTIVE
    assert category is not SlashCategory.EXIT
    assert category is not SlashCategory.NON_SLASH


@pytest.mark.parametrize("command", ["/exit", "/quit", " /exit "])
def test_classify_exit_exact_bare_word(command: str) -> None:
    assert classify(command) is SlashCategory.EXIT


@pytest.mark.parametrize("command", ["/EXIT", "/exit now", "/Quit"])
def test_classify_exit_variants_enqueue_for_runtime_interception(command: str) -> None:
    category = classify(command)
    assert category is not SlashCategory.EXIT
    assert category is not SlashCategory.DESTRUCTIVE
    assert category is not SlashCategory.NON_SLASH


@pytest.mark.parametrize("command", ["/EXIT", "/exit now", "/Quit"])
def test_runtime_exit_interception_rejects_malformed_slash_variants(command: str) -> None:
    """Command-plane input must not bypass the exact drain-and-exit policy."""
    assert is_exit_command(command, Surface.CLI_GATEWAY) is False
    assert is_exit_command(command, Surface.CLI_STANDALONE) is False


@pytest.mark.parametrize("command", ["exit", "EXIT", "quit", "QUIT", ":q", ":Q"])
def test_runtime_exit_interception_preserves_bare_exit_compatibility(command: str) -> None:
    assert is_exit_command(command, Surface.CLI_GATEWAY) is True


@pytest.mark.parametrize(
    "command",
    ["/router", "/router on", "/router status", "/ensemble", "/ensemble off"],
)
def test_classify_model_strategy_as_immediate_control(command: str) -> None:
    assert classify(command) is SlashCategory.CONTROL


@pytest.mark.parametrize("command", ["/strategy", "/router on", "/ensemble", "/meta foo"])
def test_standalone_gateway_only_commands_stay_off_the_turn_plane(command: str) -> None:
    assert classify(command, surface=Surface.CLI_STANDALONE) is SlashCategory.COMMAND


async def test_gateway_model_strategy_bare_command_opens_shared_picker() -> None:
    client = _StubGatewayClient()
    output = _StructuredOutput()

    handled = await handle_gateway_slash_command(
        "/router",
        _gateway_context(client, tui_output=output),
    )

    assert handled is True
    assert output.messages == [
        (
            "model.routing.picker",
            {
                "current": "direct",
                "options": ["direct", "router", "ensemble"],
            },
        )
    ]
    assert ("set_model_routing", "router") not in client.calls


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/router on", "router"),
        ("/router off", "direct"),
        ("/ensemble on", "ensemble"),
        ("/ensemble off", "direct"),
        ("/strategy direct", "direct"),
        ("/strategy router", "router"),
        ("/strategy ensemble", "ensemble"),
    ],
)
async def test_gateway_model_strategy_command_sets_canonical_mode(
    command: str,
    expected: str,
) -> None:
    client = _StubGatewayClient()
    output = _StructuredOutput()

    handled = await handle_gateway_slash_command(
        command,
        _gateway_context(client, tui_output=output),
    )

    assert handled is True
    assert ("set_model_routing", expected) in client.calls
    assert output.messages[-1][0] == "model.routing.state"
    assert output.messages[-1][1]["mode"] == expected


async def test_gateway_model_strategy_missing_control_rpc_is_explicit_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.raise_map["get_model_routing"] = RuntimeError("METHOD_NOT_FOUND")

    handled = await handle_gateway_slash_command(
        "/router on",
        _gateway_context(client, tui_output=_StructuredOutput()),
    )

    assert handled is True
    assert "read-only" in recorder.text()
    assert ("set_model_routing", "router") not in client.calls


async def test_gateway_model_strategy_failed_write_reprojects_canonical_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    client.raise_map["set_model_routing"] = RuntimeError("operator.write required")
    output = _StructuredOutput()

    handled = await handle_gateway_slash_command(
        "/ensemble on",
        _gateway_context(client, tui_output=output),
    )

    assert handled is True
    assert "strategy remains direct" in recorder.text()
    assert output.messages[-1] == (
        "model.routing.state",
        {
            "mode": "direct",
            "router_enabled": False,
            "ensemble_enabled": False,
            "selection_mode": "router_dynamic",
            "rollout_phase": "observe",
            "applies_to": "next_accepted_turn",
            "busy": False,
        },
    )


# --------------------------------------------------------------------------- #
# Protocol double works for approval-flavored commands                         #
# --------------------------------------------------------------------------- #


async def test_gateway_approval_commands_accept_protocol_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gateway_io(monkeypatch)
    client = _StubGatewayClient()
    context = _gateway_context(client)

    assert await handle_gateway_slash_command("/approvals", context) is True
    assert await handle_gateway_slash_command("/forget some-target", context) is True
    assert await handle_gateway_slash_command("/permissions off", context) is True
    assert ("forget_approvals", "some-target") in client.calls
    assert ("set_approval_mode", "prompt") in client.calls
    assert context.state.elevated is None


def test_tool_compress_dead_code_is_removed() -> None:
    assert not hasattr(_slash_gateway, "_handle_tool_compress_command")
    assert not hasattr(_slash_bridge, "handle_tool_compress_command")


# --------------------------------------------------------------------------- #
# Shared helper behavior                                                       #
# --------------------------------------------------------------------------- #


def test_resolve_transcript_target_defaults_to_session_derived_name() -> None:
    target = resolve_transcript_target("/save", "agent:main:test:1")
    assert target == Path("opensquilla-chat-agent-main-test-1.md")
    explicit = resolve_transcript_target("/save /tmp/out.md", "agent:main:test:1")
    assert explicit == Path("/tmp/out.md")


def test_transcript_messages_to_markdown_accepts_dicts_and_rows() -> None:
    markdown = transcript_messages_to_markdown(
        [
            {"role": "user", "text": "dict question"},
            SimpleNamespace(role="assistant", content="row answer"),
        ]
    )
    assert "dict question" in markdown
    assert "row answer" in markdown


def test_record_turn_updates_transcript_and_usage() -> None:
    state = ChatSessionState(session_key="agent:main:test:2")
    record_turn(state, "ask", TurnResult(text="answer"))
    assert [turn.role for turn in state.transcript.turns] == ["user", "assistant"]
    assert state.transcript.turns[0].content == "ask"
    assert state.transcript.turns[1].content == "answer"


class _HostCapableOutput:
    supports_send_message = True

    async def send_message(self, *_args: Any) -> None:
        return None


async def test_keys_dispatch_selects_cheatsheet_by_backend_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Binds the wiring, not the parts: a host-capable output must get the
    # OpenTUI chords, and a native/absent output must get only the honest
    # plain-mode rows — never OpenTUI-only shortcuts it cannot deliver.
    gateway_recorder = _patch_gateway_io(monkeypatch)
    handled = await handle_gateway_slash_command(
        "/keys", _gateway_context(tui_output=_HostCapableOutput())
    )
    assert handled is True
    table = gateway_recorder.entries[-1]
    opentui_rows = "\n".join(str(cell) for column in table.columns for cell in column.cells)
    assert "Ctrl+O" in opentui_rows

    gateway_recorder.entries.clear()
    handled = await handle_gateway_slash_command("/keys", _gateway_context(tui_output=None))
    assert handled is True
    table = gateway_recorder.entries[-1]
    native_rows = "\n".join(str(cell) for column in table.columns for cell in column.cells)
    assert "Ctrl+O" not in native_rows
    assert "Ctrl+C" in native_rows

    standalone_recorder = _patch_standalone_io(monkeypatch)
    handled = await handle_standalone_slash_command("/shortcuts", _standalone_context())
    assert handled is True
    table = standalone_recorder.entries[-1]
    native_rows = "\n".join(str(cell) for column in table.columns for cell in column.cells)
    assert "Ctrl+O" not in native_rows
