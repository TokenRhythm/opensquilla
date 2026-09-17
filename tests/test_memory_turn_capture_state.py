from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from opensquilla.memory.manager import _migrate_legacy_turn_archives
from opensquilla.memory.turn_capture import TurnCaptureService


@pytest.mark.asyncio
async def test_turn_capture_writes_state_turns_not_workspace_memory_archive(tmp_path):
    workspace = tmp_path / "workspace"
    turns_dir = tmp_path / "state" / "agents" / "main" / "turns"
    service = TurnCaptureService(
        workspace_dir=workspace,
        turns_dir=turns_dir,
        memory_config=SimpleNamespace(
            auto_capture_enabled=True,
            capture_mode="turn_pair",
            capture_user=True,
            capture_assistant=True,
            capture_max_chars=2000,
            capture_roll_max_chars=50_000,
        ),
    )

    rel_path = await service.capture_turn(
        session_key="agent:main:main",
        session_id="sess-1",
        user_text="hello",
        assistant_text="world",
        captured_at=datetime(2026, 5, 14, 3, 0, tzinfo=UTC),
    )

    assert rel_path == "turns/agent-main-main/2026-05-14.md"
    capture_file = turns_dir / "agent-main-main" / "2026-05-14.md"
    assert capture_file.is_file()
    content = capture_file.read_text(encoding="utf-8")
    assert "### User\nhello" in content
    assert "### Assistant\nworld" in content
    assert not (workspace / "memory" / "archive").exists()


@pytest.mark.asyncio
async def test_turn_capture_writes_raw_turn_without_memory_store(tmp_path):
    service = TurnCaptureService(
        workspace_dir=tmp_path / "workspace",
        turns_dir=tmp_path / "state" / "agents" / "main" / "turns",
        memory_config=SimpleNamespace(
            auto_capture_enabled=True,
            capture_mode="turn_pair",
            capture_user=True,
            capture_assistant=False,
            capture_max_chars=2000,
            capture_roll_max_chars=50_000,
        ),
    )

    await service.capture_turn(
        session_key="agent:main:main",
        session_id="sess-1",
        user_text="index toggle should not index raw turns",
        assistant_text="ignored",
        captured_at=datetime(2026, 5, 14, 3, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_turn_capture_follows_replaced_live_root_memory_config(tmp_path):
    def memory_config(*, enabled: bool) -> SimpleNamespace:
        return SimpleNamespace(
            auto_capture_enabled=enabled,
            capture_mode="turn_pair",
            capture_user=True,
            capture_assistant=False,
            capture_max_chars=2000,
            capture_roll_max_chars=50_000,
        )

    root_config = SimpleNamespace(memory=memory_config(enabled=True))
    turns_dir = tmp_path / "state" / "agents" / "main" / "turns"
    service = TurnCaptureService(
        workspace_dir=tmp_path / "workspace",
        turns_dir=turns_dir,
        memory_config=root_config,
    )

    captured = await service.capture_turn(
        session_key="agent:main:main",
        session_id="sess-1",
        user_text="capture before the live toggle",
        assistant_text="ignored",
        captured_at=datetime(2026, 5, 14, 3, 2, tzinfo=UTC),
    )
    assert captured == "turns/agent-main-main/2026-05-14.md"
    capture_file = turns_dir / "agent-main-main" / "2026-05-14.md"
    before_disable = capture_file.read_text(encoding="utf-8")

    # Gateway config patches preserve the root object but replace its nested
    # memory model. The already-running service must observe that replacement.
    root_config.memory = memory_config(enabled=False)
    skipped = await service.capture_turn(
        session_key="agent:main:main",
        session_id="sess-1",
        user_text="do not capture after the live toggle",
        assistant_text="ignored",
        captured_at=datetime(2026, 5, 14, 3, 3, tzinfo=UTC),
    )
    assert skipped is None
    assert capture_file.read_text(encoding="utf-8") == before_disable

    root_config.memory = memory_config(enabled=True)
    resumed = await service.capture_turn(
        session_key="agent:main:main",
        session_id="sess-1",
        user_text="capture after re-enabling",
        assistant_text="ignored",
        captured_at=datetime(2026, 5, 14, 3, 4, tzinfo=UTC),
    )
    assert resumed == "turns/agent-main-main/2026-05-14.md"
    assert "capture after re-enabling" in capture_file.read_text(encoding="utf-8")


def test_migrate_legacy_turn_archives_moves_only_system_captures(tmp_path):
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    archive = memory / "archive"
    legacy_session_dir = archive / "agent-main-main"
    legacy_session_dir.mkdir(parents=True)
    turns_dir = tmp_path / "state" / "agents" / "main" / "turns"
    legacy_capture = legacy_session_dir / "2026-05-14.md"
    legacy_content = "\n".join(
        [
            "# Turn Capture Archive",
            "",
            "- source_kind: turn_capture",
            "- session_key: agent:main:main",
            "- schema: turn-capture-v1",
            "",
            "## Turn 2026-05-14T03:00:00Z",
            "### User",
            "raw prompt",
        ]
    )
    legacy_capture.write_text(legacy_content, encoding="utf-8")
    user_archive_note = archive / "user-note.md"
    user_archive_note.write_text("# Curated archive note\n", encoding="utf-8")

    moved = _migrate_legacy_turn_archives(memory, turns_dir)

    assert moved == ("memory/archive/agent-main-main/2026-05-14.md",)
    migrated = turns_dir / "agent-main-main" / "2026-05-14.md"
    assert migrated.read_text(encoding="utf-8") == legacy_content
    assert not legacy_capture.exists()
    assert user_archive_note.read_text(encoding="utf-8") == "# Curated archive note\n"
