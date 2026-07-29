"""Tests for D10: Dream incremental diff (content-hash dedup)."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from opensquilla.memory.dream.candidates import (
    DreamCandidateScan,
    scan_dream_candidate_batch,
    scan_dream_candidates,
)
from opensquilla.memory.dream.runner import Dream


class TestScanDeduplication:
    def test_scan_without_known_hashes(self, tmp_path):
        """Without known_hashes, all files are processed (backward compat)."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "test.md").write_text("We decided to use PostgreSQL.", encoding="utf-8")

        candidates = scan_dream_candidates(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
        )
        assert len(candidates) == 1
        assert candidates[0].source_path == "memory/test.md"

    def test_scan_with_empty_known_hashes(self, tmp_path):
        """Empty known_hashes set processes all files."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "test.md").write_text("We decided to use PostgreSQL.", encoding="utf-8")

        candidates = scan_dream_candidates(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
            known_hashes=set(),
        )
        assert len(candidates) == 1

    def test_scan_skips_unchanged_content(self, tmp_path):
        """File with matching snippet_sha256 is skipped."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "test.md").write_text("We decided to use PostgreSQL.", encoding="utf-8")

        # First scan to get the hash
        candidates = scan_dream_candidates(
            workspace, cursor=0.0, max_batch_size=20, agent_id="main",
        )
        assert len(candidates) == 1
        known_hash = candidates[0].snippet_sha256

        # Second scan with known_hashes should skip
        candidates2 = scan_dream_candidates(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
            known_hashes={known_hash},
        )
        assert len(candidates2) == 0

    def test_scan_processes_changed_content(self, tmp_path):
        """File with different content (different hash) is not skipped."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "test.md").write_text("We decided to use PostgreSQL.", encoding="utf-8")

        # Get hash of original content
        candidates = scan_dream_candidates(
            workspace, cursor=0.0, max_batch_size=20, agent_id="main",
        )
        old_hash = candidates[0].snippet_sha256

        # Change file content
        (memory_dir / "test.md").write_text("We decided to use Redis instead.", encoding="utf-8")
        time.sleep(0.01)

        # Scan with old hash — should still pick up new content
        candidates2 = scan_dream_candidates(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
            known_hashes={old_hash},
        )
        assert len(candidates2) == 1
        assert candidates2[0].snippet_sha256 != old_hash

    def test_scan_mixed_changed_and_unchanged(self, tmp_path):
        """Multiple files: some changed, some not."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "unchanged.md").write_text("Stable content here.", encoding="utf-8")
        (memory_dir / "changed.md").write_text("Original content.", encoding="utf-8")

        # First scan
        candidates = scan_dream_candidates(
            workspace, cursor=0.0, max_batch_size=20, agent_id="main",
        )
        hashes = {c.source_path: c.snippet_sha256 for c in candidates}
        assert len(hashes) == 2

        # Change one file
        (memory_dir / "changed.md").write_text("Updated content.", encoding="utf-8")
        time.sleep(0.01)

        # Scan with known hashes
        known = set(hashes.values())
        candidates2 = scan_dream_candidates(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
            known_hashes=known,
        )
        # Only changed.md should appear
        assert len(candidates2) == 1
        assert candidates2[0].source_path == "memory/changed.md"

    def test_scan_none_known_hashes_processes_all(self, tmp_path):
        """None known_hashes is same as not passing it."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        for i in range(3):
            (memory_dir / f"file{i}.md").write_text(f"Content {i}.", encoding="utf-8")

        candidates = scan_dream_candidates(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
            known_hashes=None,
        )
        assert len(candidates) == 3

    def test_scan_reports_unchanged_high_watermark(self, tmp_path):
        """Hash-equivalent files remain visible as consumed scan observations."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        path = memory_dir / "test.md"
        path.write_text("We decided to use PostgreSQL.", encoding="utf-8")

        first = scan_dream_candidates(
            workspace, cursor=0.0, max_batch_size=20, agent_id="main",
        )
        scan = scan_dream_candidate_batch(
            workspace,
            cursor=0.0,
            max_batch_size=20,
            agent_id="main",
            known_hashes={first[0].snippet_sha256},
        )

        assert scan.candidates == []
        assert scan.files_considered == 1
        assert scan.files_skipped_unchanged == 1
        assert scan.cursor_high_watermark == path.stat().st_mtime

    def test_high_watermark_does_not_skip_deferred_changed_file(self, tmp_path):
        """A newer unchanged file cannot move the cursor past a capped candidate."""
        workspace = tmp_path / "ws"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        first = memory_dir / "first.md"
        deferred = memory_dir / "deferred.md"
        unchanged = memory_dir / "unchanged.md"
        first.write_text("First changed content.", encoding="utf-8")
        deferred.write_text("Deferred changed content.", encoding="utf-8")
        unchanged.write_text("Stable unchanged content.", encoding="utf-8")
        os.utime(first, (100.0, 100.0))
        os.utime(deferred, (200.0, 200.0))
        os.utime(unchanged, (300.0, 300.0))

        initial = scan_dream_candidates(
            workspace, cursor=0.0, max_batch_size=20, agent_id="main",
        )
        unchanged_hash = next(
            item.snippet_sha256
            for item in initial
            if item.source_path == "memory/unchanged.md"
        )
        scan = scan_dream_candidate_batch(
            workspace,
            cursor=0.0,
            max_batch_size=1,
            agent_id="main",
            known_hashes={unchanged_hash},
        )

        assert [item.source_path for item in scan.candidates] == ["memory/first.md"]
        assert scan.cursor_high_watermark == 100.0

    @pytest.mark.asyncio
    async def test_unchanged_only_run_advances_cursor(self, tmp_path, monkeypatch):
        """An unchanged-only Dream run must not rescan the observation forever."""
        workspace = tmp_path / "ws"
        (workspace / "memory").mkdir(parents=True)
        scan = DreamCandidateScan(
            candidates=[],
            files_considered=1,
            files_skipped_unchanged=1,
            cursor_high_watermark=123.5,
        )
        monkeypatch.setattr(
            "opensquilla.memory.dream.runner.scan_dream_candidate_batch",
            lambda *args, **kwargs: scan,
        )
        dream = Dream(
            workspace=workspace,
            provider=None,
            session_lock=None,
            config=SimpleNamespace(
                max_batch_size=20,
                min_batch_size=1,
                evidence_quarantine_enabled=True,
                input_slimming="off",
                preview_mode=False,
                dry_run=False,
            ),
            agent_id="main",
        )

        result = await dream._run_evidence_consolidation()

        assert result.files_considered == 1
        assert result.files_skipped_unchanged == 1
        assert result.cursor_after == 123.5
        assert dream.cursor.load() == 123.5
