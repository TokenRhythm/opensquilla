"""meta_command_launch: seeds a pending /meta launch onto ctx.metadata.

The store is one-shot and turn-bound: ``meta.run`` stamps a launch, then the
surface sends a turn whose text is the ``/meta <name>`` sentinel. Only that
launch turn seeds ``ctx.metadata["meta_launch"]`` and consumes the entry; an
ordinary turn never claims a pending launch, so a stamped-but-unclaimed launch
cannot hijack the next message. A turn with no session id or no pending launch
is a no-op.
"""

from __future__ import annotations

import pytest

from opensquilla.engine.steps.meta_command import (
    format_meta_replay_sentinel,
    meta_command_launch,
    pending_meta_launch_clear_session,
    pending_meta_launch_consumed_count,
    pending_meta_launch_peek,
    pending_meta_launch_pop,
    pending_meta_launch_promote,
    pending_meta_launch_put,
    pending_meta_launch_state,
    pending_meta_replay_count,
    pending_meta_replay_pop,
    pending_meta_replay_put,
)
from opensquilla.session.turn_context import turn_context_scope


def test_pending_meta_launch_clear_session_removes_only_the_target_session() -> None:
    pending_meta_launch_put("boundary-target", "meta-short-drama")
    pending_meta_launch_put(
        "boundary-target", "meta-paper-write", client_request_id="old-request"
    )
    pending_meta_launch_put("boundary-other", "meta-short-drama")

    assert pending_meta_launch_clear_session("boundary-target") == 2
    assert pending_meta_launch_peek("boundary-target") is None
    assert pending_meta_launch_peek("boundary-other") == "meta-short-drama"
    pending_meta_launch_pop("boundary-other")


def test_pending_meta_launch_clear_session_preserves_exact_reset_launch() -> None:
    pending_meta_launch_put(
        "boundary-reset", "meta-short-drama", client_request_id="old-request"
    )
    pending_meta_launch_put(
        "boundary-reset", "meta-paper-write", client_request_id="reset-request"
    )
    pending_meta_launch_promote(
        "boundary-reset",
        client_request_id="reset-request",
        message="/meta meta-paper-write",
    )

    assert pending_meta_launch_clear_session(
        "boundary-reset",
        preserve_client_request_id="reset-request",
        preserve_message="/meta meta-paper-write",
    ) == 1
    assert pending_meta_launch_peek(
        "boundary-reset", client_request_id="old-request"
    ) is None
    assert pending_meta_launch_peek(
        "boundary-reset", client_request_id="reset-request"
    ) == "meta-paper-write"
    pending_meta_launch_pop("boundary-reset", client_request_id="reset-request")


def _make_ctx(session_key: str, message: str = ""):
    """Minimal TurnContext-shaped stub the step reads.

    The step touches ``ctx.session_key`` and ``ctx.message`` /
    ``ctx.semantic_message`` (read) and ``ctx.metadata`` (read/write), so a
    tiny stub with those is sufficient and avoids constructing a full
    provider-bearing TurnContext.
    """

    class _Ctx:
        def __init__(self, key: str, msg: str) -> None:
            self.session_key = key
            self.message = msg
            self.metadata: dict = {}

        @property
        def semantic_message(self) -> str:
            return self.message

    return _Ctx(session_key, message)


@pytest.mark.asyncio
async def test_launch_turn_seeds_marker_then_one_shot_consumes() -> None:
    pending_meta_launch_put("S1", "meta-tiny")

    # The launch turn carries the "/meta <name>" sentinel every surface sends.
    ctx = _make_ctx("S1", "/meta meta-tiny")
    out = await meta_command_launch(ctx)
    assert out is ctx
    assert ctx.metadata["meta_launch"] == {"name": "meta-tiny"}

    # One-shot: a second launch turn (no new stamp) leaves no marker.
    ctx2 = _make_ctx("S1", "/meta meta-tiny")
    await meta_command_launch(ctx2)
    assert "meta_launch" not in ctx2.metadata


@pytest.mark.asyncio
async def test_stale_launch_does_not_hijack_a_normal_turn() -> None:
    # A launch was stamped but its "/meta <name>" launch turn never arrived.
    pending_meta_launch_put("S2", "meta-tiny")

    # The user instead sends an ordinary message. It must NOT be hijacked into
    # the meta-skill, and the pending launch must survive for its real turn.
    normal = _make_ctx("S2", "what's the weather today?")
    await meta_command_launch(normal)
    assert "meta_launch" not in normal.metadata
    assert pending_meta_launch_peek("S2") == "meta-tiny"

    # The genuine launch turn then claims it (and consumes it one-shot).
    launch = _make_ctx("S2", "/meta meta-tiny")
    await meta_command_launch(launch)
    assert launch.metadata["meta_launch"] == {"name": "meta-tiny"}
    assert pending_meta_launch_peek("S2") is None


