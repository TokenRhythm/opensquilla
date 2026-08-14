from __future__ import annotations

from pathlib import Path
from typing import Any

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
            model_supports_vision: bool | None = None,
        ) -> None:
            calls.append(
                {
                    "message": message,
                    "attachments": attachments,
                    "media_root": media_root,
                    "workspace_dir": workspace_dir,
                    "session_id": session_id,
                    "workspace_attachment_budget_bytes": workspace_attachment_budget_bytes,
                    "model_supports_vision": model_supports_vision,
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
        model_supports_vision=False,
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
            "model_supports_vision": False,
        }
    ]
