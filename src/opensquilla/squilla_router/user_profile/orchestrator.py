"""Async driver: select sessions -> gate -> LLM extract -> aggregate -> persist.

The wiring layer remains provider-neutral: the gateway injects the resolved
Dream provider, stream factory, and complete baseline profile. Its single
public entry point never raises — a failure logs and returns, so the post-dream
hook is never poisoned and no half-written profile ships.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from opensquilla.squilla_router.user_profile import builder, extractor, store
from opensquilla.squilla_router.user_profile.gates import evaluate_profile_gates
from opensquilla.squilla_router.user_profile.schema import BatchAnalysis
from opensquilla.squilla_router.user_profile.state import (
    ProfileRunState,
    load_run_state,
    save_run_state,
)

log = structlog.get_logger(__name__)

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
WINDOW_DAYS = 90
PER_SESSION_MAX_CHARS = 6000
BATCH_SIZE = 10
BATCH_INPUT_MAX_CHARS = 48000
# Some Dream-selected reasoning models consume a substantial part of
# ``max_tokens`` in provider-side reasoning even when ``thinking=False``. A
# live OpenRouter/DeepSeek V4 Pro run used ~2.5k reasoning tokens before
# emitting the compact profile JSON, so 1.5k produced a DoneEvent with no text.
# Leave enough room for both hidden reasoning and the bounded JSON artifact.
MAX_OUTPUT_TOKENS = 6000
TEMPERATURE = 0.0
TIMEOUT_SECONDS = 120.0
RESPONSE_MAX_CHARS = 48000
TOP_N_CAPABILITIES = 3


@dataclass
class ProfileRunResult:
    ran: bool
    reason: str
    version: str | None = None
    sessions_read: int = 0
    batches: int = 0


def _ms_to_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, UTC).strftime(_TS_FMT)


async def maybe_produce_user_profile(
    agent_id: str,
    *,
    base_profile: Mapping[str, Any],
    permission_snapshot: Mapping[str, Any] | None = None,
    storage: Any,
    build_provider: Any,
    stream_factory: extractor.StreamFactory,
    home: Path | None = None,
    now: datetime | None = None,
) -> ProfileRunResult:
    """Produce one user-profile version if the gates allow. Never raises.

    ``storage`` exposes ``list_session_ids_updated_since`` and ``get_transcript``;
    ``build_provider`` is a zero-arg callable returning a resolved provider (or
    ``None``), invoked only after the gates pass so a gated run costs nothing.
    ``stream_factory`` adapts that provider to the provider-neutral extractor.
    """

    try:
        return await _run(
            agent_id,
            base_profile=base_profile,
            permission_snapshot=permission_snapshot,
            storage=storage,
            build_provider=build_provider,
            stream_factory=stream_factory,
            home=home,
            now=now or datetime.now(UTC),
        )
    except Exception as exc:  # noqa: BLE001 — the producer must never poison the hook
        log.warning("user_profile.produce_error", agent_id=agent_id, error=str(exc))
        _bump_failure(agent_id, home)
        return ProfileRunResult(ran=False, reason="error")


def _bump_failure(agent_id: str, home: Path | None) -> None:
    try:
        state = load_run_state(agent_id, home)
        state.consecutive_failures += 1
        save_run_state(state, agent_id, home)
    except Exception:  # noqa: BLE001 — bookkeeping is best-effort
        pass


def _record_attempt(
    state: ProfileRunState, agent_id: str, home: Path | None, now: datetime
) -> None:
    state.last_attempt_ts = now.strftime(_TS_FMT)
    save_run_state(state, agent_id, home)


async def _run(
    agent_id: str,
    *,
    base_profile: Mapping[str, Any],
    permission_snapshot: Mapping[str, Any] | None,
    storage: Any,
    build_provider: Any,
    stream_factory: extractor.StreamFactory,
    home: Path | None,
    now: datetime,
) -> ProfileRunResult:
    state = load_run_state(agent_id, home)

    now_ms = int(now.timestamp() * 1000)
    since_ms = now_ms - WINDOW_DAYS * 86_400_000

    rows = await storage.list_session_ids_updated_since(since_ms, agent_id=agent_id)
    session_count = len(rows)
    latest_ms = max((updated for _sid, updated in rows), default=None)
    latest_activity_ts = _ms_to_ts(latest_ms) if latest_ms is not None else None

    gate = evaluate_profile_gates(
        state=state,
        session_count=session_count,
        latest_activity_ts=latest_activity_ts,
        now=now,
    )
    if not gate.should_run:
        log.info("user_profile.gated", agent_id=agent_id, reason=gate.reason, **gate.stats)
        return ProfileRunResult(ran=False, reason=gate.reason)

    _record_attempt(state, agent_id, home, now)
    provider = build_provider()
    if provider is None:
        log.info("user_profile.no_provider", agent_id=agent_id)
        return ProfileRunResult(ran=False, reason="no_provider")

    rendered = []
    for session_id, _updated in rows:
        try:
            entries = await storage.get_transcript(session_id)
        except Exception:  # noqa: BLE001 — one unreadable session must not abort
            continue
        session = extractor.render_transcript(
            session_id, entries, per_session_max_chars=PER_SESSION_MAX_CHARS
        )
        if session.text:
            rendered.append(session)

    batches_input = extractor.batch_sessions(
        rendered,
        batch_size=BATCH_SIZE,
        batch_input_max_chars=BATCH_INPUT_MAX_CHARS,
    )
    sessions_read = sum(len(batch) for batch in batches_input)
    if sessions_read <= 0:
        log.info("user_profile.no_readable_sessions", agent_id=agent_id)
        return ProfileRunResult(ran=False, reason="no_readable_sessions")

    analyses: list[BatchAnalysis] = []
    for batch in batches_input:
        analyses.append(
            await extractor.extract_batch(
                provider=provider,
                stream_factory=stream_factory,
                batch=batch,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=TEMPERATURE,
                timeout=TIMEOUT_SECONDS,
                response_max_chars=RESPONSE_MAX_CHARS,
            )
        )

    if not any(analysis.ok for analysis in analyses):
        log.warning("user_profile.all_batches_failed", agent_id=agent_id)
        state.consecutive_failures += 1
        save_run_state(state, agent_id, home)
        return ProfileRunResult(ran=False, reason="all_batches_failed")

    day = now.strftime("%Y-%m-%d")
    version = store.next_version(day, agent_id, home)
    successful_sessions_read = sum(
        len(analysis.session_ids) for analysis in analyses if analysis.ok
    )
    profile_base = copy.deepcopy(dict(base_profile))
    if isinstance(permission_snapshot, Mapping):
        permission = dict(profile_base.get("permission") or {})
        permission.update(
            {
                key: list(value)
                for key, value in permission_snapshot.items()
                if key in {"allow_models", "deny_models", "allow_tools", "risk_allowlist"}
                and isinstance(value, list | tuple)
            }
        )
        profile_base["permission"] = permission
    payload = builder.build_profile(
        batches=analyses,
        base_profile=profile_base,
        sessions_read=successful_sessions_read,
        day=day,
        version=version,
        top_n=TOP_N_CAPABILITIES,
        window_days=WINDOW_DAYS,
    )

    store.write_profile_version(payload, version, agent_id, home=home)
    store.write_active_atomic(version, agent_id, home=home)

    state = ProfileRunState(
        last_attempt_ts=state.last_attempt_ts,
        last_run_ts=now.strftime(_TS_FMT),
        last_version=version,
        consecutive_failures=0,
    )
    save_run_state(state, agent_id, home)

    log.info(
        "user_profile.produced",
        agent_id=agent_id,
        version=version,
        sessions_read=successful_sessions_read,
        batches=len(analyses),
    )
    return ProfileRunResult(
        ran=True,
        reason="ready",
        version=version,
        sessions_read=successful_sessions_read,
        batches=len(analyses),
    )


__all__ = ["ProfileRunResult", "maybe_produce_user_profile"]
