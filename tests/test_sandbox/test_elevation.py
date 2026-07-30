from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.elevation import (
    ElevationAction,
    consume_approved_elevation,
    gate_elevated_action,
    request_elevation,
)
from opensquilla.tools.types import ToolContext, current_tool_context


def _shell_action(command: str, *, cwd: str = "/workspace/opensquilla") -> ElevationAction:
    return ElevationAction(
        tool_name="exec_command",
        action_kind="shell.exec",
        argv=("sh", "-lc", command),
        cwd=cwd,
        sandbox_permissions="require_escalated",
        justification="Perform the exact operation requested by the user.",
        target_paths=(("/mnt/desktop/probe", "write"),),
    )


def test_elevation_action_fingerprint_binds_side_effect_fields() -> None:
    action = _shell_action("touch /mnt/desktop/probe")

    assert action.fingerprint() == action.fingerprint()
    assert replace(action, cwd="/tmp").fingerprint() != action.fingerprint()
    assert (
        replace(action, argv=("sh", "-lc", "rm /mnt/desktop/probe")).fingerprint()
        != action.fingerprint()
    )
    assert (
        replace(action, target_paths=(("/mnt/desktop/other", "write"),)).fingerprint()
        != action.fingerprint()
    )


def test_elevation_action_round_trips_canonical_payload() -> None:
    action = replace(
        _shell_action("touch /mnt/desktop/probe"),
        network_targets=("example.com",),
        content_digest="sha256:abc",
        tty=True,
        prefix_rule=("touch",),
    )

    restored = ElevationAction.from_canonical_payload(action.canonical_payload())

    assert restored == action
    assert restored.fingerprint() == action.fingerprint()


