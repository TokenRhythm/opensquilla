"""End-to-end wiring: the gate decides, and only a full run ships a profile.

The orchestrator is the one place storage, the provider, and the on-disk store
meet, so these tests drive it with fakes and assert the two things the wiring
must guarantee: a gated or provider-less run writes *nothing* (no active pointer,
no version file), and a failure — a storage raise or every batch failing to
parse — leaves no half-written profile while bumping the consecutive-failure
counter so a broken provider backs off instead of retrying every dream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.squilla_router.user_profile import store
from opensquilla.squilla_router.user_profile.defaults import default_user_profile
from opensquilla.squilla_router.user_profile.orchestrator import (
    MAX_OUTPUT_TOKENS,
    TIMEOUT_SECONDS,
    maybe_produce_user_profile,
)
from opensquilla.squilla_router.user_profile.state import load_run_state

_AGENT = "main"
_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


def test_llm_budget_leaves_room_for_forced_reasoning_before_json() -> None:
    """Dream-selected reasoning models must still have budget for final JSON."""

    assert MAX_OUTPUT_TOKENS >= 6000
    assert TIMEOUT_SECONDS >= 120.0


@dataclass
class _Row:
    role: str
    content: str | None


class _Storage:
    """Fake session storage: a fixed row set and canned transcripts."""

    def __init__(
        self,
        session_ids: list[str],
        *,
        hours_ago: float = 5.0,
        raise_on_list: bool = False,
    ) -> None:
        latest_ms = int((_NOW.timestamp() - hours_ago * 3600) * 1000)
        self._rows = [(sid, latest_ms) for sid in session_ids]
        self._raise_on_list = raise_on_list
        self.last_limit = "not-called"

    async def list_session_ids_updated_since(
        self, since_ms: int, *, agent_id: str | None = None, limit: int | None = None
    ) -> list[tuple[str, int]]:
        self.last_limit = limit
        if self._raise_on_list:
            raise RuntimeError("db down")
        return list(self._rows)

    async def get_transcript(self, session_id: str) -> list[_Row]:
        return [_Row("user", f"write code for {session_id}"), _Row("assistant", "ok")]


class _Provider:
    """A provider whose ``chat`` replays a scripted event stream."""

    def __init__(self, events) -> None:  # noqa: ANN001
        self._events = events

    def chat(self, messages, tools=None, config=None):  # noqa: ANN001
        events = self._events

        async def _stream():
            for event in events:
                yield event

        return _stream()


class _SequencedProvider:
    """A provider whose successive ``chat`` calls consume successive streams."""

    def __init__(self, streams) -> None:  # noqa: ANN001
        self._streams = list(streams)
        self.calls = 0

    def chat(self, messages, tools=None, config=None):  # noqa: ANN001
        events = self._streams[self.calls]
        self.calls += 1

        async def _stream():
            for event in events:
                yield event

        return _stream()


def _good_stream(session_ids: list[str]):
    payload = {
        "session_labels": [
            {"session_id": sid, "capability": "code_generation", "confidence": 0.8}
            for sid in session_ids
        ],
        "quality_latency_tradeoff": {
            "value": "quality_first",
            "confidence": 0.7,
            "session_ids": session_ids,
        },
        "model_mentions": [],
    }
    return [TextDeltaEvent(text=json.dumps(payload)), DoneEvent()]


def _stream_factory(
    *,
    provider,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
):
    del user_prompt, system_prompt, max_output_tokens, temperature, timeout
    return provider.chat([], tools=None, config=None)


async def _produce(
    storage: _Storage,
    provider,
    home: Path,
    *,
    permission_snapshot: dict | None = None,
):
    return await maybe_produce_user_profile(
        _AGENT,
        base_profile=default_user_profile(),
        permission_snapshot=permission_snapshot,
        storage=storage,
        build_provider=lambda: provider,
        stream_factory=_stream_factory,
        home=home,
        now=_NOW,
    )


def _nothing_written(home: Path) -> bool:
    directory = store.profiles_dir(_AGENT, home)
    versions = list(directory.glob("user_profile.*.json")) if directory.is_dir() else []
    return store.read_active_name(_AGENT, home) is None and versions == []


async def test_a_full_run_writes_a_versioned_active_profile(tmp_path: Path) -> None:
    ids = [f"s{i}" for i in range(25)]
    storage = _Storage(ids)
    result = await _produce(storage, _Provider(_good_stream(ids)), tmp_path)

    assert result.ran is True
    assert result.version is not None
    assert storage.last_limit is None
    # The active pointer and the version file it names both exist.
    assert store.read_active_name(_AGENT, tmp_path) == store.version_filename(result.version)
    version_file = store.profiles_dir(_AGENT, tmp_path) / store.version_filename(result.version)
    assert version_file.is_file()
    # A successful run stamps the run time and clears the failure counter.
    state = load_run_state(_AGENT, tmp_path)
    assert state.last_attempt_ts is not None
    assert state.last_run_ts is not None
    assert state.last_version == result.version
    assert state.consecutive_failures == 0


async def test_full_run_persists_the_gateway_permission_snapshot(tmp_path: Path) -> None:
    ids = [f"s{i}" for i in range(25)]
    permission = {
        "allow_models": ["model-a"],
        "deny_models": ["model-b"],
        "allow_tools": ["memory_search"],
        "risk_allowlist": ["low", "medium"],
    }

    result = await _produce(
        _Storage(ids),
        _Provider(_good_stream(ids)),
        tmp_path,
        permission_snapshot=permission,
    )

    assert result.ran is True
    payload = store.load_active_profile(_AGENT, tmp_path)
    assert payload is not None
    assert payload["permission"] == permission


async def test_partial_permission_snapshot_keeps_the_complete_schema(
    tmp_path: Path,
) -> None:
    ids = [f"s{i}" for i in range(25)]

    result = await _produce(
        _Storage(ids),
        _Provider(_good_stream(ids)),
        tmp_path,
        permission_snapshot={"deny_models": ["model-b"]},
    )

    assert result.ran is True
    payload = store.load_active_profile(_AGENT, tmp_path)
    assert payload is not None
    assert payload["permission"] == {
        "allow_models": [],
        "deny_models": ["model-b"],
        "allow_tools": [],
        "risk_allowlist": ["low", "medium", "high"],
    }


async def test_env_disabled_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """The internal kill switch short-circuits before provider or any write."""
    monkeypatch.setenv("OPENSQUILLA_USER_PROFILE_DISABLED", "1")
    result = await _produce(_Storage([f"s{i}" for i in range(25)]), None, tmp_path)
    assert result.ran is False
    assert result.reason == "disabled"
    assert _nothing_written(tmp_path)


async def test_too_few_sessions_never_calls_the_provider_or_writes(tmp_path: Path) -> None:
    # min_sessions is 3; one in-window session cannot produce a stable profile.
    provider = _Provider(_good_stream(["s0"]))
    result = await _produce(_Storage(["s0"]), provider, tmp_path)
    assert result.ran is False
    assert result.reason == "insufficient_sessions"
    assert _nothing_written(tmp_path)


async def test_a_null_provider_after_the_gate_writes_nothing(tmp_path: Path) -> None:
    """Gates pass, but the provider could not be built — no profile ships."""
    result = await _produce(_Storage([f"s{i}" for i in range(25)]), None, tmp_path)
    assert result.ran is False
    assert result.reason == "no_provider"
    assert _nothing_written(tmp_path)
    assert load_run_state(_AGENT, tmp_path).last_attempt_ts is not None


async def test_failed_provider_attempt_is_cooldown_gated(tmp_path: Path) -> None:
    """A failed attempt still stamps cooldown before provider construction."""
    ids = [f"s{i}" for i in range(25)]
    first = await _produce(_Storage(ids), None, tmp_path)
    assert first.reason == "no_provider"

    second = await _produce(_Storage(ids), _Provider(_good_stream(ids)), tmp_path)
    assert second.ran is False
    assert second.reason == "cooldown"
    assert _nothing_written(tmp_path)


async def test_a_storage_raise_writes_nothing_and_bumps_failures(tmp_path: Path) -> None:
    """A raise anywhere in the run degrades to a logged no-op, never a raise."""
    storage = _Storage([f"s{i}" for i in range(25)], raise_on_list=True)
    result = await _produce(storage, _Provider(_good_stream([])), tmp_path)

    assert result.ran is False
    assert result.reason == "error"
    assert _nothing_written(tmp_path)
    assert load_run_state(_AGENT, tmp_path).consecutive_failures == 1


async def test_every_batch_failing_bumps_failures_and_writes_nothing(tmp_path: Path) -> None:
    """No parseable batch means no evidence — write nothing, back off."""
    ids = [f"s{i}" for i in range(25)]
    provider = _Provider([TextDeltaEvent(text="I cannot help."), DoneEvent()])
    result = await _produce(_Storage(ids), provider, tmp_path)

    assert result.ran is False
    assert result.reason == "all_batches_failed"
    assert _nothing_written(tmp_path)
    state = load_run_state(_AGENT, tmp_path)
    assert state.last_attempt_ts is not None
    assert state.consecutive_failures == 1


async def test_feedback_count_counts_only_successful_batches(tmp_path: Path) -> None:
    ids = [f"s{i}" for i in range(25)]
    ok_ids = ids[:10]
    provider = _SequencedProvider(
        [
            _good_stream(ok_ids),
            [TextDeltaEvent(text="bad"), DoneEvent()],
            [TextDeltaEvent(text="bad"), DoneEvent()],
        ]
    )
    result = await _produce(_Storage(ids), provider, tmp_path)

    assert result.ran is True
    assert result.sessions_read == 10
    profile = store.load_active_profile(_AGENT, tmp_path)
    assert profile is not None
    assert profile["history"]["feedback_count"] == 10


async def test_normal_run_keeps_historical_versions_unpruned(tmp_path: Path) -> None:
    for seq in range(1, 18):
        store.write_profile_version(
            {"profile_version": f"old-{seq}"},
            f"2026-07-19.{seq}",
            _AGENT,
            home=tmp_path,
        )
    ids = [f"s{i}" for i in range(25)]
    result = await _produce(_Storage(ids), _Provider(_good_stream(ids)), tmp_path)

    assert result.ran is True
    directory = store.profiles_dir(_AGENT, tmp_path)
    versions = sorted(directory.glob("user_profile.*.json"))
    assert len(versions) == 18
    assert (directory / "user_profile.2026-07-19.1.json").is_file()
