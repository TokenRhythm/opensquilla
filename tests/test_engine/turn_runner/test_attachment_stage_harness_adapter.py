from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.turn_runner.harness import _TurnRunnerAttachmentMessageBuilderAdapter


def test_attachment_builder_adapter_prefers_resolved_workspace(tmp_path: Path) -> None:
    """Per-agent workspace resolution must beat the runner config fallback."""

    calls: list[dict[str, Any]] = []

    class _Runner:
        _config = type("_Config", (), {"workspace_dir": str(tmp_path / "config")})()

        def _attachment_media_root(self) -> Path:
            return tmp_path / "media"

        def _build_attachment_messages(
            self,
            message: str,
            attachments: list[dict],
            *,
            media_root: Path | None = None,
            workspace_dir: str | Path | None = None,
            session_id: str | None = None,
            workspace_attachment_budget_bytes: int | None = None,
        ) -> None:
            calls.append(
                {
                    "message": message,
                    "attachments": attachments,
                    "media_root": media_root,
                    "workspace_dir": workspace_dir,
                    "session_id": session_id,
                    "workspace_attachment_budget_bytes": workspace_attachment_budget_bytes,
                }
            )
            return None

    adapter = _TurnRunnerAttachmentMessageBuilderAdapter(_Runner())  # type: ignore[arg-type]
    agent_workspace = tmp_path / "agent"

    adapter.build(
        "hello",
        [{"type": "text/plain", "data": "aGk="}],
        workspace_dir=agent_workspace,
        session_id="session-a",
    )

    assert calls == [
        {
            "message": "hello",
            "attachments": [{"type": "text/plain", "data": "aGk="}],
            "media_root": tmp_path / "media",
            "workspace_dir": agent_workspace,
            "session_id": "session-a",
            # The fake config has no attachments section, so the budget
            # resolver falls back to unbounded.
            "workspace_attachment_budget_bytes": None,
        }
    ]


@pytest.mark.parametrize("cancellable", [True, False])
@pytest.mark.parametrize("explicit", [None, True, False])
def test_attachment_builder_preserves_explicit_turn_image_policy(
    tmp_path: Path, cancellable: bool, explicit: bool | None,
) -> None:
    from types import SimpleNamespace

    calls = []

    class _Runner:
        _config = SimpleNamespace(attachments=SimpleNamespace(persist_transcripts=True))

        def _turn_config(self):
            return SimpleNamespace(attachments=SimpleNamespace(persist_transcripts=False))

        def _attachment_media_root(self):
            return tmp_path / "media"

        def _build_attachment_messages(self, *args, **kwargs):
            calls.append(kwargs)

    adapter = _TurnRunnerAttachmentMessageBuilderAdapter(_Runner())  # type: ignore[arg-type]
    kwargs = {"persist_image_material": explicit, "image_workspace_dir": tmp_path / "scratch"}
    if cancellable:
        adapter.build_cancellable("hello", [], cancel_check=lambda: None, **kwargs)
    else:
        adapter.build("hello", [], **kwargs)
    assert len(calls) == 1
    if explicit is True:
        assert "persist_image_material" not in calls[0]
    else:
        assert calls[0]["persist_image_material"] is False
        assert calls[0]["image_workspace_dir"] == tmp_path / "scratch"
