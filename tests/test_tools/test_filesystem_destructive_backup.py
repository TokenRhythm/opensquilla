from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.file_policy import FileDecision
from opensquilla.sandbox.policy_models import SandboxPolicy
from opensquilla.tools.builtin import filesystem
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_approved_existing_file_write_is_backed_up_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_approval_queue()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "important.txt"
    target.write_text("before", encoding="utf-8")
    state_dir = tmp_path / "state"
    context = ToolContext(
        run_mode="safe",
        workspace_dir=str(workspace),
        session_key="session-1",
        sandbox_policy=SandboxPolicy(),
        sandbox_gateway_config=SimpleNamespace(state_dir=str(state_dir)),
    )
    monkeypatch.setattr(filesystem, "_sandbox_path_access_enabled", lambda: True)
    monkeypatch.setattr(
        filesystem,
        "decide_file_access",
        lambda *_args, **_kwargs: FileDecision(
            allowed=False,
            approval_required=True,
            code="test_protected_path",
            matched_path=target,
            rule_source="custom",
        ),
    )
    token = current_tool_context.set(context)
    try:
        first, elevated, first_backups = await filesystem._gate_out_of_workspace_write(
            "write_file",
            target,
            str(target),
            None,
            sandbox_permissions="require_escalated",
            justification="Replace the exact protected file requested by the user.",
            content_digest="sha256:after",
        )
        assert first is not None
        assert elevated is False
        assert first_backups == ()
        approval_id = str(first["approval_id"])
        pending_action = get_approval_queue().get(approval_id).params["action"]
        assert pending_action["display"]["kind"] == "modify"
        assert pending_action["display"]["backup_state"] == "enabled"
        get_approval_queue().resolve(approval_id, True)

        resumed, elevated, backup_summaries = await filesystem._gate_out_of_workspace_write(
            "write_file",
            target,
            str(target),
            approval_id,
            sandbox_permissions="require_escalated",
            justification="Replace the exact protected file requested by the user.",
            content_digest="sha256:after",
        )

        assert resumed is None
        assert elevated is True
        assert len(backup_summaries) == 1
        assert set(backup_summaries[0]) == {
            "backupId",
            "target",
            "sizeBytes",
            "createdAt",
        }
        assert backup_summaries[0]["target"] == str(target.resolve())
        receipts = tuple((state_dir / "backup-vault" / "entries").iterdir())
        assert len(receipts) == 1
        assert (receipts[0] / "content").read_text(encoding="utf-8") == "before"
        assert target.read_text(encoding="utf-8") == "before"
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()