@pytest.mark.asyncio
async def test_launch_turn_sentinel_tolerates_surrounding_whitespace() -> None:
    pending_meta_launch_put("S3", "meta-tiny")

    ctx = _make_ctx("S3", "  /meta meta-tiny  ")
    await meta_command_launch(ctx)
    assert ctx.metadata["meta_launch"] == {"name": "meta-tiny"}


@pytest.mark.asyncio
async def test_launch_turn_preserves_explicit_request_payload() -> None:
    pending_meta_launch_put("S-request", "meta-tiny")

    ctx = _make_ctx(
        "S-request",
        "/meta meta-tiny -- Write a ten-page paper\nwith cited sources.",
    )
    await meta_command_launch(ctx)

    assert ctx.metadata["meta_launch"] == {
        "name": "meta-tiny",
        "request": "Write a ten-page paper\nwith cited sources.",
    }
    assert pending_meta_launch_peek("S-request") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "/meta meta-other -- this targets another skill",
        "/meta meta-tiny trailing text without a separator",
        "write a paper with meta-tiny",
    ],
)
async def test_request_launch_strictly_matches_skill_and_command_grammar(
    message: str,
) -> None:
    pending_meta_launch_put("S-strict", "meta-tiny")

    ctx = _make_ctx("S-strict", message)
    await meta_command_launch(ctx)

    assert "meta_launch" not in ctx.metadata
    assert pending_meta_launch_peek("S-strict") == "meta-tiny"
    pending_meta_launch_pop("S-strict")


@pytest.mark.asyncio
async def test_meta_command_launch_no_pending_is_noop() -> None:
    # Ensure no residual entry for this session.
    pending_meta_launch_pop("S-empty")

    ctx = _make_ctx("S-empty", "/meta meta-tiny")
    await meta_command_launch(ctx)
    assert "meta_launch" not in ctx.metadata


@pytest.mark.asyncio
async def test_meta_command_launch_no_session_id_is_noop() -> None:
    # Even if a launch were stamped under the empty key, an empty session
    # id must not resolve it (the store ignores empty keys on put).
    pending_meta_launch_put("", "meta-tiny")

    ctx = _make_ctx("", "/meta meta-tiny")
    await meta_command_launch(ctx)
    assert "meta_launch" not in ctx.metadata


def test_pending_store_is_one_shot_and_isolated_per_session() -> None:
    pending_meta_launch_put("A", "meta-a")
    pending_meta_launch_put("B", "meta-b")

    assert pending_meta_launch_pop("A") == "meta-a"
    # Second pop on A returns None (consumed); B is untouched.
    assert pending_meta_launch_pop("A") is None
    assert pending_meta_launch_pop("B") == "meta-b"
    assert pending_meta_launch_pop("B") is None


@pytest.mark.asyncio
async def test_request_bound_launch_replay_cannot_recreate_consumed_marker() -> None:
    session_key = "request-bound-replay"
    request_id = "meta-provider-handoff-1"

    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_id,
        )
        == "stamped"
    )
    assert (
        pending_meta_launch_promote(
            session_key,
            client_request_id=request_id,
            message="/meta meta-short-drama",
        )
        == "promoted"
    )
    launch = _make_ctx(session_key, "/meta meta-short-drama")
    with turn_context_scope({"client_request_id": request_id}):
        await meta_command_launch(launch)
    assert launch.metadata["meta_launch"] == {"name": "meta-short-drama"}
    assert pending_meta_launch_peek(session_key) is None

    # A second tab can finish its provider-settings handoff after the first
    # tab's launch turn has already claimed the marker. The shared request id
    # makes that late meta.run an idempotent replay rather than a new stamp.
    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_id,
        )
        == "replayed"
    )
    assert pending_meta_launch_peek(session_key) is None


@pytest.mark.asyncio
async def test_accepted_request_bound_launch_survives_long_queue_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.steps.meta_command as meta_command

    session_key = "request-bound-long-queue"
    request_id = "meta-provider-handoff-long-queue"
    now = 100.0
    monkeypatch.setattr(meta_command.time, "monotonic", lambda: now)

    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_id,
        )
        == "stamped"
    )
    assert (
        pending_meta_launch_promote(
            session_key,
            client_request_id=request_id,
            message="/meta meta-short-drama",
        )
        == "promoted"
    )

    # Consumed request-id tombstones expire after 15 minutes, but this marker
    # is still live: a successfully accepted turn may remain queued longer.
    now += 15 * 60 + 1
    launch = _make_ctx(session_key, "/meta meta-short-drama")
    with turn_context_scope({"client_request_id": request_id}):
        await meta_command_launch(launch)

    assert launch.metadata["meta_launch"] == {"name": "meta-short-drama"}
    assert pending_meta_launch_peek(
        session_key,
        client_request_id=request_id,
    ) is None


