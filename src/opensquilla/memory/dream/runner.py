"""Dream — per-agent cron-scheduled evidence-gated memory consolidation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.engine.usage_accounting import (
    account_provider_stream,
    current_usage_accounting_scope,
    provider_accounts_physical_usage,
)
from opensquilla.memory.dream.candidates import scan_dream_candidates
from opensquilla.memory.dream.curated_apply import apply_promotion_patch
from opensquilla.memory.dream.evidence import (
    mark_evidence_promoted,
    mark_evidence_represented,
    mark_evidence_skipped,
    update_promotion_evidence,
    write_evidence_store,
)
from opensquilla.memory.dream.models import (
    ApplyPromotionResult,
    PromotionPatch,
    PromotionPatchOperation,
)
from opensquilla.memory.dream.prompts import parse_promotion_patch, promotion_patch_prompt
from opensquilla.memory.dream.ranking import rank_promotion_candidates
from opensquilla.memory.dream.receipts import write_dream_receipt
from opensquilla.memory.dream.rehydrate import rehydrate_candidate
from opensquilla.memory.protocols import MemoryProviderCapability
from opensquilla.provider.protocol import provider_metadata
from opensquilla.provider.types import Message

logger = logging.getLogger(__name__)


async def _run_complete(
    provider: MemoryProviderCapability,
    messages: list[Message],
    max_tokens: int,
) -> str:
    """Completion through the explicit memory provider capability surface.

    Prefers ``provider.complete(messages=..., max_tokens=...)`` when
    present (unit tests + stubs). Falls back to streaming
    ``provider.chat(messages)`` and concatenating text deltas (real
    providers like OpenAIProvider).
    """
    complete = getattr(provider, "complete", None)
    if callable(complete):
        resp = await complete(messages=messages, max_tokens=max_tokens)
        return getattr(resp, "content", None) or getattr(resp, "text", "") or ""
    chat = getattr(provider, "chat", None)
    if not callable(chat):
        raise TypeError(
            f"Provider {type(provider).__name__} supports neither complete() nor chat()"
        )
    from opensquilla.provider.types import ChatConfig

    config = ChatConfig(max_tokens=max_tokens)
    scope = current_usage_accounting_scope()
    close_stream = None
    if scope is None:
        stream = chat(messages, config=config)
    elif provider_accounts_physical_usage(provider):
        stream = chat(messages, config=config)
        close_stream = stream
    else:
        metadata = provider_metadata(provider)
        stream = account_provider_stream(
            lambda: chat(messages, config=config),
            provider=metadata.provider_name or metadata.provider_kind,
            model=metadata.model,
        )
        close_stream = stream

    chunks: list[str] = []
    try:
        async for event in stream:
            ev_name = type(event).__name__
            if ev_name == "ErrorEvent":
                # Surface provider errors (auth, rate-limit, HTTP) instead of
                # pretending we got an empty response that fails later as bad JSON.
                msg = getattr(event, "message", "") or "provider error"
                raise RuntimeError(f"provider error: {msg}")
            text = getattr(event, "text", "") or ""
            if text and "Delta" in ev_name:
                chunks.append(text)
    finally:
        aclose = getattr(close_stream, "aclose", None)
        if callable(aclose):
            await aclose()
    return "".join(chunks)


class DreamCursor:
    """Timestamp (UTC epoch seconds) of the last successful Dream batch.

    Persisted at ``<memory_dir>/.dream_cursor``. Files with mtime greater
    than the cursor are candidates for the next Dream run.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._path = memory_dir / ".dream_cursor"

    def load(self) -> float:
        if not self._path.exists():
            return 0.0
        try:
            return float(self._path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return 0.0

    def save(self, ts: float) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(f"{ts}\n", encoding="utf-8")

    def reset(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class DreamResult:
    """Outcome of a Dream run — emitted to logs and receipts."""

    files_considered: int = 0
    files_processed: int = 0
    evidence_status: str = "skipped"  # skipped | ok | error
    apply_status: str = "skipped"  # skipped | ok | error
    evidence_ms: int = 0
    apply_ms: int = 0
    provider_calls: int = 0
    error: str | None = None
    cursor_before: float = 0.0
    cursor_after: float = 0.0
    memory_md_sha_before: str | None = None
    memory_md_sha_after: str | None = None
    input_slimming: str = "off"
    promotion_prompt_chars: int = 0
    dry_run: bool = False
    edit_receipt_path: str | None = None


class Dream:
    """Per-agent Dream runner. Constructed once per cron invocation."""

    def __init__(
        self,
        *,
        workspace: Path,
        provider: Any,
        session_lock: asyncio.Lock | None,
        config: Any,  # DreamConfig — avoid circular import
        agent_id: str = "main",
    ) -> None:
        self.workspace = workspace
        self.memory_dir = workspace / "memory"
        self.memory_md = workspace / "MEMORY.md"
        self.cursor = DreamCursor(self.memory_dir)
        self.provider = provider
        self.session_lock = session_lock
        self.config = config
        self.agent_id = agent_id

    def _emit_log(self, result: DreamResult) -> None:
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        log_dir = self.workspace / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"dream-{self.agent_id}-{today}.jsonl"
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "agent_id": getattr(self, "agent_id", "main"),
            "cursor_before": result.cursor_before,
            "cursor_after": result.cursor_after,
            "files_considered": result.files_considered,
            "files_processed": result.files_processed,
            "evidence_ms": result.evidence_ms,
            "evidence_status": result.evidence_status,
            "apply_ms": result.apply_ms,
            "apply_status": result.apply_status,
            "provider_calls": result.provider_calls,
            "memory_md_sha_before": result.memory_md_sha_before,
            "memory_md_sha_after": result.memory_md_sha_after,
            "input_slimming": result.input_slimming,
            "promotion_prompt_chars": result.promotion_prompt_chars,
            "dry_run": result.dry_run,
            "edit_receipt_path": result.edit_receipt_path,
            "error": result.error,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def _artifact_id(self) -> str:
        import time

        return f"{getattr(self, 'agent_id', 'main')}-{int(time.time() * 1000)}"

    def _workspace_relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def _backup_memory_md(self, artifact_id: str) -> str:
        backup_dir = self.memory_dir / ".dream_backups" / artifact_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / "MEMORY.md"
        backup_path.write_bytes(
            self.memory_md.read_bytes() if self.memory_md.exists() else b""
        )
        return self._workspace_relative(backup_path)

    def pending_candidate_count(self) -> int:
        return len(
            scan_dream_candidates(
                self.workspace,
                cursor=self.cursor.load(),
                max_batch_size=getattr(self.config, "max_batch_size", 20),
                agent_id=getattr(self, "agent_id", "main"),
                quarantine_enabled=getattr(self.config, "evidence_quarantine_enabled", True),
            )
        )

    async def _run_evidence_consolidation(self) -> DreamResult:
        """Evidence-gated consolidation path."""
        import time
        from datetime import UTC, datetime

        result = DreamResult(
            cursor_before=self.cursor.load(),
            memory_md_sha_before=(
                hashlib.sha256(self.memory_md.read_bytes()).hexdigest()
                if self.memory_md.exists()
                else None
            ),
            input_slimming=getattr(self.config, "input_slimming", "off"),
            dry_run=bool(
                getattr(self.config, "preview_mode", False)
                or getattr(self.config, "dry_run", False)
            ),
        )
        raw_candidates = scan_dream_candidates(
            self.workspace,
            cursor=result.cursor_before,
            max_batch_size=getattr(self.config, "max_batch_size", 20),
            agent_id=getattr(self, "agent_id", "main"),
            quarantine_enabled=getattr(self.config, "evidence_quarantine_enabled", True),
        )
        result.files_considered = len(raw_candidates)
        if len(raw_candidates) < getattr(self.config, "min_batch_size", 1):
            result.cursor_after = result.cursor_before
            try:
                self._emit_log(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dream.log_emit_failed", extra={"error": str(exc)})
            return result

        now_iso = datetime.now(UTC).isoformat()
        evidence_start = time.monotonic()
        try:
            store = update_promotion_evidence(
                self.workspace,
                raw_candidates,
                now_iso=now_iso,
                persist=not result.dry_run,
            )
            ranked = rank_promotion_candidates(
                store,
                min_score=getattr(self.config, "evidence_min_score", 0.55),
                negative_recurrence_threshold=getattr(
                    self.config, "evidence_negative_recurrence_threshold", 2
                ),
                min_seen_count=getattr(self.config, "evidence_min_seen_count", 1),
                limit=getattr(self.config, "max_batch_size", 20),
            )
            result.evidence_status = "ok"
            result.evidence_ms = int((time.monotonic() - evidence_start) * 1000)
        except Exception as exc:  # noqa: BLE001
            result.evidence_status = "error"
            result.evidence_ms = int((time.monotonic() - evidence_start) * 1000)
            result.error = f"evidence: {exc}"
            result.cursor_after = result.cursor_before
            return result

        if not ranked:
            max_mtime = max(
                (candidate.source_mtime_ns / 1_000_000_000 for candidate in raw_candidates),
                default=result.cursor_before,
            )
            if not result.dry_run:
                write_evidence_store(self.workspace, store)
                result.files_processed = len(raw_candidates)
                self.cursor.save(max_mtime)
                result.cursor_after = max_mtime
            else:
                result.cursor_after = result.cursor_before
            result.apply_status = "skipped"
            result.edit_receipt_path = write_dream_receipt(
                workspace=self.workspace,
                artifact_id=self._artifact_id(),
                agent_id=getattr(self, "agent_id", "main"),
                dry_run=result.dry_run,
                candidate_paths=[candidate.source_path for candidate in raw_candidates],
                evidence_updated=len(raw_candidates),
                ranked_candidates=[],
                skipped_candidates=[],
                applied=ApplyPromotionResult(),
                memory_md_backup_path="",
                cursor_before=result.cursor_before,
                cursor_after=result.cursor_after,
            )
            try:
                self._emit_log(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dream.log_emit_failed", extra={"error": str(exc)})
            return result

        apply_start = time.monotonic()
        artifact_id = self._artifact_id()
        candidate_paths = [candidate.source_path for candidate in raw_candidates]
        skipped_candidates: list[dict[str, Any]] = []
        memory_backup_path = ""
        try:
            current_memory = (
                self.memory_md.read_text(encoding="utf-8") if self.memory_md.exists() else ""
            )
            prompt = promotion_patch_prompt(current_memory, ranked)
            result.promotion_prompt_chars = len(prompt)
            text = await _run_complete(self.provider, [Message(role="user", content=prompt)], 4096)
            patch = parse_promotion_patch(text, ranked)
            result.provider_calls = 1

            live_candidate_ids: set[str] = set()
            for candidate in ranked:
                rehydrated = rehydrate_candidate(self.workspace, candidate)
                if rehydrated.ok:
                    live_candidate_ids.add(candidate.candidate_id)
                else:
                    reason = rehydrated.reason or "rehydrate_failed"
                    skipped_candidates.append(
                        {"candidate_id": candidate.candidate_id, "reason": reason}
                    )
                    mark_evidence_skipped(store, candidate.candidate_id, reason)

            filtered_operations: list[PromotionPatchOperation] = []
            for operation in patch.operations:
                if operation.op == "skip":
                    filtered_operations.append(operation)
                    continue
                live_ids = [
                    candidate_id
                    for candidate_id in operation.candidate_ids
                    if candidate_id in live_candidate_ids
                ]
                if not live_ids:
                    continue
                operation.candidate_ids = live_ids
                filtered_operations.append(operation)
            filtered_patch = PromotionPatch(operations=filtered_operations)
            if not result.dry_run and getattr(
                self.config, "evidence_curated_writes_enabled", True
            ):
                memory_backup_path = self._backup_memory_md(artifact_id)
            applied = apply_promotion_patch(
                self.workspace,
                filtered_patch,
                dry_run=result.dry_run
                or not getattr(self.config, "evidence_curated_writes_enabled", True),
            )
            if not result.dry_run:
                promoted_ids: list[str] = []
                represented_ids: list[str] = []
                for applied_operation in applied.applied_operations:
                    if applied_operation.get("op") not in {"upsert", "merge"}:
                        continue
                    raw_candidate_ids = applied_operation.get("candidate_ids", [])
                    if not isinstance(raw_candidate_ids, list):
                        continue
                    candidate_ids = [
                        str(candidate_id)
                        for candidate_id in raw_candidate_ids
                        if isinstance(candidate_id, str)
                    ]
                    if applied_operation.get("changed") is True:
                        promoted_ids.extend(candidate_ids)
                    else:
                        represented_ids.extend(candidate_ids)
                promoted_set = set(promoted_ids)
                represented_ids = [
                    candidate_id
                    for candidate_id in represented_ids
                    if candidate_id not in promoted_set
                ]
                mark_evidence_promoted(store, promoted_ids, now_iso)
                mark_evidence_represented(store, represented_ids, "no_curated_change")
                write_evidence_store(self.workspace, store)
                max_mtime = max(
                    (candidate.source_mtime_ns / 1_000_000_000 for candidate in raw_candidates),
                    default=result.cursor_before,
                )
                result.files_processed = len(raw_candidates)
                self.cursor.save(max_mtime)
                result.cursor_after = max_mtime
            else:
                result.cursor_after = result.cursor_before

            result.memory_md_sha_after = (
                hashlib.sha256(self.memory_md.read_bytes()).hexdigest()
                if self.memory_md.exists()
                else None
            )
            result.apply_status = "ok"
            result.apply_ms = int((time.monotonic() - apply_start) * 1000)
            result.edit_receipt_path = write_dream_receipt(
                workspace=self.workspace,
                artifact_id=artifact_id,
                agent_id=getattr(self, "agent_id", "main"),
                dry_run=result.dry_run,
                candidate_paths=candidate_paths,
                evidence_updated=len(raw_candidates),
                ranked_candidates=ranked,
                skipped_candidates=skipped_candidates,
                applied=applied,
                memory_md_backup_path=memory_backup_path,
                cursor_before=result.cursor_before,
                cursor_after=result.cursor_after,
            )
        except Exception as exc:  # noqa: BLE001
            result.apply_status = "error"
            result.apply_ms = int((time.monotonic() - apply_start) * 1000)
            result.error = f"apply: {exc}"
            result.cursor_after = result.cursor_before

        try:
            self._emit_log(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dream.log_emit_failed", extra={"error": str(exc)})
        return result

    async def run(self) -> DreamResult:
        """Run the single evidence-gated Dream consolidation path."""
        return await self._run_evidence_consolidation()
