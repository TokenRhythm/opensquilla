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
from opensquilla.memory.dream.candidates import (
    scan_dream_candidate_batch,
    scan_dream_candidates,
)
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
from opensquilla.memory.file_mutation import (
    get_memory_mutation_lock,
    memory_content_sha256,
)
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


@dataclass(frozen=True)
class DreamCursorPosition:
    mtime_ns: int = 0
    source_path: str = ""

    @property
    def timestamp(self) -> float:
        return self.mtime_ns / 1_000_000_000


class DreamCursor:
    """Stable ``(mtime_ns, source_path)`` cursor of the last successful batch.

    Legacy timestamp-only cursor files remain readable.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._path = memory_dir / ".dream_cursor"

    def load(self) -> float:
        return self.load_position().timestamp

    def load_position(self) -> DreamCursorPosition:
        if not self._path.exists():
            return DreamCursorPosition()
        try:
            text = self._path.read_text(encoding="utf-8").strip()
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                return DreamCursorPosition(mtime_ns=int(float(text) * 1_000_000_000))
            if not isinstance(raw, dict):
                return DreamCursorPosition()
            return DreamCursorPosition(
                mtime_ns=max(0, int(raw.get("mtime_ns") or 0)),
                source_path=str(raw.get("source_path") or ""),
            )
        except (TypeError, ValueError, OSError):
            return DreamCursorPosition()

    def save(self, ts: float) -> None:
        self.save_position(
            DreamCursorPosition(mtime_ns=max(0, int(ts * 1_000_000_000)))
        )

    def save_position(self, position: DreamCursorPosition) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "mtime_ns": position.mtime_ns,
            "source_path": position.source_path,
        }
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)

    def reset(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


def _cursor_position_from_scan(scan: Any) -> DreamCursorPosition:
    mtime_ns = int(getattr(scan, "cursor_high_watermark_ns", 0) or 0)
    if mtime_ns <= 0:
        mtime_ns = int(float(scan.cursor_high_watermark) * 1_000_000_000)
    return DreamCursorPosition(
        mtime_ns=mtime_ns,
        source_path=str(getattr(scan, "cursor_high_watermark_path", "") or ""),
    )


@dataclass
class DreamResult:
    """Outcome of a Dream run — emitted to logs and receipts."""

    files_considered: int = 0
    files_processed: int = 0
    files_skipped_unchanged: int = 0  # D10: content-hash dedup
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
        memory_store: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self.memory_dir = workspace / "memory"
        self.memory_md = workspace / "MEMORY.md"
        self.cursor = DreamCursor(self.memory_dir)
        self.provider = provider
        self.session_lock = session_lock
        self.config = config
        self.agent_id = agent_id
        # D5: optional LongTermMemoryStore for usage stats + constraint types
        self._memory_store = memory_store

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
            "files_skipped_unchanged": result.files_skipped_unchanged,
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
        cursor_position = self.cursor.load_position()
        return len(
            scan_dream_candidates(
                self.workspace,
                cursor=cursor_position.timestamp,
                cursor_position=(
                    cursor_position.mtime_ns,
                    cursor_position.source_path,
                ),
                max_batch_size=getattr(self.config, "max_batch_size", 20),
                agent_id=getattr(self, "agent_id", "main"),
                quarantine_enabled=getattr(self.config, "evidence_quarantine_enabled", True),
            )
        )

    async def _run_evidence_consolidation(self) -> DreamResult:
        """Evidence-gated consolidation path."""
        import time
        from datetime import UTC, datetime

        cursor_position = self.cursor.load_position()
        result = DreamResult(
            cursor_before=cursor_position.timestamp,
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
        # D10: deduplicate only touch-only rewrites of the same source.
        # Identical content in another source is recurrence evidence.
        d10_known_observations: set[tuple[str, str]] | None = None
        existing_evidence_store = None
        try:
            from opensquilla.memory.dream.evidence import load_evidence_store

            existing_evidence_store = load_evidence_store(self.workspace)
            d10_known_observations = {
                (e.source_path, e.content_sha256 or e.snippet_sha256)
                for e in existing_evidence_store.entries.values()
                if e.source_path and (e.content_sha256 or e.snippet_sha256)
            }
        except Exception:  # noqa: BLE001
            pass  # D10 is best-effort; degrade to full scan

        candidate_scan = scan_dream_candidate_batch(
            self.workspace,
            cursor=result.cursor_before,
            max_batch_size=getattr(self.config, "max_batch_size", 20),
            agent_id=getattr(self, "agent_id", "main"),
            quarantine_enabled=getattr(self.config, "evidence_quarantine_enabled", True),
            known_observations=d10_known_observations,
            cursor_position=(
                cursor_position.mtime_ns,
                cursor_position.source_path,
            ),
        )
        raw_candidates = candidate_scan.candidates
        result.files_considered = candidate_scan.files_considered
        result.files_skipped_unchanged = candidate_scan.files_skipped_unchanged
        has_pending_evidence = bool(
            existing_evidence_store is not None
            and any(
                entry.status == "candidate"
                for entry in existing_evidence_store.entries.values()
            )
        )
        if (
            len(raw_candidates) < getattr(self.config, "min_batch_size", 1)
            and not has_pending_evidence
        ):
            if (
                not raw_candidates
                and result.files_skipped_unchanged
                and not result.dry_run
            ):
                self.cursor.save_position(_cursor_position_from_scan(candidate_scan))
                result.cursor_after = candidate_scan.cursor_high_watermark
            else:
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
            # D5: gather usage stats + constraint types for enhanced scoring
            d5_usage_stats: dict[str, dict] | None = None
            d5_constraint_types: dict[str, str] | None = None
            if self._memory_store is not None:
                candidate_paths = list(
                    {
                        entry.source_path
                        for entry in store.entries.values()
                        if entry.status == "candidate" and entry.source_path
                    }
                )
                try:
                    d5_usage_stats = await self._memory_store.get_usage_stats(candidate_paths)
                    d5_constraint_types = await self._memory_store.get_dominant_constraint_types(
                        candidate_paths
                    )
                except Exception:  # noqa: BLE001
                    pass  # D5 is best-effort; degrade to old scoring

            ranked = rank_promotion_candidates(
                store,
                min_score=getattr(self.config, "evidence_min_score", 0.55),
                negative_recurrence_threshold=getattr(
                    self.config, "evidence_negative_recurrence_threshold", 2
                ),
                min_seen_count=getattr(self.config, "evidence_min_seen_count", 1),
                limit=getattr(self.config, "max_batch_size", 20),
                usage_stats=d5_usage_stats,
                constraint_types=d5_constraint_types,
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
            max_mtime = candidate_scan.cursor_high_watermark
            if not result.dry_run:
                write_evidence_store(self.workspace, store)
                result.files_processed = len(raw_candidates)
                self.cursor.save_position(_cursor_position_from_scan(candidate_scan))
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
            expected_memory_sha = memory_content_sha256(current_memory)
            live_candidate_ids: set[str] = set()
            live_ranked = []
            for candidate in ranked:
                rehydrated = rehydrate_candidate(self.workspace, candidate)
                if rehydrated.ok:
                    live_candidate_ids.add(candidate.candidate_id)
                    live_ranked.append(candidate)
                else:
                    reason = rehydrated.reason or "rehydrate_failed"
                    skipped_candidates.append(
                        {"candidate_id": candidate.candidate_id, "reason": reason}
                    )
                    mark_evidence_skipped(store, candidate.candidate_id, reason)

            if live_ranked:
                prompt = promotion_patch_prompt(current_memory, live_ranked)
                result.promotion_prompt_chars = len(prompt)
                text = await _run_complete(
                    self.provider,
                    [Message(role="user", content=prompt)],
                    4096,
                )
                patch = parse_promotion_patch(text, live_ranked)
                result.provider_calls = 1
            else:
                patch = PromotionPatch()

            # The source may change while the provider is running. Revalidate
            # immediately before accepting operations derived from its prompt.
            for candidate in live_ranked:
                rehydrated = rehydrate_candidate(self.workspace, candidate)
                if not rehydrated.ok:
                    live_candidate_ids.discard(candidate.candidate_id)
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
            async with get_memory_mutation_lock(self.workspace):
                applied = apply_promotion_patch(
                    self.workspace,
                    filtered_patch,
                    dry_run=result.dry_run
                    or not getattr(self.config, "evidence_curated_writes_enabled", True),
                    expected_content_sha256=expected_memory_sha,
                )
            if not result.dry_run:
                promoted_ids: list[str] = []
                represented_ids: list[str] = []
                skipped_ids: list[tuple[str, str]] = []
                for applied_operation in applied.applied_operations:
                    if applied_operation.get("op") == "skip":
                        reason = str(applied_operation.get("reason") or "model_skip")
                        raw_candidate_ids = applied_operation.get("candidate_ids", [])
                        if isinstance(raw_candidate_ids, list):
                            skipped_ids.extend(
                                (candidate_id, reason)
                                for candidate_id in raw_candidate_ids
                                if isinstance(candidate_id, str)
                            )
                        continue
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
                for candidate_id, reason in skipped_ids:
                    mark_evidence_skipped(store, candidate_id, reason)
                write_evidence_store(self.workspace, store)
                max_mtime = candidate_scan.cursor_high_watermark
                result.files_processed = len(raw_candidates)
                self.cursor.save_position(_cursor_position_from_scan(candidate_scan))
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
        if self.session_lock is not None:
            async with self.session_lock:
                return await self._run_evidence_consolidation()
        return await self._run_evidence_consolidation()
