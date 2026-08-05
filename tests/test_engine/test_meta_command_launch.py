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
    meta_command_launch,
    pending_meta_launch_peek,
    pending_meta_launch_pop,
    pending_meta_launch_put,
)


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
async def test_launch_turn_accepts_a_concrete_request_after_the_skill_name() -> None:
    pending_meta_launch_put("S-request", "meta-tiny")

    ctx = _make_ctx(
        "S-request",
        "/meta meta-tiny create a competitor research meta-skill",
    )
    await meta_command_launch(ctx)

    assert ctx.metadata["meta_launch"] == {"name": "meta-tiny"}
    assert pending_meta_launch_peek("S-request") is None


@pytest.mark.asyncio
async def test_launch_turn_rejects_a_skill_name_prefix_collision() -> None:
    pending_meta_launch_put("S-prefix", "meta-tiny")

    ctx = _make_ctx("S-prefix", "/meta meta-tiny-extra create something")
    await meta_command_launch(ctx)

    assert "meta_launch" not in ctx.metadata
    assert pending_meta_launch_peek("S-prefix") == "meta-tiny"
    pending_meta_launch_pop("S-prefix")


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
