"""A killed writer leaves a whole old or new checkpoint, never half a rewrite.

Only a test-owned subprocess is killed. SQLite, the archive/rewrite transaction,
source CAS, and replay selection are real; this does not simulate power loss.
"""

from __future__ import annotations

import asyncio
import json
import os
import site
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from opensquilla.session.compaction_state import (
    StructuredCompactionSummary,
    render_structured_summary,
)
from opensquilla.session.context_view import (
    build_compaction_context_records,
    compaction_context_fingerprint,
)
from opensquilla.session.models import (
    SessionContextState,
    SessionNode,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage, _transcript_preimage


def _checkpoint(node, entries, generation):
    compaction_id = f"crash-checkpoint-{generation}"
    payload = StructuredCompactionSummary(
        current_status=f"CHECKPOINT_{generation}: task state {generation}",
        source_coverage={"replaces_prior_context": True},
    ).model_dump(mode="json")
    payload["compaction_id"] = compaction_id
    common = dict(
        session_id=node.session_id, session_key=node.session_key,
        covered_through_id=entries[1].id, created_at=1000 + generation,
    )
    return (
        SessionSummary(
            **common, compaction_id=compaction_id, summary_payload=payload,
            summary_text=render_structured_summary(payload), summary_format="structured_v1",
            removed_count=2, kept_count=len(entries) - 2,
        ),
        SessionContextState(
            **common, state_kind="structured_summary_v1", payload=payload, portable=True,
        ),
    )


async def _rewrite(storage, node, entries, generation, fingerprint):
    summary, state = _checkpoint(node, entries, generation)
    node = node.model_copy(update={"compaction_count": generation})
    return await storage.rewrite_compacted_session(
        node=node, summary=summary, context_states=[state],
        entries=entries[2:], archived_entries=entries[:2],
        expected_source_entries=entries,
        expected_source_preimage=_transcript_preimage(entries),
        expected_context_fingerprint=fingerprint,
        expected_session_id=node.session_id, expected_session_epoch=node.epoch,
    )


async def _seed(root):
    storage = await SessionStorage.open(str(root / "sessions.db"))
    try:
        node = SessionNode(
            session_key="agent:main:webchat:crash-only", session_id="crash-only",
        )
        await storage.upsert_session(node)
        for index in range(6):
            await storage.append_transcript_entry(TranscriptEntry(
                session_id=node.session_id, session_key=node.session_key,
                message_id=f"raw-{index}", role="user" if index % 2 == 0 else "assistant",
                content=f"Original record {index}; 中文 raw source",
                created_at=100 + index, turn_context={"turn_id": f"turn-{index // 2}"},
                tool_calls=[
                    {"type": "tool_use", "id": "call_42", "name": "exec",
                     "input": {"command": "echo 48"}},
                    {"type": "tool_result", "tool_use_id": "call_42", "content": "48"},
                ] if index == 3 else None,
            ), expected_epoch=0)
        original = await storage.get_transcript(node.session_id)
        assert await _rewrite(storage, node, original, 1, None)
        states = await storage.get_context_states(node.session_key)
        summaries = await storage.get_all_summaries(node.session_id)
        return {
            "node": node.model_dump(mode="json"),
            "original": [entry.model_dump(mode="json") for entry in original],
            "fingerprint": compaction_context_fingerprint(
                context_states=states, summaries=summaries,
            ),
        }
    finally:
        await storage.close()


async def _assert_generation(storage, fixture, generation):
    node = SessionNode.model_validate(fixture["node"])
    canonical = await storage.get_canonical_transcript(node.session_id)
    assert [entry.model_dump(mode="json") for entry in canonical] == fixture["original"]
    assert len({entry.id for entry in canonical}) == 6
    active = await storage.get_transcript(node.session_id)
    assert [entry.model_dump(mode="json") for entry in active] == (
        fixture["original"][generation * 2:]
    )
    summaries = await storage.get_all_summaries(node.session_id)
    states = await storage.get_context_states(node.session_key)
    assert len(summaries) == len(states) == generation
    assert [summary.compaction_id for summary in summaries] == [
        f"crash-checkpoint-{number}" for number in range(1, generation + 1)
    ]
    assert (await storage.get_session(node.session_key)).compaction_count == generation
    records = build_compaction_context_records(context_states=states, summaries=summaries)
    assert len(records) == 1
    assert f"CHECKPOINT_{generation}" in records[0].text
    assert records[0].covered_through_id == canonical[generation * 2 - 1].id
    if generation == 2:
        assert "CHECKPOINT_1" not in records[0].text
    for number in range(1, generation + 1):
        archived = await storage.get_compacted_transcript_entries(
            session_id=node.session_id, compaction_id=f"crash-checkpoint-{number}",
        )
        assert [entry.model_dump(mode="json") for entry in archived] == (
            fixture["original"][(number - 1) * 2:number * 2]
        )
    async with storage.conn.execute("PRAGMA integrity_check") as cursor:
        assert [row[0] for row in await cursor.fetchall()] == ["ok"]
    async with storage.conn.execute("PRAGMA foreign_key_check") as cursor:
        assert await cursor.fetchall() == []


async def _worker(root, mode, phase, nonce):
    fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    node = SessionNode.model_validate(fixture["node"])
    source = [TranscriptEntry.model_validate(item) for item in fixture["original"][2:]]
    storage = await SessionStorage.open(str(root / "sessions.db"))
    try:
        if mode == "inspect":
            expected = 2 if phase in {"after_commit", "settled"} else 1
            await _assert_generation(storage, fixture, expected)
            # The exact pre-crash source can finish once or be rejected as stale;
            # an unknown result must not duplicate archive/checkpoint records.
            applied = await _rewrite(storage, node, source, 2, fixture["fingerprint"])
            assert applied is (expected == 1)
            await _assert_generation(storage, fixture, 2)
            return

        async def pause():
            (root / "ready.json").write_text(json.dumps({
                "pid": os.getpid(), "nonce": nonce, "phase": phase,
            }), encoding="utf-8")
            await asyncio.Event().wait()

        transaction = storage._write_transaction

        @asynccontextmanager
        async def paused_transaction(operation, **kwargs):
            assert operation == "rewrite_compacted_session"
            if phase == "before_transaction":
                await pause()
            async with transaction(operation, **kwargs) as conn:
                yield conn
                if phase == "before_commit":
                    # Prove all four tables changed on the actual uncommitted
                    # writer connection before killing, not merely BEGIN.
                    for table, expected in (
                        ("transcript_entries", 2), ("compacted_transcript_entries", 4),
                        ("session_summaries", 2), ("session_context_states", 2),
                    ):
                        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                            assert (await cursor.fetchone())[0] == expected
                    await pause()
            if phase == "after_commit":
                await pause()

        storage._write_transaction = paused_transaction
        await _rewrite(storage, node, source, 2, fixture["fingerprint"])
        raise AssertionError("crash worker returned without being terminated")
    finally:
        await storage.close()


def _command(root, mode, phase, nonce):
    # Windows venv python.exe can be a launcher with a different child PID.
    # Launch the interpreter directly so the nonce handshake identifies Popen.
    executable = sys._base_executable if os.name == "nt" else sys.executable
    return [executable, str(Path(__file__).resolve()), "--worker",
            str(root), mode, phase, nonce]


@pytest.mark.parametrize("phase", ["before_transaction", "before_commit", "after_commit"])
def test_compaction_process_kill_preserves_atomic_checkpoint(tmp_path, phase):
    fixture = asyncio.run(_seed(tmp_path))
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    nonce = uuid.uuid4().hex
    repo = Path(__file__).resolve().parents[2]
    env = {
        **os.environ, "PYTHONPATH": os.pathsep.join([str(repo / "src"), *site.getsitepackages()]),
        "OPENSQUILLA_STATE_DIR": str(tmp_path / "state"),
        "OPENSQUILLA_USER_STATE_DIR": str(tmp_path / "profile"),
        "OPENSQUILLA_LOG_DIR": str(tmp_path / "logs"),
        "OPENSQUILLA_TURN_CALL_LOG": "0", "PYTHONDONTWRITEBYTECODE": "1",
    }
    options = dict(
        env=env, cwd=repo, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    with (tmp_path / "writer.log").open("wb") as log:
        process = subprocess.Popen(
            _command(tmp_path, "write", phase, nonce), stdout=log, stderr=log, **options,
        )
        try:
            deadline = time.monotonic() + 30
            ready = None
            while time.monotonic() < deadline:
                if (tmp_path / "ready.json").exists():
                    try:
                        ready = json.loads((tmp_path / "ready.json").read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        pass
                    if ready is not None:
                        break
                assert process.poll() is None, (tmp_path / "writer.log").read_text(
                    encoding="utf-8", errors="replace",
                )
                time.sleep(0.02)
            assert ready == {"pid": process.pid, "nonce": nonce, "phase": phase}
            assert process.poll() is None
            process.kill()
            assert process.wait(timeout=10) != 0
        finally:
            # Popen owns this exact PID; never search/terminate unrelated processes.
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    for expected_phase in (phase, "settled"):
        result = subprocess.run(
            _command(tmp_path, "inspect", expected_phase, nonce),
            capture_output=True, text=True, timeout=30, **options,
        )
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    assert sys.argv[1] == "--worker"
    asyncio.run(_worker(Path(sys.argv[2]), *sys.argv[3:]))