def test_request_elevation_is_non_human_actionable_for_auto_review(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("touch /mnt/desktop/probe")
    try:
        pending = request_elevation(
            queue,
            action,
            session_key="session-1",
            reviewer="auto_review",
        )

        entry = queue.get(pending.approval_id or "")
        assert pending.status == "approval_required"
        assert entry.params["approvalKind"] == "sandbox_elevation"
        assert entry.params["reviewer"] == "auto_review"
        assert entry.params["humanActionable"] is False
        assert entry.params["fingerprint"] == action.fingerprint()
        assert "touch /mnt/desktop/probe" in entry.params["action"]["argv"]
    finally:
        queue.close()


def test_duplicate_pending_elevation_reuses_approval(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("touch /mnt/desktop/probe")
    try:
        first = request_elevation(queue, action, session_key="session-1")
        second = request_elevation(queue, action, session_key="session-1")

        assert second.status == "approval_pending"
        assert second.approval_id == first.approval_id
        assert len(queue.list_pending("exec")) == 1
    finally:
        queue.close()


def test_create_and_delete_have_independent_consumed_one_shot_audit_sequences(
    tmp_path: Path,
) -> None:
    """Complement real shell/direct execution tests with the delete grant lifecycle."""
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    target = tmp_path / "outside" / "probe.txt"
    create = replace(
        _shell_action(f"mkdir -p {target.parent} && printf test > {target}"),
        target_paths=((str(target.parent), "write"), (str(target), "write")),
    )
    delete = replace(
        _shell_action(f"rm {target} && rmdir {target.parent}"),
        target_paths=((str(target), "write"), (str(target.parent), "write")),
    )
    try:
        create_pending = request_elevation(queue, create, session_key="session-1")
        delete_pending = request_elevation(queue, delete, session_key="session-1")

        assert create_pending.status == "approval_required"
        assert delete_pending.status == "approval_required"
        assert create.fingerprint() != delete.fingerprint()
        assert create_pending.approval_id != delete_pending.approval_id
        assert len(queue.list_pending("exec")) == 2

        create_id = create_pending.approval_id or ""
        delete_id = delete_pending.approval_id or ""
        assert queue.get(create_id).params["fingerprint"] == create.fingerprint()
        assert queue.get(delete_id).params["fingerprint"] == delete.fingerprint()
        queue.resolve(create_id, True)

        cross_action = consume_approved_elevation(queue, create_id, delete)
        changed_action = consume_approved_elevation(
            queue,
            create_id,
            replace(create, argv=("sh", "-lc", f"printf changed > {target}")),
        )

        assert cross_action.status == "approval_action_mismatch"
        assert changed_action.status == "approval_action_mismatch"
        assert queue.get(create_id).resolved is True
        assert queue.get(create_id).approved is True
        assert queue.get(create_id).consumed is False

        create_allowed = consume_approved_elevation(queue, create_id, create)

        assert create_allowed.allowed is True
        assert create_allowed.status == "approved"
        assert queue.get(create_id).consumed is True
        with pytest.raises(ValueError, match="already consumed"):
            consume_approved_elevation(queue, create_id, create)

        wrong_delete_grant = consume_approved_elevation(queue, delete_id, create)

        assert wrong_delete_grant.status == "approval_action_mismatch"
        assert queue.get(delete_id).resolved is False
        assert queue.get(delete_id).consumed is False

        queue.resolve(delete_id, True)
        delete_allowed = consume_approved_elevation(queue, delete_id, delete)

        assert delete_allowed.allowed is True
        assert delete_allowed.status == "approved"
        assert queue.get(delete_id).approved is True
        assert queue.get(delete_id).consumed is True
        with pytest.raises(ValueError, match="already consumed"):
            consume_approved_elevation(queue, delete_id, delete)
    finally:
        queue.close()


def test_elevation_request_persists_internal_retry_metadata(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("touch /mnt/desktop/probe")
    try:
        pending = request_elevation(
            queue,
            action,
            session_key="session-1",
            metadata={
                "backendRetry": True,
                "sandboxRequestFingerprint": "request-fp",
            },
        )

        params = queue.get(pending.approval_id or "").params
        assert params["backendRetry"] is True
        assert params["sandboxRequestFingerprint"] == "request-fp"
    finally:
        queue.close()


def test_approved_elevation_is_consumed_once(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("touch /mnt/desktop/probe")
    try:
        pending = request_elevation(queue, action, session_key="session-1")
        queue.resolve(pending.approval_id or "", True)

        decision = consume_approved_elevation(queue, pending.approval_id or "", action)

        assert decision.allowed is True
        assert decision.status == "approved"
        assert queue.get(pending.approval_id or "").consumed is True
        with pytest.raises(ValueError, match="already consumed"):
            consume_approved_elevation(queue, pending.approval_id or "", action)
    finally:
        queue.close()


def test_approved_elevation_cannot_be_consumed_from_another_session(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("touch /mnt/desktop/probe")
    try:
        pending = request_elevation(queue, action, session_key="session-a")
        queue.resolve(pending.approval_id or "", True)

        decision = consume_approved_elevation(
            queue,
            pending.approval_id or "",
            action,
            expected_session_key="session-b",
        )

        assert decision.allowed is False
        assert decision.reason == "approval_session_mismatch"
        assert queue.get(pending.approval_id or "").consumed is False
    finally:
        queue.close()


def test_approved_elevation_rejects_changed_action_without_consuming(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    original = _shell_action("touch /mnt/desktop/probe")
    changed = _shell_action("rm -rf /mnt/desktop")
    try:
        pending = request_elevation(queue, original, session_key="session-1")
        queue.resolve(pending.approval_id or "", True)

        decision = consume_approved_elevation(queue, pending.approval_id or "", changed)

        assert decision.allowed is False
        assert decision.reason == "approval_action_mismatch"
        assert queue.get(pending.approval_id or "").consumed is False
    finally:
        queue.close()


def test_denied_elevation_returns_reviewer_rationale(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("rm -rf /mnt/desktop")
    try:
        pending = request_elevation(queue, action, session_key="session-1")
        entry = queue.get(pending.approval_id or "")
        entry.params["reviewRationale"] = "The recursive delete has an uncertain target."
        queue.update_params(entry.approval_id, entry.params)
        queue.resolve(entry.approval_id, False)

        decision = consume_approved_elevation(queue, entry.approval_id, action)

        assert decision.allowed is False
        assert decision.status == "approval_denied"
        assert decision.reason == "The recursive delete has an uncertain target."
    finally:
        queue.close()


def test_request_elevation_requires_justification(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        with pytest.raises(ValueError, match="justification_required"):
            request_elevation(
                queue,
                replace(_shell_action("true"), justification=""),
                session_key="session-1",
            )
    finally:
        queue.close()


def test_gate_elevated_action_ignores_default_sandbox_intent(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        decision = gate_elevated_action(
            replace(_shell_action("true"), sandbox_permissions="use_default"),
            approval_id=None,
            session_key="session-1",
            queue=queue,
        )

        assert decision.requested is False
        assert decision.allowed is False
        assert decision.status == "use_default"
        assert queue.list_pending() == []
    finally:
        queue.close()


def test_gate_elevated_action_full_host_never_touches_approval_queue() -> None:
    class _FailIfUsedQueue:
        def __getattribute__(self, name: str):
            if name.startswith("__"):
                return object.__getattribute__(self, name)
            raise AssertionError(f"Full Host must not access ApprovalQueue.{name}")

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            run_mode="full",
            elevated="full",
            session_key="session-full",
        )
    )
    try:
        decision = gate_elevated_action(
            _shell_action("touch /mnt/desktop/probe"),
            approval_id="must-not-be-consumed",
            session_key="session-full",
            queue=_FailIfUsedQueue(),  # type: ignore[arg-type]
        )
    finally:
        current_tool_context.reset(token)

    assert decision.requested is False
    assert decision.allowed is True
    assert decision.status == "full_host_access"


def test_gate_elevated_action_requests_then_consumes_exact_approval(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    action = _shell_action("touch /mnt/desktop/probe")
    try:
        pending = gate_elevated_action(
            action,
            approval_id=None,
            session_key="session-1",
            queue=queue,
            reviewer="auto_review",
        )
        queue.resolve(pending.approval_id or "", True)

        approved = gate_elevated_action(
            action,
            approval_id=pending.approval_id,
            session_key="session-1",
            queue=queue,
            reviewer="auto_review",
        )

        assert approved.allowed is True
        assert queue.get(pending.approval_id or "").consumed is True
    finally:
        queue.close()
