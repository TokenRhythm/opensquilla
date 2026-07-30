from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opensquilla.memory.dream import Dream
from opensquilla.memory.dream.evidence import load_evidence_store


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _PatchProvider:
    def __init__(self, text: str | None = None, *, delete_path=None) -> None:
        self.calls = 0
        self.delete_path = delete_path
        self.text = text or json.dumps(
            {
                "operations": [
                    {
                        "op": "upsert",
                        "candidate_ids": ["auto"],
                        "section": "User Preferences",
                        "memory_id": "mem_provider_benchmarks",
                        "text": "- User prefers provider-backed benchmarks over toy simulations.",
                    }
                ]
            }
        )

    async def complete(self, *, messages, max_tokens):
        self.calls += 1
        if self.delete_path is not None and self.delete_path.exists():
            self.delete_path.unlink()
        return _Response(self.text)


class _FailIfCalledProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, messages, max_tokens):  # noqa: ARG002
        self.calls += 1
        raise AssertionError("provider should not be called")


class _FailOnceProvider(_PatchProvider):
    async def complete(self, *, messages, max_tokens):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider failure")
        return _Response(self.text)


def _dream(workspace, *, provider=None, preview=False):
    config = SimpleNamespace(
        max_batch_size=10,
        min_batch_size=1,
        input_slimming="off",
        preview_mode=preview,
        dry_run=preview,
        evidence_min_score=0.0,
        evidence_negative_recurrence_threshold=2,
        evidence_quarantine_enabled=True,
        evidence_curated_writes_enabled=True,
    )
    return Dream(
        workspace=workspace,
        provider=provider or _PatchProvider(),
        session_lock=None,
        config=config,
    )


@pytest.mark.asyncio
async def test_dream_records_evidence_and_writes_curated_memory(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "note.md").write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )

    result = await _dream(tmp_path).run()

    assert result.evidence_status == "ok"
    assert result.apply_status == "ok"
    assert "User Preferences" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert (memory_dir / ".dream_state" / "promotion_evidence.json").exists()
    assert (memory_dir / "note.md").exists()


@pytest.mark.asyncio
async def test_dream_retries_pending_evidence_after_apply_failure(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "note.md").write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )
    provider = _FailOnceProvider()
    dream = _dream(tmp_path, provider=provider)

    failed = await dream.run()
    retried = await dream.run()

    assert failed.apply_status == "error"
    assert retried.apply_status == "ok"
    assert provider.calls == 2
    assert "User Preferences" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert all(
        entry.status != "candidate"
        for entry in load_evidence_store(tmp_path).entries.values()
    )


@pytest.mark.asyncio
async def test_dream_model_skip_is_terminal_without_repeated_provider_calls(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "note.md").write_text(
        "User prefers a one-off experimental label.",
        encoding="utf-8",
    )
    provider = _PatchProvider(
        text=json.dumps(
            {
                "operations": [
                    {
                        "op": "skip",
                        "candidate_ids": ["auto"],
                        "reason": "not durable",
                    }
                ]
            }
        )
    )
    dream = _dream(tmp_path, provider=provider)

    first = await dream.run()
    second = await dream.run()

    assert first.apply_status == "ok"
    assert second.apply_status == "skipped"
    assert provider.calls == 1
    assert {
        entry.status for entry in load_evidence_store(tmp_path).entries.values()
    } == {"skipped"}


@pytest.mark.asyncio
async def test_dream_waits_for_agent_session_lock(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    lock = asyncio.Lock()
    await lock.acquire()
    dream = _dream(tmp_path)
    dream.session_lock = lock

    task = asyncio.create_task(dream.run())
    await asyncio.sleep(0)
    assert not task.done()

    lock.release()
    result = await task
    assert result.evidence_status == "skipped"


@pytest.mark.asyncio
async def test_dream_result_and_log_use_single_path_names(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "note.md").write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )

    result = await _dream(tmp_path).run()
    log_path = next((tmp_path / "logs").glob("dream-main-*.jsonl"))
    log_row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])

    assert result.evidence_status == "ok"
    assert result.apply_status == "ok"
    assert result.provider_calls == 1
    assert not hasattr(result, "phase" + "1_status")
    assert not hasattr(result, "phase" + "2_status")
    assert not hasattr(result, "files_deleted")
    assert "evidence_status" in log_row
    assert "apply_status" in log_row
    assert "provider_calls" in log_row
    assert "files_deleted" not in log_row
    assert all("phase" not in key for key in log_row)