def test_request_bound_launch_rejects_same_id_for_another_skill() -> None:
    session_key = "request-bound-conflict"
    request_id = "meta-provider-handoff-conflict"

    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_id,
        )
        == "stamped"
    )
    assert (
        pending_meta_launch_put(
            session_key,
            "meta-paper-write",
            client_request_id=request_id,
        )
        == "conflict"
    )
    assert pending_meta_launch_peek(session_key) == "meta-short-drama"
    pending_meta_launch_pop(session_key)


@pytest.mark.asyncio
async def test_different_request_ids_claim_same_skill_launches_independently() -> None:
    session_key = "request-bound-concurrent"
    request_a = "meta-provider-handoff-A"
    request_b = "meta-provider-handoff-B"
    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_a,
        )
        == "stamped"
    )
    assert (
        pending_meta_launch_promote(
            session_key,
            client_request_id=request_a,
            message="/meta meta-short-drama -- request A",
        )
        == "promoted"
    )
    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_b,
        )
        == "stamped"
    )
    assert (
        pending_meta_launch_promote(
            session_key,
            client_request_id=request_b,
            message="/meta meta-short-drama -- request B",
        )
        == "promoted"
    )

    # Arrival order does not swap the two bindings.
    turn_b = _make_ctx(session_key, "/meta meta-short-drama -- request B")
    with turn_context_scope({"client_request_id": request_b}):
        await meta_command_launch(turn_b)
    turn_a = _make_ctx(session_key, "/meta meta-short-drama -- request A")
    with turn_context_scope({"client_request_id": request_a}):
        await meta_command_launch(turn_a)

    assert turn_b.metadata["meta_launch"] == {
        "name": "meta-short-drama",
        "request": "request B",
    }
    assert turn_a.metadata["meta_launch"] == {
        "name": "meta-short-drama",
        "request": "request A",
    }
    assert pending_meta_launch_peek(
        session_key,
        client_request_id=request_a,
    ) is None
    assert pending_meta_launch_peek(
        session_key,
        client_request_id=request_b,
    ) is None


def test_pending_request_capacity_never_evicts_an_accepted_marker() -> None:
    session_key = "request-bound-capacity"
    accepted_request_ids: list[str] = []
    rejected_request_id = ""
    for index in range(2048):
        request_id = f"capacity-{index}"
        disposition = pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_id,
        )
        if disposition == "capacity":
            rejected_request_id = request_id
            break
        assert disposition == "stamped"
        accepted_request_ids.append(request_id)

    assert rejected_request_id
    assert accepted_request_ids
    assert (
        pending_meta_launch_promote(
            session_key,
            client_request_id=accepted_request_ids[0],
            message="/meta meta-short-drama",
        )
        == "promoted"
    )
    assert pending_meta_launch_state(
        session_key,
        client_request_id=accepted_request_ids[0],
    ) == "accepted"
    assert pending_meta_launch_peek(
        session_key,
        client_request_id=accepted_request_ids[0],
    ) == "meta-short-drama"
    assert pending_meta_launch_peek(
        session_key,
        client_request_id=rejected_request_id,
    ) is None

    for request_id in accepted_request_ids:
        pending_meta_launch_pop(session_key, client_request_id=request_id)


def test_abandoned_staged_launches_expire_and_free_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.steps.meta_command as meta_command

    session_key = "request-bound-abandoned-capacity"
    now = 100.0
    monkeypatch.setattr(meta_command.time, "monotonic", lambda: now)
    accepted_request_ids: list[str] = []
    rejected_request_id = ""
    for index in range(2048):
        request_id = f"abandoned-{index}"
        disposition = pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=request_id,
        )
        if disposition == "capacity":
            rejected_request_id = request_id
            break
        assert disposition == "stamped"
        accepted_request_ids.append(request_id)

    assert rejected_request_id
    now += 15 * 60 + 1
    assert (
        pending_meta_launch_put(
            session_key,
            "meta-short-drama",
            client_request_id=rejected_request_id,
        )
        == "stamped"
    )
    assert all(
        pending_meta_launch_state(session_key, client_request_id=request_id) is None
        for request_id in accepted_request_ids
    )
    pending_meta_launch_pop(
        session_key,
        client_request_id=rejected_request_id,
    )


