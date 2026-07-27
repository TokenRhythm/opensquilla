"""Channel-context behavior of ``request_sandbox_approval``.

A plain channel caller must never open a sandbox approval (hard deny, ask an
admin instead); a channel-admin turn opens one that is routed back to the
originating chat via the ``senderId``/``sessionKey`` params the notifier keys
on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.elevation import ElevationAction, gate_elevated_action
from opensquilla.sandbox.escalation import request_sandbox_approval
from opensquilla.sandbox.governance import ALLOW, ApprovalGate
from opensquilla.sandbox.types import (
    DenialReason,
    DenialResult,
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def _reset_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


def _channel_context(
    *,
    is_owner: bool,
    sender_id: str | None = "ou_admin",
    channel_admin_verified: bool | None = None,
) -> ToolContext:
    return ToolContext(
        is_owner=is_owner,
        channel_admin_verified=(
            is_owner if channel_admin_verified is None else channel_admin_verified
        ),
        caller_kind=CallerKind.CHANNEL,
        session_key="feishu:oc_demo",
        channel_kind="feishu",
        channel_id="oc_demo",
        sender_id=sender_id,
        source_name="feishu-main",
    )


def _params() -> dict[str, object]:
    return {
        "approvalKind": "sandbox_network",
        "host": "pypi.org",
        "fingerprint": "fp-test",
        "choices": [
            {"id": "allow_once", "label": "Allow once", "approved": True},
            {"id": "allow_same_type", "label": "Allow same type", "approved": True},
            {"id": "deny", "label": "Deny", "approved": False},
        ],
    }


def _elevation_action() -> ElevationAction:
    return ElevationAction(
        tool_name="exec_command",
        action_kind="shell.exec",
        argv=("sh", "-lc", "touch /tmp/channel-probe"),
        cwd="/workspace",
        sandbox_permissions="require_escalated",
        justification="Create the exact probe requested by the user.",
    )


def _approval_required_request() -> SandboxRequest:
    workspace = Path("/workspace")
    policy = SandboxPolicy(
        level=SecurityLevel.LOCKED,
        network=NetworkMode.NONE,
        mounts=(MountSpec(host_path=workspace, sandbox_path=workspace, mode="rw"),),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=True,
    )
    return SandboxRequest(
        argv=("sh", "-lc", "rm -rf generated-output"),
        cwd=workspace,
        action_kind="shell.exec",
        policy=policy,
        run_mode="standard",
    )


def _with_context(ctx: ToolContext | None, fn):
    token = current_tool_context.set(ctx)
    try:
        return fn()
    finally:
        current_tool_context.reset(token)


def test_non_admin_channel_caller_is_denied_without_a_request() -> None:
    payload = _with_context(
        _channel_context(is_owner=False),
        lambda: request_sandbox_approval(_params(), message="ask"),
    )

    assert payload["status"] == "approval_denied"
    assert payload["approval_id"] == ""
    assert "admin" in str(payload["message"])
    assert get_approval_queue().list_pending() == []


def test_admin_channel_caller_opens_a_channel_routed_approval() -> None:
    payload = _with_context(
        _channel_context(is_owner=True),
        lambda: request_sandbox_approval(_params(), message="ask"),
    )

    assert payload["status"] == "approval_required"
    approval_id = str(payload["approval_id"])
    entry = get_approval_queue().get(approval_id)
    # The stamps the channel notifier keys on: without them the prompt would
    # never reach the chat and the approval could not be resolved there.
    assert entry.params["senderId"] == "ou_admin"
    assert entry.params["sessionKey"] == "feishu:oc_demo"


def test_trusted_verified_channel_admin_routes_elevation_approval() -> None:
    ctx = _channel_context(is_owner=True)
    ctx.run_mode = "trusted"

    def _request() -> tuple[object, object]:
        queue = get_approval_queue()
        result = gate_elevated_action(
            _elevation_action(),
            approval_id=None,
            session_key=ctx.session_key,
            queue=queue,
            reviewer="user",
        )
        return result, queue

    result, queue = _with_context(ctx, _request)

    assert result.status == "approval_required"
    entry = queue.get(result.approval_id or "")
    assert entry.params["approvalKind"] == "sandbox_elevation"
    assert entry.params["senderId"] == "ou_admin"
    assert entry.params["sessionKey"] == "feishu:oc_demo"


def test_channel_elevation_approval_is_bound_to_the_originating_sender() -> None:
    ctx = _channel_context(is_owner=True)

    pending = _with_context(
        ctx,
        lambda: gate_elevated_action(
            _elevation_action(),
            approval_id=None,
            session_key=ctx.session_key,
            queue=get_approval_queue(),
            reviewer="user",
        ),
    )
    get_approval_queue().resolve(pending.approval_id or "", True)

    other_admin = _channel_context(is_owner=True, sender_id="ou_other_admin")
    result = _with_context(
        other_admin,
        lambda: gate_elevated_action(
            _elevation_action(),
            approval_id=pending.approval_id,
            session_key=other_admin.session_key,
            queue=get_approval_queue(),
            reviewer="user",
        ),
    )

    assert result.status == "approval_sender_mismatch"
    assert get_approval_queue().get(pending.approval_id or "").consumed is False


def test_unverified_channel_owner_elevation_fails_closed_before_queueing() -> None:
    ctx = _channel_context(is_owner=True, channel_admin_verified=False)

    result = _with_context(
        ctx,
        lambda: gate_elevated_action(
            _elevation_action(),
            approval_id=None,
            session_key=ctx.session_key,
            queue=get_approval_queue(),
            reviewer="user",
        ),
    )

    assert result.status == "approval_denied"
    assert result.requested is False
    assert get_approval_queue().list_pending() == []


def test_channel_elevation_requires_the_authenticated_session_binding() -> None:
    ctx = _channel_context(is_owner=True)

    result = _with_context(
        ctx,
        lambda: gate_elevated_action(
            _elevation_action(),
            approval_id=None,
            session_key="feishu:oc_other",
            queue=get_approval_queue(),
            reviewer="user",
        ),
    )

    assert result.status == "approval_denied"
    assert result.requested is False
    assert get_approval_queue().list_pending() == []


@pytest.mark.asyncio
async def test_standard_channel_admin_governance_approval_is_routed_to_sender() -> None:
    class _ApprovedQueue:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def request(self, namespace: str = "exec", params: dict | None = None) -> str:
            assert namespace == "exec"
            self.requests.append(dict(params or {}))
            return "approval-1"

        async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
            assert approval_id == "approval-1"
            return True

        def resolve(self, approval_id: str, approved: bool) -> None:  # pragma: no cover
            raise AssertionError("not used")

    ctx = _channel_context(is_owner=True)
    ctx.run_mode = "standard"
    request = _approval_required_request()
    queue = _ApprovedQueue()
    token = current_tool_context.set(ctx)
    try:
        decision = await ApprovalGate(queue).gate(
            request,
            request.policy,
            session_id=ctx.session_key or "",
        )
    finally:
        current_tool_context.reset(token)

    assert decision is ALLOW
    assert len(queue.requests) == 1
    params = queue.requests[0]
    assert params["action_kind"] == "shell.exec"
    assert params["session_id"] == "feishu:oc_demo"
    assert params["senderId"] == "ou_admin"
    assert params["sessionKey"] == "feishu:oc_demo"
    assert isinstance(params["fingerprint"], str)


@pytest.mark.asyncio
async def test_standard_channel_governance_approval_fails_closed_without_verified_route() -> None:
    ctx = _channel_context(is_owner=True, channel_admin_verified=False)
    ctx.run_mode = "standard"
    request = _approval_required_request()

    class _NeverQueued:
        def request(self, namespace: str = "exec", params: dict | None = None) -> str:
            raise AssertionError("unroutable channel approval must not queue")

        async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
            raise AssertionError("unroutable channel approval must not wait")

        def resolve(self, approval_id: str, approved: bool) -> None:  # pragma: no cover
            raise AssertionError("not used")

    token = current_tool_context.set(ctx)
    try:
        decision = await ApprovalGate(_NeverQueued()).gate(
            request,
            request.policy,
            session_id=ctx.session_key or "",
        )
    finally:
        current_tool_context.reset(token)

    assert isinstance(decision, DenialResult)
    assert decision.reason is DenialReason.POLICY_DENIED


def test_admin_without_sender_identity_stays_denied() -> None:
    # is_owner alone is not enough: without a sender id the resolver could
    # never match the approval back to a person, so the deny stands.
    payload = _with_context(
        _channel_context(is_owner=True, sender_id=None),
        lambda: request_sandbox_approval(_params(), message="ask"),
    )

    assert payload["status"] == "approval_denied"
    assert get_approval_queue().list_pending() == []


def test_non_channel_context_is_not_stamped() -> None:
    payload = _with_context(
        None,
        lambda: request_sandbox_approval(_params(), message="ask"),
    )

    assert payload["status"] == "approval_required"
    entry = get_approval_queue().get(str(payload["approval_id"]))
    assert "senderId" not in entry.params
