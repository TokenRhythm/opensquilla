"""Tests for D10: Dream incremental diff (content-hash dedup)."""

from __future__ import annotations

import time

from opensquilla.memory.dream.candidates import scan_dream_candidates


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
