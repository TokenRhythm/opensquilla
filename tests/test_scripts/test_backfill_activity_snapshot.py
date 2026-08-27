from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.backfill_activity_snapshot import main


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    replay = tmp_path / "replay.json"
    history = tmp_path / "history.json"
    db = tmp_path / "sessions.db"
    common = {
        "task_id": "turn-1",
        "turn_id": "turn-1",
        "stream_generation": "generation-1",
    }
    events = [
        {
            "event": "session.event.provider_activity",
            "payload": {
                **common,
                "stream_seq": 1,
                "emitted_at": 1_000,
                "phase": "requesting",
            },
        },
        {
            "event": "session.event.thinking_start",
            "payload": {
                **common,
                "stream_seq": 2,
                "emitted_at": 2_000,
                "block_id": "reasoning-1",
                "block_index": 0,
            },
        },
        {
            "event": "session.event.thinking",
            "payload": {
                **common,
                "stream_seq": 3,
                "emitted_at": 2_100,
                "block_id": "reasoning-1",
                "text": "A😀",
            },
        },
        {
            "event": "session.event.thinking_end",
            "payload": {
                **common,
                "stream_seq": 4,
                "emitted_at": 2_200,
                "block_id": "reasoning-1",
                "status": "completed",
            },
        },
        {
            "event": "session.event.text_delta",
            "payload": {
                **common,
                "stream_seq": 5,
                "emitted_at": 3_000,
                "text": "answer",
                "presentation": "answer",
            },
        },
        {
            "event": "session.event.done",
            "payload": {**common, "stream_seq": 6, "emitted_at": 4_000},
        },
        {
            "event": "session.event.turn_committed",
            "payload": {**common, "stream_seq": 7, "emitted_at": 4_100},
        },
    ]
    _write(replay, {"events": events})
    _write(
        history,
        {
            "messages": [
                {
                    "role": "assistant",
                    "message_id": "assistant-1",
                    "text": "answer",
                    "reasoning_content": "A😀",
                    "turn_context": {"turn_id": "turn-1"},
                }
            ]
        },
    )
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE agent_tasks ("
        "task_id TEXT PRIMARY KEY, session_key TEXT, status TEXT, details TEXT)"
    )
    connection.execute(
        "INSERT INTO agent_tasks VALUES (?, ?, ?, ?)",
        ("turn-1", "agent:main:test", "succeeded", json.dumps({"turn_id": "turn-1"})),
    )
    connection.commit()
    connection.close()
    return replay, history, db


def test_backfill_dry_run_then_apply_with_readback_hash(
    tmp_path: Path,
    capsys,
) -> None:
    replay, history, db = _fixture(tmp_path)
    common = [
        "--replay",
        str(replay),
        "--history",
        str(history),
        "--db",
        str(db),
        "--session-key",
        "agent:main:test",
        "--task-id",
        "turn-1",
        "--turn-id",
        "turn-1",
        "--assistant-message-id",
        "assistant-1",
    ]
    assert main(common) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["ok"] is True
    assert dry_run["applied"] is False

    backup = tmp_path / "sessions.backup.db"
    assert main([*common, "--apply", "--backup", str(backup)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert backup.is_file()
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT details FROM agent_tasks WHERE task_id = 'turn-1'"
        ).fetchone()
    assert row is not None
    details = json.loads(row[0])
    assert details["activity_snapshot"]["version"] == 2
    assert details["activity_snapshot"]["complete"] is True


def test_backfill_rejects_replay_without_terminal_commit(
    tmp_path: Path,
    capsys,
) -> None:
    replay, history, db = _fixture(tmp_path)
    data = json.loads(replay.read_text(encoding="utf-8"))
    data["events"].pop()
    _write(replay, data)
    assert main(
        [
            "--replay",
            str(replay),
            "--history",
            str(history),
            "--db",
            str(db),
            "--session-key",
            "agent:main:test",
            "--task-id",
            "turn-1",
            "--turn-id",
            "turn-1",
        ]
    ) == 2
    error = json.loads(capsys.readouterr().err)
    assert "terminal commit" in error["error"]
