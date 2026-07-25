"""Owner-only resolution of channel approval actions in dispatch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.channels.approval_prompt import bind_short_code, reset_short_codes
from opensquilla.channels.types import (
    AuthenticatedPrincipal,
    IncomingMessage,
    IngressProvenance,
    IngressVerification,
)
from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.gateway.channel_dispatch import (
    _maybe_resolve_channel_approval,
    _reset_approval_probe_throttle,
    _stamp_channel_admin_principal,
)
from opensquilla.gateway.routing import build_channel_route_envelope


@pytest.fixture(autouse=True)
def _reset_state():
    reset_approval_queue()
    reset_short_codes()
    _reset_approval_probe_throttle()
    yield
    reset_approval_queue()
    reset_short_codes()
    _reset_approval_probe_throttle()


def _pending_approval(
    owner_sender_id: str,
    *,
    origin_channel_name: str = "",
    origin_channel_id: str = "",
    session_key: str = "agent:main:chat",
    params: dict | None = None,
) -> tuple[str, str]:
    queue = get_approval_queue()
    approval_id = queue.request(
        namespace="exec",
        params=params
        or {
            "toolName": "exec_command",
            "command": "rm target.txt",
            "sessionKey": session_key,
            "senderId": owner_sender_id,
            "channelKind": "feishu",
        },
    )
    code = bind_short_code(
        approval_id,
        namespace="exec",
        session_key=session_key,
        owner_sender_id=owner_sender_id,
        origin_channel_name=origin_channel_name,
        origin_channel_id=origin_channel_id,
    )
    return approval_id, code


def test_non_action_message_is_ignored() -> None:
    msg = IncomingMessage(sender_id="owner", channel_id="c1", content="hello there")
    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )
    assert reply is None


def test_unknown_code_returns_no_pending() -> None:
    msg = IncomingMessage(sender_id="owner", channel_id="c1", content="/approve ZZZZ")
    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )
    assert reply is not None
    assert "No pending approval ZZZZ" in reply.content


def test_non_owner_cannot_resolve() -> None:
    approval_id, code = _pending_approval(owner_sender_id="owner-1")
    msg = IncomingMessage(sender_id="intruder-2", channel_id="c1", content=f"/approve {code}")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )

    assert reply is not None
    assert "Only the session owner" in reply.content
    # The request must still be unresolved — the non-owner attempt did not flip it.
    assert get_approval_queue().get(approval_id).resolved is False


def test_owner_approve_resolves_and_forces_no_elevation() -> None:
    approval_id, code = _pending_approval(owner_sender_id="owner-1")
    queue = get_approval_queue()
    # A waiter blocked on the approval (mirrors the suspended tool call).
    waited: list[bool] = []

    async def _run() -> None:
        async def _waiter() -> None:
            waited.append(await queue.wait(approval_id, timeout=5.0))

        waiter_task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        msg = IncomingMessage(
            sender_id="owner-1", channel_id="c1", content=f"/approve {code}"
        )
        reply = await _maybe_resolve_channel_approval(
            msg=msg, session_key="agent:main:chat"
        )
        assert reply is not None
        assert f"Approved {code}" in reply.content
        await asyncio.wait_for(waiter_task, timeout=5.0)

    asyncio.run(_run())

    assert waited == [True]
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True
    # Channel approval never grants session-wide elevation.
    assert queue.get_elevated_mode("agent:main:chat") is None
    assert "elevatedMode" not in entry.params


def test_owner_deny_resolves_to_not_approved() -> None:
    approval_id, code = _pending_approval(owner_sender_id="owner-1")
    msg = IncomingMessage(sender_id="owner-1", channel_id="c1", content=f"/deny {code}")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )

    assert reply is not None
    assert f"Denied {code}" in reply.content
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False


def _admin_config(channel_name: str, sender_id: str) -> SimpleNamespace:
    return SimpleNamespace(channel_admin_senders={channel_name: [sender_id]})


def _authenticated_admin_route(
    msg: IncomingMessage,
    *,
    channel_name: str,
) -> tuple[SimpleNamespace, object]:
    config = _admin_config(channel_name, msg.sender_id)
    envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:chat",
        session_prefix=channel_name,
    )
    assert _stamp_channel_admin_principal(config, envelope, msg) is True
    return config, envelope


def test_always_requires_channel_admin() -> None:
    approval_id, code = _pending_approval(
        owner_sender_id="owner-1", origin_channel_name="feishu-main"
    )
    msg = IncomingMessage(
        sender_id="owner-1", channel_id="c1", content=f"/approve {code} always"
    )

    # Owner of the turn but NOT a configured channel admin: rejected, and the
    # approval stays pending so a plain /approve still works afterwards.
    reply = asyncio.run(
        _maybe_resolve_channel_approval(
            msg=msg,
            session_key="agent:main:chat",
            config=SimpleNamespace(channel_admin_senders={}),
        )
    )
    assert reply is not None
    assert "needs a channel admin" in reply.content
    assert get_approval_queue().get(approval_id).resolved is False


def test_always_rejects_unverified_configured_sender() -> None:
    approval_id, code = _pending_approval(
        owner_sender_id="owner-1", origin_channel_name="feishu-main"
    )
    msg = IncomingMessage(
        sender_id="owner-1",
        channel_id="c1",
        content=f"/approve {code} always",
        metadata={"principal_is_owner": True, "channel_admin_verified": True},
    )
    config = _admin_config("feishu-main", "owner-1")
    route_envelope = build_channel_route_envelope(
        msg,
        session_key="agent:main:chat",
        session_prefix="feishu-main",
    )

    assert _stamp_channel_admin_principal(config, route_envelope, msg) is False
    reply = asyncio.run(
        _maybe_resolve_channel_approval(
            msg=msg,
            session_key="agent:main:chat",
            config=config,
            route_envelope=route_envelope,
        )
    )

    assert reply is not None
    assert "needs a channel admin" in reply.content
    assert get_approval_queue().get(approval_id).resolved is False


def test_always_from_admin_resolves_plain_exec_approval() -> None:
    # Non-sandbox (plain exec) approvals accept "always" as a plain approve —
    # there is no durable-grant choice to apply.
    approval_id, code = _pending_approval(
        owner_sender_id="owner-1", origin_channel_name="feishu-main"
    )
    msg = IncomingMessage(
        sender_id="owner-1",
        channel_id="c1",
        content=f"/approve {code} always",
        provenance=IngressProvenance(
            provider="feishu",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id="owner-1"),
        ),
    )
    config, route_envelope = _authenticated_admin_route(msg, channel_name="feishu-main")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(
            msg=msg,
            session_key="agent:main:chat",
            config=config,
            route_envelope=route_envelope,
        )
    )

    assert reply is not None
    assert f"Approved {code}" in reply.content
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True


def test_always_from_admin_applies_sandbox_same_type_choice(monkeypatch) -> None:
    applied: list[dict] = []

    async def _fake_apply(params, *, choice, approved, session_manager, config):
        applied.append({"params": params, "choice": choice, "approved": approved})

    monkeypatch.setattr(
        "opensquilla.sandbox.escalation.apply_sandbox_approval_choice", _fake_apply
    )

    approval_id, code = _pending_approval(
        owner_sender_id="owner-1",
        origin_channel_name="feishu-main",
        params={
            "approvalKind": "sandbox_network",
            "host": "pypi.org",
            "fingerprint": "fp-1",
            "sessionKey": "agent:main:chat",
            "senderId": "owner-1",
            "choices": [
                {"id": "allow_once", "label": "Allow once", "approved": True},
                {"id": "allow_same_type", "label": "Allow same type", "approved": True},
                {"id": "deny", "label": "Deny", "approved": False},
            ],
        },
    )
    msg = IncomingMessage(
        sender_id="owner-1",
        channel_id="c1",
        content=f"/approve {code} always",
        provenance=IngressProvenance(
            provider="feishu",
            verification=IngressVerification.SDK_SESSION,
            principal=AuthenticatedPrincipal(subject_id="owner-1"),
        ),
    )
    config, route_envelope = _authenticated_admin_route(msg, channel_name="feishu-main")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(
            msg=msg,
            session_key="agent:main:chat",
            config=config,
            route_envelope=route_envelope,
        )
    )

    assert reply is not None
    assert "won't ask again" in reply.content
    assert applied == [
        {
            "params": get_approval_queue().get(approval_id).params,
            "choice": "allow_same_type",
            "approved": True,
        }
    ]
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True


def test_sandbox_deny_remembers_and_fans_out(monkeypatch) -> None:
    remembered: list[str] = []
    monkeypatch.setattr(
        "opensquilla.sandbox.escalation.remember_sandbox_approval_denial",
        lambda params, approval_id: remembered.append(approval_id),
    )

    approval_id, code = _pending_approval(
        owner_sender_id="owner-1",
        origin_channel_name="feishu-main",
        params={
            "approvalKind": "sandbox_network",
            "host": "pypi.org",
            "fingerprint": "fp-1",
            "sessionKey": "agent:main:chat",
            "senderId": "owner-1",
        },
    )
    msg = IncomingMessage(sender_id="owner-1", channel_id="c1", content=f"/deny {code}")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )

    assert reply is not None
    assert f"Denied {code}" in reply.content
    assert remembered == [approval_id]
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False


_SANDBOX_PARAMS = {
    "approvalKind": "sandbox_network",
    "host": "pypi.org",
    "fingerprint": "fp-1",
    "sessionKey": "agent:main:chat",
    "senderId": "owner-1",
    "choices": [
        {"id": "allow_once", "label": "Allow once", "approved": True},
        {"id": "allow_same_type", "label": "Allow same type", "approved": True},
        {"id": "deny", "label": "Deny", "approved": False},
    ],
}


def test_plain_approve_applies_sandbox_allow_once(monkeypatch) -> None:
    # Sandbox kinds refuse empty choices — a plain Approve must select the
    # primary allow_once choice, resolve the entry, and unblock the waiter
    # (instead of failing validation and reporting "was already resolved").
    applied: list[dict] = []

    async def _fake_apply(params, *, choice, approved, session_manager, config):
        applied.append({"choice": choice, "approved": approved})

    monkeypatch.setattr(
        "opensquilla.sandbox.escalation.apply_sandbox_approval_choice", _fake_apply
    )

    approval_id, code = _pending_approval(
        owner_sender_id="owner-1",
        origin_channel_name="feishu-main",
        params=dict(_SANDBOX_PARAMS),
    )
    queue = get_approval_queue()
    waited: list[bool] = []

    async def _run() -> None:
        async def _waiter() -> None:
            waited.append(await queue.wait(approval_id, timeout=5.0))

        waiter_task = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        msg = IncomingMessage(
            sender_id="owner-1", channel_id="c1", content=f"/approve {code}"
        )
        reply = await _maybe_resolve_channel_approval(
            msg=msg, session_key="agent:main:chat"
        )
        assert reply is not None
        assert f"Approved {code}" in reply.content
        assert "already resolved" not in reply.content
        await asyncio.wait_for(waiter_task, timeout=5.0)

    asyncio.run(_run())

    assert waited == [True]
    assert applied == [{"choice": "allow_once", "approved": True}]
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True


def test_sandbox_apply_failure_reopens_and_replies(monkeypatch) -> None:
    # A transient storage failure while applying the grant must produce a
    # reply (not an exception that burns the channel's restart budget) and
    # leave the approval pending for a retry.
    from opensquilla.session.storage import StorageBusyError

    async def _busy_apply(params, *, choice, approved, session_manager, config):
        raise StorageBusyError("database is locked")

    monkeypatch.setattr(
        "opensquilla.sandbox.escalation.apply_sandbox_approval_choice", _busy_apply
    )

    approval_id, code = _pending_approval(
        owner_sender_id="owner-1",
        origin_channel_name="feishu-main",
        params=dict(_SANDBOX_PARAMS),
    )
    msg = IncomingMessage(sender_id="owner-1", channel_id="c1", content=f"/approve {code}")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )

    assert reply is not None
    assert "still pending" in reply.content
    entry = get_approval_queue().get(approval_id)
    assert entry.resolved is False


def test_session_mismatch_gets_generic_reply() -> None:
    # A live code probed from another session must be indistinguishable from
    # an unknown code — response text is not an existence oracle.
    approval_id, code = _pending_approval(owner_sender_id="owner-1")
    msg = IncomingMessage(sender_id="owner-1", channel_id="c1", content=f"/approve {code}")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:other")
    )

    assert reply is not None
    assert f"No pending approval {code}" in reply.content
    assert "session" not in reply.content
    assert get_approval_queue().get(approval_id).resolved is False


def test_origin_mismatch_gets_generic_reply() -> None:
    approval_id, code = _pending_approval(
        owner_sender_id="owner-1", origin_channel_id="c-origin"
    )
    msg = IncomingMessage(
        sender_id="owner-1", channel_id="c-other", content=f"/approve {code}"
    )

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )

    assert reply is not None
    assert f"No pending approval {code}" in reply.content
    assert "where it was requested" not in reply.content
    assert get_approval_queue().get(approval_id).resolved is False


def test_repeated_failed_probes_hit_cooldown() -> None:
    # Five misses inside the window exhaust the probe budget; the next
    # attempt gets a constant cooldown reply without touching the queue.
    async def _run() -> None:
        for idx in range(5):
            msg = IncomingMessage(
                sender_id="prober", channel_id="c1", content=f"/approve ZZZ{idx}"
            )
            reply = await _maybe_resolve_channel_approval(
                msg=msg, session_key="agent:main:chat"
            )
            assert reply is not None
            assert "No pending approval" in reply.content
        msg = IncomingMessage(
            sender_id="prober", channel_id="c1", content="/approve ZZZ9"
        )
        reply = await _maybe_resolve_channel_approval(
            msg=msg, session_key="agent:main:chat"
        )
        assert reply is not None
        assert "Too many failed approval attempts" in reply.content

    asyncio.run(_run())


def test_sandbox_deny_fans_out_to_duplicate_pending_asks(monkeypatch) -> None:
    # Two pending asks with the same sandbox fingerprint: denying one from
    # chat must resolve the duplicate too, and both denials are remembered.
    remembered: list[str] = []
    monkeypatch.setattr(
        "opensquilla.sandbox.escalation.remember_sandbox_approval_denial",
        lambda params, approval_id: remembered.append(approval_id),
    )

    params = {
        "approvalKind": "sandbox_network",
        "host": "pypi.org",
        "fingerprint": "fp-1",
        "sessionKey": "agent:main:chat",
        "senderId": "owner-1",
    }
    approval_id, code = _pending_approval(
        owner_sender_id="owner-1",
        origin_channel_name="feishu-main",
        params=dict(params),
    )
    queue = get_approval_queue()
    duplicate_id = queue.request(namespace="exec", params=dict(params))
    msg = IncomingMessage(sender_id="owner-1", channel_id="c1", content=f"/deny {code}")

    reply = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )

    assert reply is not None
    assert f"Denied {code}" in reply.content
    duplicate = queue.get(duplicate_id)
    assert duplicate.resolved is True
    assert duplicate.approved is False
    assert set(remembered) == {approval_id, duplicate_id}


def test_finalize_failure_releases_claim_so_a_retry_succeeds(monkeypatch) -> None:
    # A failed finalize must release the resolution claim; otherwise the
    # approval is stuck claimed-but-unresolved and no retry can ever land.
    async def _fake_apply(params, *, choice, approved, session_manager, config):
        return None

    monkeypatch.setattr(
        "opensquilla.sandbox.escalation.apply_sandbox_approval_choice", _fake_apply
    )

    approval_id, code = _pending_approval(
        owner_sender_id="owner-1",
        origin_channel_name="feishu-main",
        params=dict(_SANDBOX_PARAMS),
    )
    queue = get_approval_queue()
    real_finalize = queue.finalize_claimed_resolution
    calls = {"count": 0}

    def _flaky_finalize(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("storage hiccup")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(queue, "finalize_claimed_resolution", _flaky_finalize)
    msg = IncomingMessage(sender_id="owner-1", channel_id="c1", content=f"/approve {code}")

    first = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )
    assert first is not None
    assert "still pending" in first.content
    assert queue.get(approval_id).resolved is False

    second = asyncio.run(
        _maybe_resolve_channel_approval(msg=msg, session_key="agent:main:chat")
    )
    assert second is not None
    assert f"Approved {code}" in second.content
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True
