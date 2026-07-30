from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.memory.dream import Dream


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _CapturingPatchProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, *, messages, max_tokens):  # noqa: ARG002
        self.calls += 1
        self.prompts.append(messages[0].content)
        return _Response(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "upsert",
                            "candidate_ids": ["auto"],
                            "section": "User Preferences",
                            "memory_id": "mem_provider_benchmarks",
                            "text": (
                                "- User prefers provider-backed benchmarks "
                                "over toy simulations."
                            ),
                        }
                    ]
                }
            )
        )


def _dream(workspace, *, provider=None):
    return Dream(
        workspace=workspace,
        provider=provider or _CapturingPatchProvider(),
        session_lock=None,
        config=SimpleNamespace(
            max_batch_size=10,
            min_batch_size=1,
            input_slimming="off",
            preview_mode=False,
            dry_run=False,
            evidence_min_score=0.0,
            evidence_negative_recurrence_threshold=2,
            evidence_quarantine_enabled=True,
            evidence_curated_writes_enabled=True,
        ),
    )


@pytest.mark.asyncio
async def test_dream_uses_workspace_root_memory_md_for_curated_memory(tmp_path):
    root_memory = tmp_path / "MEMORY.md"
    nested_memory_dir = tmp_path / "memory"
    nested_memory = nested_memory_dir / "MEMORY.md"
    candidate = nested_memory_dir / "candidate.md"
    root_memory.write_text("root curated marker", encoding="utf-8")
    nested_memory_dir.mkdir()
    nested_memory.write_text("nested stale marker", encoding="utf-8")
    candidate.write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )
    provider = _CapturingPatchProvider()

    result = await _dream(tmp_path, provider=provider).run()

    assert result.apply_status == "ok"
    assert provider.calls == 1
    assert "root curated marker" in provider.prompts[0]
    assert "nested stale marker" not in provider.prompts[0]


@pytest.mark.asyncio
async def test_dream_reports_apply_error_when_cursor_cleanup_fails(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "candidate.md").write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )
    dream = _dream(tmp_path)

    def fail_save(_position) -> None:
        raise OSError("cursor denied")

    monkeypatch.setattr(dream.cursor, "save_position", fail_save)

    result = await dream.run()

    assert result.apply_status == "error"
    assert "cursor denied" in (result.error or "")
