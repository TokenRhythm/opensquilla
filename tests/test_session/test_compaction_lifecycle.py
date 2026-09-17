from __future__ import annotations

from opensquilla.session.compaction_lifecycle import (
    durable_receipt_allows_destructive_compaction,
)


def test_checkpoint_receipt_allows_destructive_compaction() -> None:
    receipt = {
        "scope": "checkpoint",
        "status": "checkpoint_saved",
        "source_path": "memory/.checkpoints/agent-main-webchat-abc/turn-1.jsonl",
        "content_hash": "h1",
    }

    assert durable_receipt_allows_destructive_compaction(receipt) is True


def test_orphaned_checkpoint_receipt_is_not_destructive_safe() -> None:
    receipt = {"scope": "checkpoint", "status": "receipt_orphaned"}

    assert durable_receipt_allows_destructive_compaction(receipt) is False


def test_checkpoint_failed_receipt_is_not_destructive_safe() -> None:
    receipt = {
        "scope": "checkpoint",
        "status": "checkpoint_failed",
        "source_path": "memory/.checkpoints/agent-main-webchat-abc/turn-1.jsonl",
        "content_hash": "h1",
    }

    assert durable_receipt_allows_destructive_compaction(receipt) is False


def test_checkpoint_receipt_without_evidence_is_not_destructive_safe() -> None:
    receipt = {
        "scope": "checkpoint",
        "status": "checkpoint_saved",
        "source_path": "memory/.checkpoints/agent-main-webchat-abc/turn-1.jsonl",
        "content_hash": "",
    }

    assert durable_receipt_allows_destructive_compaction(receipt) is False