@pytest.mark.asyncio
async def test_dream_preview_mutates_nothing(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    note = memory_dir / "note.md"
    note.write_text("User prefers concise implementation notes.", encoding="utf-8")

    result = await _dream(tmp_path, preview=True).run()

    assert result.evidence_status == "ok"
    assert result.apply_status == "ok"
    assert note.exists()
    assert not (tmp_path / "MEMORY.md").exists()
    assert not (memory_dir / ".dream_state" / "promotion_evidence.json").exists()
    assert result.cursor_after == result.cursor_before


@pytest.mark.asyncio
async def test_dream_externally_missing_candidate_after_evidence_does_not_raise(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    note = memory_dir / "note.md"
    note.write_text("User prefers concise implementation notes.", encoding="utf-8")
    provider = _PatchProvider(delete_path=note)

    result = await _dream(tmp_path, provider=provider).run()

    assert result.apply_status == "ok"
    assert "FileNotFoundError" not in (result.error or "")


@pytest.mark.asyncio
async def test_dream_writes_evidence_receipt(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "note.md").write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )

    result = await _dream(tmp_path).run()

    assert result.edit_receipt_path is not None
    receipt = json.loads((tmp_path / result.edit_receipt_path).read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 1
    assert "version" not in receipt
    assert "ranked_candidates" in receipt
    assert "applied_promotions" in receipt


@pytest.mark.asyncio
async def test_dream_skips_provider_when_no_candidates_rank(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    note = memory_dir / "note.md"
    note.write_text("Do not use this one-off label.", encoding="utf-8")
    provider = _FailIfCalledProvider()

    result = await _dream(tmp_path, provider=provider).run()

    assert provider.calls == 0
    assert result.provider_calls == 0
    assert result.evidence_status == "ok"
    assert result.apply_status == "skipped"
    assert note.exists()
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_dream_does_not_promote_stale_candidate_from_mixed_operation(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    stale = memory_dir / "stale.md"
    live = memory_dir / "live.md"
    stale.write_text("User prefers stale source.", encoding="utf-8")
    live.write_text("User prefers live source.", encoding="utf-8")
    provider = _PatchProvider(
        text=json.dumps(
            {
                "operations": [
                    {
                        "op": "upsert",
                        "candidate_ids": ["auto"],
                        "section": "User Preferences",
                        "memory_id": "mem_mixed",
                        "text": "- User prefers live source.",
                    }
                ]
            }
        ),
        delete_path=stale,
    )

    result = await _dream(tmp_path, provider=provider).run()
    store = load_evidence_store(tmp_path)
    stale_entries = [
        entry for entry in store.entries.values() if entry.source_path == "memory/stale.md"
    ]
    live_entries = [
        entry for entry in store.entries.values() if entry.source_path == "memory/live.md"
    ]

    assert result.apply_status == "ok"
    assert stale_entries and stale_entries[0].status == "skipped"
    assert stale_entries[0].last_skip_reason == "source_missing"
    assert live_entries and live_entries[0].status == "promoted"


@pytest.mark.asyncio
async def test_dream_marks_noop_curated_write_as_represented_not_promoted(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    existing = "- User prefers provider-backed benchmarks over toy simulations."
    (tmp_path / "MEMORY.md").write_text(
        f"# Long-Term Memory\n\n## User Preferences\n\n{existing}\n",
        encoding="utf-8",
    )
    (memory_dir / "note.md").write_text(
        "User prefers provider-backed benchmarks over toy simulations.",
        encoding="utf-8",
    )

    result = await _dream(tmp_path).run()
    store = load_evidence_store(tmp_path)
    entry = next(iter(store.entries.values()))
    receipt = json.loads((tmp_path / result.edit_receipt_path).read_text(encoding="utf-8"))

    assert result.apply_status == "ok"
    assert entry.status == "represented"
    assert entry.promoted_at is None
    assert entry.last_skip_reason == "no_curated_change"
    assert receipt["applied_promotions"][0]["changed"] is False