def test_request_bound_consumed_tombstones_expire_and_stay_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.steps.meta_command as meta_command

    session_key = "request-bound-retention"
    now = 100.0
    monkeypatch.setattr(meta_command.time, "monotonic", lambda: now)

    for index in range(1030):
        assert (
            pending_meta_launch_put(
                session_key,
                "meta-short-drama",
                client_request_id=f"bounded-{index}",
            )
            == "stamped"
        )
        assert pending_meta_launch_pop(session_key) == "meta-short-drama"

    assert pending_meta_launch_consumed_count(session_key) == 1024

    now += 15 * 60 + 1
    assert pending_meta_launch_consumed_count(session_key) == 0


@pytest.mark.asyncio
async def test_replay_sentinel_claims_bound_payload_once() -> None:
    nonce = pending_meta_replay_put(
        "replay-A",
        run_id="run-1",
        name="meta-paper-write",
        mode="failed-step",
    )

    sentinel = format_meta_replay_sentinel(nonce)
    first = _make_ctx("replay-A", sentinel)
    await meta_command_launch(first)

    assert first.metadata["meta_replay"] == {
        "run_id": "run-1",
        "name": "meta-paper-write",
        "mode": "failed-step",
    }
    assert pending_meta_replay_count("replay-A") == 0

    reused = _make_ctx("replay-A", sentinel)
    await meta_command_launch(reused)
    assert "meta_replay" not in reused.metadata
    assert "already used" in reused.metadata["meta_replay_error"]


@pytest.mark.asyncio
async def test_replay_sentinel_rejects_cross_session_without_consuming_owner_entry() -> None:
    nonce = pending_meta_replay_put(
        "replay-owner",
        run_id="run-owner",
        name="meta-short-drama",
        mode="partial-context",
    )

    sentinel = format_meta_replay_sentinel(nonce)
    forged = _make_ctx("replay-other", sentinel)
    await meta_command_launch(forged)
    assert "meta_replay" not in forged.metadata
    assert "meta_replay_error" in forged.metadata
    assert pending_meta_replay_count("replay-owner") == 1

    owner = _make_ctx("replay-owner", sentinel)
    await meta_command_launch(owner)
    assert owner.metadata["meta_replay"]["run_id"] == "run-owner"


@pytest.mark.asyncio
async def test_expired_replay_sentinel_never_falls_through_to_model() -> None:
    nonce = pending_meta_replay_put(
        "replay-expired",
        run_id="run-expired",
        name="meta-paper-write",
        mode="failed-step",
        ttl_seconds=0,
    )

    ctx = _make_ctx("replay-expired", format_meta_replay_sentinel(nonce))
    await meta_command_launch(ctx)

    assert "meta_replay" not in ctx.metadata
    assert "expired" in ctx.metadata["meta_replay_error"]
    assert pending_meta_replay_pop("replay-expired", nonce) is None


@pytest.mark.asyncio
async def test_replay_sentinels_keep_bindings_when_same_session_turns_arrive_in_reverse() -> None:
    nonce_a = pending_meta_replay_put(
        "replay-reverse",
        run_id="run-A",
        name="meta-paper-write",
        mode="failed-step",
    )
    nonce_b = pending_meta_replay_put(
        "replay-reverse",
        run_id="run-B",
        name="meta-short-drama",
        mode="partial-context",
    )

    second = _make_ctx("replay-reverse", format_meta_replay_sentinel(nonce_b))
    await meta_command_launch(second)
    first = _make_ctx("replay-reverse", format_meta_replay_sentinel(nonce_a))
    await meta_command_launch(first)

    assert second.metadata["meta_replay"] == {
        "run_id": "run-B",
        "name": "meta-short-drama",
        "mode": "partial-context",
    }
    assert first.metadata["meta_replay"] == {
        "run_id": "run-A",
        "name": "meta-paper-write",
        "mode": "failed-step",
    }
    assert pending_meta_replay_count("replay-reverse") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forged_sentinel",
    [
        "/meta-replay",
        "/meta-replay not-a-valid-nonce",
        f"/meta-replay {'0' * 32}",
    ],
)
async def test_typed_or_forged_replay_sentinel_cannot_consume_pending_entry(
    forged_sentinel: str,
) -> None:
    nonce = pending_meta_replay_put(
        "replay-typed",
        run_id="run-real",
        name="meta-paper-write",
        mode="failed-step",
    )

    forged = _make_ctx("replay-typed", forged_sentinel)
    await meta_command_launch(forged)

    assert "meta_replay" not in forged.metadata
    assert "invalid" in forged.metadata["meta_replay_error"]
    assert pending_meta_replay_count("replay-typed") == 1

    owner = _make_ctx("replay-typed", format_meta_replay_sentinel(nonce))
    await meta_command_launch(owner)
    assert owner.metadata["meta_replay"]["run_id"] == "run-real"
