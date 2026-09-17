"""Profile import orchestration independent of Gateway and provider routing."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from opensquilla.memory.profile_import.errors import (
    ProfileImportBusyError,
    ProfileImportError,
    ProfileImportInputTooLargeError,
    ProfileImportInvalidOutputError,
    ProfileImportJobNotFoundError,
    ProfileImportModelError,
    ProfileImportPreviewExpiredError,
    ProfileImportStalePreviewError,
    ProfileImportUnavailableError,
    ProfileImportWriteError,
)
from opensquilla.memory.profile_import.files import (
    build_file_plans,
    build_undo_review_file_plans,
    enforce_quotas,
    history_snapshot,
    image_hash,
    public_file_diffs,
    read_text_image,
    reverse_file_plans,
    root_identity_hash,
    sha256_text,
    stable_hash,
    target_path,
)
from opensquilla.memory.profile_import.models import (
    PROMPT_VERSION,
    DecisionCounts,
    DecisionOutcome,
    DecisionTarget,
    FusionCompletion,
    FusionModelRequest,
    FusionOutput,
    ImportStatus,
    InternalFilePlan,
    InternalPreviewRecord,
    ModelIdentity,
    ProfileImportApplyResult,
    ProfileImportInfo,
    ProfileImportJob,
    ProfileImportJobRecord,
    ProfileImportJobStage,
    ProfileImportJobStatus,
    ProfileImportPaths,
    ProfileImportPreview,
    ProfileImportPreviewRequest,
    ProfileImportQuotas,
    ProfileImportReceipt,
    ProfileImportUndoResult,
    RecentProfileImport,
    UndoFileContext,
    UndoReviewContext,
)
from opensquilla.memory.profile_import.parsing import parse_fusion_output
from opensquilla.memory.profile_import.prompts import (
    FUSION_SYSTEM_PROMPT,
    UNDO_SYSTEM_PROMPT,
    render_fusion_user_prompt,
    render_undo_user_prompt,
)
from opensquilla.memory.profile_import.store import ProfileImportStore
from opensquilla.memory.profile_import.transaction import (
    all_after_images_match,
    execute_transaction,
    recover_transaction,
)
from opensquilla.profile_import_io import ProfileOperationLock
from opensquilla.session.tokenizer import estimate_tokens

_LOCKS_GUARD = threading.Lock()
_AGENT_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_IMPORTED_FROM_LINE = re.compile(r"^Imported from:\s*(?P<source>[^<>\r\n]{1,128})\s*$")


def _default_now() -> datetime:
    return datetime.now(UTC)


def _default_profile_lock(path: Path) -> contextlib.AbstractContextManager[object]:
    return ProfileOperationLock(path, timeout=2.0)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _agent_lock(paths: ProfileImportPaths) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), f"{paths.state_dir}:{paths.agent_id}")
    with _LOCKS_GUARD:
        return _AGENT_LOCKS.setdefault(key, asyncio.Lock())


def _root_hash(path: Path) -> str:
    return root_identity_hash(path)


def _declared_source(request: ProfileImportPreviewRequest) -> str | None:
    explicit = (request.declared_source or "").strip()
    if explicit:
        return explicit
    lines = request.raw_text.rstrip().splitlines()
    if not lines:
        return None
    match = _IMPORTED_FROM_LINE.fullmatch(lines[-1].strip())
    if match is None:
        return None
    source = match.group("source").strip()
    return source or None


def _has_applied_import_decision(output: FusionOutput) -> bool:
    return any(
        decision.outcome is DecisionOutcome.APPLIED
        and decision.target is DecisionTarget.IMPORT
        for decision in output.decisions
    )


def _validate_normal_import_output(output: FusionOutput) -> None:
    has_applied_import = _has_applied_import_decision(output)
    has_import_content = bool((output.candidate.import_md or "").strip())
    if has_applied_import and not has_import_content:
        raise ProfileImportInvalidOutputError(
            "profile fusion output applies IMPORT without non-empty IMPORT content"
        )
    if has_import_content and not has_applied_import:
        raise ProfileImportInvalidOutputError(
            "profile fusion output contains IMPORT content without an applied IMPORT decision"
        )


class ProfileImportService:
    """One-agent profile import service with a dependency-injected model call."""

    def __init__(
        self,
        paths: ProfileImportPaths,
        model: ModelIdentity,
        complete: FusionCompletion | None,
        *,
        quotas: ProfileImportQuotas | None = None,
        now: Callable[[], datetime] = _default_now,
        id_factory: Callable[[], str] | None = None,
        profile_lock_factory: Callable[
            [Path], contextlib.AbstractContextManager[object]
        ] = _default_profile_lock,
    ) -> None:
        self.paths = paths
        self.model = model
        self.complete = complete
        self.quotas = quotas or ProfileImportQuotas()
        self._now = now
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._profile_lock_factory = profile_lock_factory
        self.store = ProfileImportStore(paths)

    async def info(self) -> ProfileImportInfo:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                self.store.cleanup_expired_raw(now)
            receipt = self.store.latest_receipt()
            recent = None
            if receipt is not None:
                recent = RecentProfileImport(
                    receipt_id=receipt.receipt_id,
                    batch_id=receipt.batch_id,
                    status=receipt.status,
                    provider=receipt.model_identity.provider,
                    model=receipt.model_identity.model,
                    summary=receipt.summary,
                    applied_at=receipt.applied_at,
                    undone_at=receipt.undone_at,
                    file_count=len(receipt.files),
                    targets=[plan.target for plan in receipt.files],
                    index_status=receipt.index_status,
                )
            draft = self.store.latest_draft_job()
            return ProfileImportInfo(
                available=self.complete is not None,
                provider=self.model.provider,
                model=self.model.model,
                is_loopback=self.model.is_loopback,
                max_raw_bytes=self.quotas.max_raw_bytes,
                recent_import=recent,
                draft_job=(
                    self._public_job(draft, now=now, include_preview=True)
                    if draft is not None and draft.expires_at > now
                    else None
                ),
            )

    async def preview(self, request: ProfileImportPreviewRequest) -> ProfileImportPreview:
        """Compatibility wrapper over the durable background-job primitives."""

        job = await self.prepare_job(request)
        if job.status is ProfileImportJobStatus.READY:
            assert job.preview is not None
            return job.preview
        return await self.run_job(job.job_id)

    async def prepare_job(self, request: ProfileImportPreviewRequest) -> ProfileImportJob:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                self.store.cleanup_expired_raw(now)
            if self.complete is None:
                raise ProfileImportUnavailableError(
                    "the configured default model is unavailable"
                )
            raw_size = len(request.raw_text.encode("utf-8"))
            if raw_size > self.quotas.max_raw_bytes:
                raise ProfileImportInputTooLargeError(
                    f"imported profile exceeds {self.quotas.max_raw_bytes} UTF-8 bytes"
                )

            raw_hash = sha256_text(request.raw_text)
            request_key = f"job:{request.client_request_id}"
            existing = self.store.find_job_by_request(request_key)
            if existing is not None:
                if existing.raw_hash != raw_hash:
                    raise ProfileImportStalePreviewError(
                        "the client request ID is already bound to different imported text"
                    )
                return self._public_job(existing, now=now, include_preview=True)

            (
                _current_user,
                _current_memory,
                user_hash,
                memory_hash,
                _history,
                history_hash,
            ) = self._read_context()
            reuse_key = stable_hash(
                (
                    "import-job",
                    raw_hash,
                    _root_hash(self.paths.agent_workspace_dir),
                    _root_hash(self.paths.memory_workspace_dir),
                    user_hash,
                    memory_hash,
                    history_hash,
                    self.model.provider,
                    self.model.model,
                    PROMPT_VERSION,
                    request.export_prompt_version,
                )
            )
            already_applied = self.store.find_applied_receipt_by_raw_hash(raw_hash)
            if (
                already_applied is not None
                and self._receipt_roots_match(already_applied)
                and self._receipt_applied_context_matches(
                    already_applied,
                    user_hash=user_hash,
                    memory_hash=memory_hash,
                    history_hash=history_hash,
                )
                and all_after_images_match(self.paths, already_applied.files)
            ):
                preview = self._persist_already_applied_preview(
                    request=request,
                    request_key=f"import:{request.client_request_id}",
                    receipt=already_applied,
                    current_user=_current_user,
                    current_memory=_current_memory,
                    history_hash=history_hash,
                    now=now,
                )
                record = ProfileImportJobRecord(
                    job_id=self._new_id(),
                    batch_id=preview.batch_id,
                    agent_id=self.paths.agent_id,
                    status=ProfileImportJobStatus.READY,
                    stage=ProfileImportJobStage.DIFF,
                    created_at=now,
                    updated_at=now,
                    expires_at=preview.expires_at,
                    finished_at=now,
                    raw_hash=raw_hash,
                    reuse_key=reuse_key,
                    client_request_id=request_key,
                    export_prompt_version=request.export_prompt_version,
                    ui_locale=request.ui_locale,
                    declared_source=_declared_source(request),
                    model_identity=already_applied.model_identity,
                    baseline_user_hash=user_hash,
                    baseline_memory_hash=memory_hash,
                    baseline_history_hash=history_hash,
                    agent_workspace_root_hash=_root_hash(
                        self.paths.agent_workspace_dir
                    ),
                    memory_workspace_root_hash=_root_hash(
                        self.paths.memory_workspace_dir
                    ),
                    preview_id=preview.preview_id,
                )
                self.store.save_job(record)
                return self._public_job(record, now=now, include_preview=True)
            reusable = self.store.find_job_by_reuse_key(reuse_key)
            if (
                reusable is not None
                and reusable.status
                not in {
                    ProfileImportJobStatus.APPLIED,
                    ProfileImportJobStatus.DISCARDED,
                }
                and reusable.expires_at > now
            ):
                return self._public_job(reusable, now=now, include_preview=True)

            draft = self.store.latest_draft_job()
            if draft is not None and draft.expires_at > now:
                raise ProfileImportBusyError(
                    "another profile import must be applied or discarded first"
                )

            batch_id = self._new_id()
            record = ProfileImportJobRecord(
                job_id=self._new_id(),
                batch_id=batch_id,
                agent_id=self.paths.agent_id,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(hours=24),
                raw_hash=raw_hash,
                reuse_key=reuse_key,
                client_request_id=request_key,
                export_prompt_version=request.export_prompt_version,
                ui_locale=request.ui_locale,
                declared_source=_declared_source(request),
                model_identity=self.model,
                baseline_user_hash=user_hash,
                baseline_memory_hash=memory_hash,
                baseline_history_hash=history_hash,
                agent_workspace_root_hash=_root_hash(self.paths.agent_workspace_dir),
                memory_workspace_root_hash=_root_hash(self.paths.memory_workspace_dir),
            )
            self.store.write_raw(batch_id, request.raw_text)
            self.store.save_job(record)
            return self._public_job(record, now=now)

    async def run_job(self, job_id: str) -> ProfileImportPreview:
        now = _aware_utc(self._now())
        async with _agent_lock(self.paths):
            record = self.store.load_job(job_id)
            if record.status is ProfileImportJobStatus.READY:
                job = self._public_job(record, now=now, include_preview=True)
                assert job.preview is not None
                return job.preview
            if record.status is not ProfileImportJobStatus.QUEUED:
                raise ProfileImportBusyError(
                    f"profile import job cannot run from {record.status.value}"
                )
            raw_text = self.store.read_raw(record.batch_id)
            current_user, current_memory, user_hash, memory_hash, history, history_hash = (
                self._read_context()
            )
            if (
                user_hash != record.baseline_user_hash
                or memory_hash != record.baseline_memory_hash
                or history_hash != record.baseline_history_hash
            ):
                error = ProfileImportStalePreviewError(
                    "the local profile changed before analysis started"
                )
                self._fail_job(record, error, now=now)
                raise error
            record.status = ProfileImportJobStatus.ANALYZING
            record.stage = ProfileImportJobStage.MODEL
            record.started_at = now
            record.updated_at = now
            record.attempt_count += 1
            record.error_code = None
            record.error_message = None
            self.store.update_job(record)
            request = ProfileImportPreviewRequest(
                raw_text=raw_text,
                ui_locale=record.ui_locale,
                export_prompt_version=record.export_prompt_version,
                client_request_id=record.client_request_id,
                declared_source=record.declared_source,
            )
            user_prompt = self._fit_fusion_prompt(
                request=request,
                batch_id=record.batch_id,
                current_user=current_user,
                current_memory=current_memory,
                history=history,
            )

        try:
            output = await self._complete_output(
                system_prompt=FUSION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                evidence_source=raw_text,
                retry_invalid_response=True,
                validate_normal_import=True,
            )
        except asyncio.CancelledError:
            async with _agent_lock(self.paths):
                current = self.store.load_job(job_id)
                if current.status in {
                    ProfileImportJobStatus.ANALYZING,
                    ProfileImportJobStatus.CANCELLING,
                }:
                    current.status = (
                        ProfileImportJobStatus.CANCELLED
                        if current.status is ProfileImportJobStatus.CANCELLING
                        else ProfileImportJobStatus.INTERRUPTED
                    )
                    current.updated_at = _aware_utc(self._now())
                    current.finished_at = current.updated_at
                    self.store.update_job(current)
            raise
        except ProfileImportError as exc:
            async with _agent_lock(self.paths):
                current = self.store.load_job(job_id)
                self._fail_job(current, exc, now=_aware_utc(self._now()))
            raise

        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            record = self.store.load_job(job_id)
            if record.status is not ProfileImportJobStatus.ANALYZING:
                raise ProfileImportBusyError("profile import job was cancelled")
            try:
                record.stage = ProfileImportJobStage.DIFF
                record.updated_at = now
                self.store.update_job(record)
                plans, planned_user_hash, planned_memory_hash = build_file_plans(
                    self.paths,
                    batch_id=record.batch_id,
                    output=output,
                )
                _history, current_history_hash = history_snapshot(self.paths)
                if (
                    planned_user_hash != record.baseline_user_hash
                    or planned_memory_hash != record.baseline_memory_hash
                    or current_history_hash != record.baseline_history_hash
                ):
                    raise ProfileImportStalePreviewError(
                        "the local profile changed while the model prepared the preview"
                    )
                enforce_quotas(self.paths, plans, self.quotas)
                preview_id = self._new_id()
                preview_record = InternalPreviewRecord(
                    preview_id=preview_id,
                    batch_id=record.batch_id,
                    agent_id=self.paths.agent_id,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                    raw_hash=record.raw_hash,
                    reuse_key=record.reuse_key,
                    client_request_id=(
                        f"import:{record.client_request_id.removeprefix('job:')}"
                    ),
                    export_prompt_version=record.export_prompt_version,
                    prompt_version=PROMPT_VERSION,
                    ui_locale=record.ui_locale,
                    declared_source=record.declared_source,
                    model_identity=record.model_identity,
                    fusion_output=output,
                    candidate_hash=self._candidate_hash(plans, output),
                    baseline_user_hash=record.baseline_user_hash,
                    baseline_memory_hash=record.baseline_memory_hash,
                    baseline_history_hash=record.baseline_history_hash,
                    agent_workspace_root_hash=record.agent_workspace_root_hash,
                    memory_workspace_root_hash=record.memory_workspace_root_hash,
                    files=plans,
                )
                self.store.save_preview(preview_record)
                record.status = ProfileImportJobStatus.READY
                record.preview_id = preview_id
                record.updated_at = now
                record.finished_at = now
                self.store.update_job(record)
                return self._public_preview(preview_record, now=now)
            except ProfileImportError as exc:
                self._fail_job(record, exc, now=now)
                raise

    async def job_status(self, job_id: str) -> ProfileImportJob:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            try:
                record = self.store.load_job(job_id)
            except ProfileImportError as exc:
                raise ProfileImportJobNotFoundError(
                    "profile import job is no longer available"
                ) from exc
            return self._public_job(record, now=now, include_preview=True)

    async def retry_job(
        self,
        job_id: str,
        client_request_id: str,
    ) -> ProfileImportJob:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            record = self.store.load_job(job_id)
            if client_request_id in record.retry_request_ids:
                return self._public_job(record, now=now, include_preview=True)
            if record.status not in {
                ProfileImportJobStatus.CANCELLED,
                ProfileImportJobStatus.INTERRUPTED,
                ProfileImportJobStatus.FAILED,
            }:
                raise ProfileImportBusyError(
                    f"profile import job cannot retry from {record.status.value}"
                )
            self.store.read_raw(record.batch_id)
            (
                _user,
                _memory,
                user_hash,
                memory_hash,
                _history,
                history_hash,
            ) = self._read_context()
            record.status = ProfileImportJobStatus.QUEUED
            record.stage = ProfileImportJobStage.READING
            record.updated_at = now
            record.expires_at = now + timedelta(hours=24)
            record.finished_at = None
            record.started_at = None
            record.error_code = None
            record.error_message = None
            record.model_identity = self.model
            record.baseline_user_hash = user_hash
            record.baseline_memory_hash = memory_hash
            record.baseline_history_hash = history_hash
            record.agent_workspace_root_hash = _root_hash(
                self.paths.agent_workspace_dir
            )
            record.memory_workspace_root_hash = _root_hash(
                self.paths.memory_workspace_dir
            )
            record.reuse_key = stable_hash(
                (
                    "import-job",
                    record.raw_hash,
                    record.agent_workspace_root_hash,
                    record.memory_workspace_root_hash,
                    user_hash,
                    memory_hash,
                    history_hash,
                    self.model.provider,
                    self.model.model,
                    PROMPT_VERSION,
                    record.export_prompt_version,
                )
            )
            record.retry_request_ids.append(client_request_id)
            self.store.save_job(record)
            return self._public_job(record, now=now)

    async def request_cancel(self, job_id: str) -> ProfileImportJob:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            record = self.store.load_job(job_id)
            if record.status is ProfileImportJobStatus.READY:
                return self._public_job(record, now=now, include_preview=True)
            if record.status in {
                ProfileImportJobStatus.CANCELLED,
                ProfileImportJobStatus.INTERRUPTED,
                ProfileImportJobStatus.FAILED,
            }:
                return self._public_job(record, now=now)
            if record.status is ProfileImportJobStatus.QUEUED:
                record.status = ProfileImportJobStatus.CANCELLED
                record.finished_at = now
            elif record.status is ProfileImportJobStatus.ANALYZING:
                record.status = ProfileImportJobStatus.CANCELLING
            record.updated_at = now
            self.store.update_job(record)
            return self._public_job(record, now=now)

    async def finish_cancel(self, job_id: str) -> ProfileImportJob:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            record = self.store.load_job(job_id)
            if record.status is ProfileImportJobStatus.CANCELLING:
                record.status = ProfileImportJobStatus.CANCELLED
                record.updated_at = now
                record.finished_at = now
                self.store.update_job(record)
            return self._public_job(record, now=now, include_preview=True)

    async def interrupt_job(self, job_id: str) -> ProfileImportJob:
        """Persist a gateway-shutdown interruption without turning it into user cancellation."""

        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            record = self.store.load_job(job_id)
            if record.status in {
                ProfileImportJobStatus.QUEUED,
                ProfileImportJobStatus.ANALYZING,
                ProfileImportJobStatus.CANCELLING,
            }:
                record.status = ProfileImportJobStatus.INTERRUPTED
                record.error_code = "MEMORY_IMPORT_INTERRUPTED"
                record.error_message = "profile import was interrupted by gateway shutdown"
                record.updated_at = now
                record.finished_at = now
                self.store.update_job(record)
            return self._public_job(record, now=now, include_preview=True)

    async def discard_job(self, job_id: str) -> None:
        async with _agent_lock(self.paths):
            record = self.store.load_job(job_id)
            if record.status in {
                ProfileImportJobStatus.ANALYZING,
                ProfileImportJobStatus.CANCELLING,
            }:
                raise ProfileImportBusyError("cancel the profile import before discarding it")
            if record.status is ProfileImportJobStatus.APPLIED:
                raise ProfileImportWriteError(
                    "an applied profile import cannot be discarded"
                )
            record.status = ProfileImportJobStatus.DISCARDED
            record.updated_at = _aware_utc(self._now())
            self.store.update_job(record)
            self.store.purge_job(record)

    async def recover_jobs(self) -> int:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            recovered = 0
            for record in self.store.iter_jobs():
                if (
                    record.status is ProfileImportJobStatus.READY
                    and record.preview_id is not None
                ):
                    preview = self.store.load_preview(record.preview_id)
                    if preview.status is ImportStatus.APPLIED:
                        record.status = ProfileImportJobStatus.APPLIED
                        record.updated_at = now
                        record.finished_at = preview.applied_at or now
                        self.store.update_job(record)
                        recovered += 1
                        continue
                if record.status in {
                    ProfileImportJobStatus.QUEUED,
                    ProfileImportJobStatus.ANALYZING,
                    ProfileImportJobStatus.CANCELLING,
                }:
                    record.status = ProfileImportJobStatus.INTERRUPTED
                    record.error_code = "MEMORY_IMPORT_INTERRUPTED"
                    record.error_message = "profile import was interrupted by a gateway restart"
                    record.updated_at = now
                    record.finished_at = now
                    self.store.update_job(record)
                    recovered += 1
            return recovered

    async def apply(
        self,
        preview_id: str,
        candidate_hash: str,
        idempotency_key: str,
    ) -> ProfileImportApplyResult:
        if not idempotency_key:
            raise ProfileImportWriteError("an idempotency key is required")
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                self._recover_locked(now)
                record = self.store.load_preview(preview_id)
                self._assert_preview_roots(record)
                if record.candidate_hash != candidate_hash:
                    raise ProfileImportStalePreviewError("profile import candidate hash changed")
                if record.status is ImportStatus.APPLIED:
                    receipt = self._receipt_for_applied_preview(record)
                    if record.operation == "import" and receipt.status == "undone":
                        raise ProfileImportStalePreviewError(
                            "this profile import was undone; generate a new preview"
                        )
                    if record.operation == "import":
                        receipt = self._repair_applied_context_if_safe(receipt)
                    self.store.delete_raw(record.batch_id)
                    result = self._apply_result(record, receipt, status="alreadyApplied")
                    self._mark_job_applied(record.preview_id, now=now)
                    return result
                if record.status is ImportStatus.DISCARDED or record.expires_at <= now:
                    raise ProfileImportPreviewExpiredError("profile import preview has expired")

                self._assert_import_preview_consistent(record)

                key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                if (
                    record.idempotency_key_hash is not None
                    and record.idempotency_key_hash != key_hash
                ):
                    raise ProfileImportStalePreviewError(
                        "profile import preview is already bound to another apply request"
                    )
                record.idempotency_key_hash = key_hash
                self.store.update_preview(record)
                self._assert_context_baseline(record)
                enforce_quotas(self.paths, record.files, self.quotas)

                if record.operation == "undo_review":
                    return self._apply_undo_review(record, now=now)
                result = self._apply_import(record, now=now)
                self._mark_job_applied(record.preview_id, now=now)
                return result

    async def undo(
        self,
        receipt_id: str,
        client_request_id: str,
    ) -> ProfileImportUndoResult:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                self._recover_locked(now)
                receipt = self.store.load_receipt(receipt_id)
                self._assert_receipt_roots(receipt)
                if receipt.status == "undone":
                    return ProfileImportUndoResult(
                        status="alreadyUndone",
                        receipt_id=receipt.receipt_id,
                        index_status="pending",
                    )
                if all_after_images_match(self.paths, receipt.files):
                    self._exact_undo(receipt, client_request_id=client_request_id, now=now)
                    return ProfileImportUndoResult(
                        status="undone",
                        receipt_id=receipt.receipt_id,
                        index_status="pending",
                    )

            context = self._undo_review_context(receipt)
            preview = None
            if self.complete is not None:
                preview = await self._create_undo_review_preview(
                    receipt,
                    context=context,
                    client_request_id=client_request_id,
                    now=now,
                )
            return ProfileImportUndoResult(
                status="reviewRequired",
                receipt_id=receipt.receipt_id,
                review_context=context,
                preview=preview,
            )

    async def discard(self, preview_id: str) -> None:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                self._recover_locked(now)
                record = self.store.load_preview(preview_id)
                if record.status is ImportStatus.APPLIED:
                    raise ProfileImportWriteError(
                        "an applied profile import cannot be discarded"
                    )
                # A no-change apply saves its receipt before preview metadata.
                # If the process or metadata write failed in that small window,
                # discard must finish the durable apply rather than deleting
                # its receipt and leaving a dangling locator.
                if self.store.load_receipt_by_batch(record.batch_id) is not None:
                    self._apply_import(record, now=now)
                    return
                record.status = ImportStatus.DISCARDED
                job = self.store.find_job_by_preview(record.preview_id)
                if job is not None:
                    job.status = ProfileImportJobStatus.DISCARDED
                    job.updated_at = now
                    job.finished_at = now
                    self.store.update_job(job)
                    self.store.purge_job(job)
                else:
                    self.store.purge_preview(record)

    async def recover(self) -> list[str]:
        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                recovered = self._recover_locked(now)
        await self.recover_jobs()
        return recovered

    async def set_index_status(
        self,
        receipt_id: str,
        status: Literal["ready", "pending"],
    ) -> None:
        """Persist derived-index health without changing receipt lifecycle state."""

        async with _agent_lock(self.paths):
            now = _aware_utc(self._now())
            with self._operation_lock():
                self._recover_locked(now)
                receipt = self.store.load_receipt(receipt_id)
                if receipt.index_status != status:
                    self.store.save_receipt(
                        receipt.model_copy(update={"index_status": status})
                    )

    def _read_context(
        self,
    ) -> tuple[str, str, str, str, list[dict[str, str]], str]:
        user_exists, user, _user_mode = read_text_image(
            self.paths.agent_workspace_dir, self.paths.user_path
        )
        memory_exists, memory, _memory_mode = read_text_image(
            self.paths.memory_workspace_dir, self.paths.memory_path
        )
        history, history_hash = history_snapshot(self.paths)
        return (
            user,
            memory,
            image_hash(exists=user_exists, content=user),
            image_hash(exists=memory_exists, content=memory),
            history,
            history_hash,
        )

    def _fit_fusion_prompt(
        self,
        *,
        request: ProfileImportPreviewRequest,
        batch_id: str,
        current_user: str,
        current_memory: str,
        history: list[dict[str, str]],
    ) -> str:
        memory_source = (
            "workspace"
            if self.paths.memory_workspace_dir == self.paths.agent_workspace_dir
            else "state"
        )
        selected: list[dict[str, str]] = []
        base = render_fusion_user_prompt(
            imported_profile=request.raw_text,
            current_user_md=current_user,
            current_memory_md=current_memory,
            import_history=[],
            omitted_history_count=len(history),
            current_date=_aware_utc(self._now()).date(),
            ui_locale=request.ui_locale,
            batch_id=batch_id,
            memory_source=memory_source,
        )
        schema = FusionOutput.model_json_schema()
        if not self._request_fits(
            system_prompt=FUSION_SYSTEM_PROMPT,
            user_prompt=base,
            response_schema=schema,
        ):
            raise ProfileImportInputTooLargeError(
                "the complete imported profile and current profile exceed the model input limit"
            )
        prompt = base
        for index, entry in enumerate(history):
            trial = render_fusion_user_prompt(
                imported_profile=request.raw_text,
                current_user_md=current_user,
                current_memory_md=current_memory,
                import_history=[*selected, entry],
                omitted_history_count=len(history) - index - 1,
                current_date=_aware_utc(self._now()).date(),
                ui_locale=request.ui_locale,
                batch_id=batch_id,
                memory_source=memory_source,
            )
            if not self._request_fits(
                system_prompt=FUSION_SYSTEM_PROMPT,
                user_prompt=trial,
                response_schema=schema,
            ):
                break
            selected.append(entry)
            prompt = trial
        return prompt

    def _request_fits(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, object],
    ) -> bool:
        if len(user_prompt.encode("utf-8")) > self.quotas.max_prompt_bytes:
            return False
        if not self.quotas.max_request_tokens:
            return True
        schema_text = json.dumps(
            response_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # Keep chunks below the tokenizer's fast-estimate cutoff so a locally
        # available tokenizer is used even for the 256 KiB import ceiling.
        material = (system_prompt, user_prompt, schema_text)
        estimated = 32 + sum(
            estimate_tokens(value[offset : offset + 50_000])
            for value in material
            for offset in range(0, max(1, len(value)), 50_000)
        )
        return estimated <= self.quotas.max_request_tokens

    async def _complete_output(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        evidence_source: str,
        retry_invalid_response: bool = False,
        validate_normal_import: bool = False,
    ) -> FusionOutput:
        if self.complete is None:
            raise ProfileImportUnavailableError("the configured default model is unavailable")
        schema = FusionOutput.model_json_schema()
        initial = FusionModelRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=schema,
            identity=self.model,
        )
        if not self._request_fits(
            system_prompt=initial.system_prompt,
            user_prompt=initial.user_prompt,
            response_schema=initial.response_schema,
        ):
            raise ProfileImportInputTooLargeError(
                "the complete profile import request exceeds the model input limit"
            )

        def parse_and_validate(raw_response: str) -> FusionOutput:
            output = parse_fusion_output(raw_response, imported_profile=evidence_source)
            if validate_normal_import:
                _validate_normal_import_output(output)
            return output

        raw = await self._call_model(initial)
        try:
            return parse_and_validate(raw)
        except ProfileImportInvalidOutputError:
            if not retry_invalid_response:
                raise
        # A provider can satisfy the transport contract while returning a
        # malformed structured result. Keep that provider noise inside the
        # import operation: retry the identical bounded request once, then run
        # the same schema and evidence validation again. Do not retry provider
        # failures, oversized/non-text responses, or the undo flow.
        retry_raw = await self._call_model(initial)
        return parse_and_validate(retry_raw)

    async def _call_model(self, request: FusionModelRequest) -> str:
        assert self.complete is not None
        try:
            value = await self.complete(request)
        except ProfileImportError:
            raise
        except Exception as exc:
            raise ProfileImportModelError("the configured default model call failed") from exc
        if not isinstance(value, str):
            raise ProfileImportModelError("the configured default model returned no text")
        if len(value.encode("utf-8")) > self.quotas.max_prompt_bytes:
            raise ProfileImportInvalidOutputError("profile fusion output exceeds the size limit")
        return value

    def _assert_context_baseline(self, record: InternalPreviewRecord) -> None:
        self._assert_preview_roots(record)
        user_exists, user, _mode = read_text_image(
            self.paths.agent_workspace_dir, self.paths.user_path
        )
        memory_exists, memory, _memory_mode = read_text_image(
            self.paths.memory_workspace_dir, self.paths.memory_path
        )
        _history, history_hash = history_snapshot(self.paths)
        if (
            image_hash(exists=user_exists, content=user) != record.baseline_user_hash
            or image_hash(exists=memory_exists, content=memory) != record.baseline_memory_hash
            or history_hash != record.baseline_history_hash
        ):
            raise ProfileImportStalePreviewError(
                "the local profile changed after this import preview"
            )

    def _assert_import_preview_consistent(self, record: InternalPreviewRecord) -> None:
        if record.operation != "import":
            return
        has_applied_import = _has_applied_import_decision(record.fusion_output)
        has_import_plan = any(
            plan.target is DecisionTarget.IMPORT for plan in record.files
        )
        if has_applied_import != has_import_plan:
            raise ProfileImportStalePreviewError(
                "profile import preview is incomplete; generate a new preview before applying"
            )

    def _apply_import(
        self,
        record: InternalPreviewRecord,
        *,
        now: datetime,
    ) -> ProfileImportApplyResult:
        existing_receipt = self.store.load_receipt_by_batch(record.batch_id)
        if existing_receipt is not None:
            if (
                existing_receipt.preview_id != record.preview_id
                or existing_receipt.candidate_hash != record.candidate_hash
                or existing_receipt.files
                or record.files
                or existing_receipt.status != "applied"
            ):
                raise ProfileImportWriteError(
                    "stored profile import receipt does not match its preview"
                )
            # Repair a crash after the no-change receipt became durable but
            # before preview metadata (or its receipt locator) was finalized.
            self.store.save_receipt(existing_receipt)
            record.status = ImportStatus.APPLIED
            record.receipt_id = existing_receipt.receipt_id
            record.applied_at = existing_receipt.applied_at
            self.store.update_preview(record)
            self.store.delete_raw(record.batch_id)
            return self._apply_result(
                record,
                existing_receipt,
                status="alreadyApplied",
            )

        receipt = ProfileImportReceipt(
            receipt_id=self._new_id(),
            preview_id=record.preview_id,
            batch_id=record.batch_id,
            agent_id=record.agent_id,
            source_type="profile_text",
            raw_hash=record.raw_hash,
            declared_source=record.declared_source,
            export_prompt_version=record.export_prompt_version,
            prompt_version=record.prompt_version,
            ui_locale=record.ui_locale,
            model_identity=record.model_identity,
            summary=record.fusion_output.summary,
            decisions=record.fusion_output.decisions,
            files=record.files,
            candidate_hash=record.candidate_hash,
            baseline_user_hash=record.baseline_user_hash,
            baseline_memory_hash=record.baseline_memory_hash,
            baseline_history_hash=record.baseline_history_hash,
            agent_workspace_root_hash=record.agent_workspace_root_hash,
            memory_workspace_root_hash=record.memory_workspace_root_hash,
            applied_at=now,
        )
        if not record.files:
            receipt = receipt.model_copy(
                update={
                    "applied_user_hash": record.baseline_user_hash,
                    "applied_memory_hash": record.baseline_memory_hash,
                    "applied_history_hash": record.baseline_history_hash,
                    "index_status": "ready",
                }
            )
            self.store.save_receipt(receipt)
            record.status = ImportStatus.APPLIED
            record.receipt_id = receipt.receipt_id
            record.applied_at = now
            self.store.update_preview(record)
            self.store.delete_raw(record.batch_id)
            return self._apply_result(record, receipt, status="noChanges")
        execute_transaction(
            paths=self.paths,
            store=self.store,
            preview=record,
            receipt=receipt,
            plans=record.files,
            operation="profile-import-apply",
            now=now,
        )
        receipt = self._repair_applied_context_if_safe(receipt)
        self.store.delete_raw(record.batch_id)
        return self._apply_result(record, receipt, status="applied")

    def _apply_undo_review(
        self,
        record: InternalPreviewRecord,
        *,
        now: datetime,
    ) -> ProfileImportApplyResult:
        if record.source_receipt_id is None:
            raise ProfileImportWriteError("undo review preview has no source receipt")
        original = self.store.load_receipt(record.source_receipt_id)
        if original.status == "undone":
            record.status = ImportStatus.APPLIED
            record.receipt_id = original.receipt_id
            record.applied_at = original.undone_at or now
            self.store.update_preview(record)
            return self._apply_result(record, original, status="alreadyApplied")
        updated = original.model_copy(
            update={
                "status": "undone",
                "undone_at": now,
                "index_status": "pending",
            }
        )
        if record.client_request_id not in updated.undo_client_request_ids:
            updated.undo_client_request_ids.append(record.client_request_id)
        if record.files:
            execute_transaction(
                paths=self.paths,
                store=self.store,
                preview=record,
                receipt=updated,
                plans=record.files,
                operation="profile-import-undo",
                now=now,
            )
        else:
            self.store.save_receipt(updated)
        record.status = ImportStatus.APPLIED
        record.receipt_id = updated.receipt_id
        record.applied_at = now
        self.store.update_preview(record)
        return self._apply_result(record, updated, status="applied")

    def _exact_undo(
        self,
        receipt: ProfileImportReceipt,
        *,
        client_request_id: str,
        now: datetime,
    ) -> None:
        updated = receipt.model_copy(
            update={
                "status": "undone",
                "undone_at": now,
                "index_status": "pending",
                "undo_client_request_ids": [
                    *receipt.undo_client_request_ids,
                    *(
                        [client_request_id]
                        if client_request_id not in receipt.undo_client_request_ids
                        else []
                    ),
                ],
            }
        )
        if not receipt.files:
            self.store.save_receipt(updated)
            return
        preview = self.store.load_preview(receipt.preview_id)
        execute_transaction(
            paths=self.paths,
            store=self.store,
            preview=preview,
            receipt=updated,
            plans=reverse_file_plans(receipt.files),
            operation="profile-import-undo",
            now=now,
        )

    def _undo_review_context(self, receipt: ProfileImportReceipt) -> UndoReviewContext:
        files: list[UndoFileContext] = []
        for plan in receipt.files:
            root, path = target_path(self.paths, plan)
            exists, content, _mode = read_text_image(root, path)
            files.append(
                UndoFileContext(
                    target=plan.target,
                    relative_path=plan.relative_path,
                    existed_before=plan.before_exists,
                    before_content=plan.before_content,
                    imported_content=plan.after_content,
                    current_exists=exists,
                    current_content=content,
                )
            )
        return UndoReviewContext(
            batch_id=receipt.batch_id,
            reason="local profile files changed after this import",
            current_files=files,
            original_files=files,
        )

    async def _create_undo_review_preview(
        self,
        receipt: ProfileImportReceipt,
        *,
        context: UndoReviewContext,
        client_request_id: str,
        now: datetime,
    ) -> ProfileImportPreview:
        request_key = f"undo:{receipt.receipt_id}:{client_request_id}"
        existing = self.store.find_preview_by_request(request_key)
        if existing is not None:
            return self._public_preview(existing, now=now)
        (
            current_user,
            current_memory,
            initial_user_hash,
            initial_memory_hash,
            _history,
            initial_history_hash,
        ) = self._read_context()
        prompt_files = [
            {
                "target": item.target.value,
                "before_import": item.before_content if item.existed_before else None,
                "after_import": item.imported_content,
                "current": item.current_content if item.current_exists else None,
            }
            for item in context.current_files
        ]
        user_prompt = render_undo_user_prompt(
            receipt_id=receipt.receipt_id,
            current_date=now.date(),
            ui_locale=receipt.ui_locale,
            current_user_md=current_user,
            current_memory_md=current_memory,
            files=prompt_files,
        )
        if len(user_prompt.encode("utf-8")) > self.quotas.max_prompt_bytes:
            raise ProfileImportInputTooLargeError(
                "the current profile and undo evidence exceed the model input limit"
            )
        output = await self._complete_output(
            system_prompt=UNDO_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            evidence_source="",
        )
        if output.decisions:
            raise ProfileImportInvalidOutputError("undo candidate must return no decisions")
        batch_id = self._new_id()
        preview_id = self._new_id()
        plans, user_hash, memory_hash = build_undo_review_file_plans(
            self.paths,
            receipt=receipt,
            output=output,
        )
        _history, history_hash = history_snapshot(self.paths)
        if (
            user_hash != initial_user_hash
            or memory_hash != initial_memory_hash
            or history_hash != initial_history_hash
        ):
            raise ProfileImportStalePreviewError(
                "the local profile changed while the model prepared the undo preview"
            )
        enforce_quotas(self.paths, plans, self.quotas)
        candidate_hash = self._candidate_hash(plans, output)
        reuse_key = stable_hash(
            (
                "undo_review",
                receipt.receipt_id,
                user_hash,
                memory_hash,
                history_hash,
                self.model.provider,
                self.model.model,
                PROMPT_VERSION,
            )
        )
        record = InternalPreviewRecord(
            preview_id=preview_id,
            batch_id=batch_id,
            agent_id=self.paths.agent_id,
            operation="undo_review",
            source_receipt_id=receipt.receipt_id,
            created_at=now,
            expires_at=now + timedelta(hours=24),
            raw_hash=receipt.raw_hash,
            reuse_key=reuse_key,
            client_request_id=request_key,
            export_prompt_version=receipt.export_prompt_version,
            prompt_version=PROMPT_VERSION,
            ui_locale=receipt.ui_locale,
            declared_source=receipt.declared_source,
            model_identity=self.model,
            fusion_output=output,
            candidate_hash=candidate_hash,
            baseline_user_hash=user_hash,
            baseline_memory_hash=memory_hash,
            baseline_history_hash=history_hash,
            agent_workspace_root_hash=_root_hash(self.paths.agent_workspace_dir),
            memory_workspace_root_hash=_root_hash(self.paths.memory_workspace_dir),
            files=plans,
        )
        self.store.save_preview(record)
        return self._public_preview(record, now=now)

    def _persist_already_applied_preview(
        self,
        *,
        request: ProfileImportPreviewRequest,
        request_key: str,
        receipt: ProfileImportReceipt,
        current_user: str,
        current_memory: str,
        history_hash: str,
        now: datetime,
    ) -> ProfileImportPreview:
        from opensquilla.memory.profile_import.models import FusionCandidate

        batch_id = self._new_id()
        preview_id = self._new_id()
        output = FusionOutput(
            schema_version=1,
            candidate=FusionCandidate(
                user_md=current_user,
                memory_md=current_memory,
                import_md=None,
            ),
            decisions=[],
            summary=receipt.summary,
        )
        user_exists, _user, _mode = read_text_image(
            self.paths.agent_workspace_dir, self.paths.user_path
        )
        memory_exists, _memory, _memory_mode = read_text_image(
            self.paths.memory_workspace_dir, self.paths.memory_path
        )
        record = InternalPreviewRecord(
            preview_id=preview_id,
            batch_id=batch_id,
            agent_id=self.paths.agent_id,
            status=ImportStatus.APPLIED,
            created_at=now,
            expires_at=now + timedelta(hours=24),
            raw_hash=receipt.raw_hash,
            reuse_key=stable_hash(("already_applied", receipt.receipt_id, history_hash)),
            client_request_id=request_key,
            export_prompt_version=request.export_prompt_version,
            prompt_version=receipt.prompt_version,
            ui_locale=request.ui_locale,
            declared_source=receipt.declared_source or _declared_source(request),
            model_identity=receipt.model_identity,
            fusion_output=output,
            candidate_hash=stable_hash(("already_applied", receipt.candidate_hash)),
            baseline_user_hash=image_hash(exists=user_exists, content=current_user),
            baseline_memory_hash=image_hash(exists=memory_exists, content=current_memory),
            baseline_history_hash=history_hash,
            agent_workspace_root_hash=_root_hash(self.paths.agent_workspace_dir),
            memory_workspace_root_hash=_root_hash(self.paths.memory_workspace_dir),
            files=[],
            receipt_id=receipt.receipt_id,
            applied_at=receipt.applied_at,
        )
        self.store.save_preview(record)
        return self._public_preview(record, now=now)

    def _receipt_for_applied_preview(
        self,
        record: InternalPreviewRecord,
    ) -> ProfileImportReceipt:
        if record.receipt_id is None:
            raise ProfileImportWriteError("applied profile import has no receipt")
        return self.store.load_receipt(record.receipt_id)

    def _apply_result(
        self,
        record: InternalPreviewRecord,
        receipt: ProfileImportReceipt,
        *,
        status: Literal["applied", "alreadyApplied", "noChanges"],
    ) -> ProfileImportApplyResult:
        return ProfileImportApplyResult(
            status=status,
            receipt_id=receipt.receipt_id,
            batch_id=record.batch_id,
            index_status=receipt.index_status,
            applied_at=receipt.undone_at or receipt.applied_at,
        )

    def _public_preview(
        self,
        record: InternalPreviewRecord,
        *,
        now: datetime,
    ) -> ProfileImportPreview:
        if record.status is ImportStatus.DISCARDED or (
            record.status is ImportStatus.PREVIEW and record.expires_at <= now
        ):
            raise ProfileImportPreviewExpiredError("profile import preview has expired")
        counts = DecisionCounts()
        for decision in record.fusion_output.decisions:
            if decision.outcome is DecisionOutcome.APPLIED:
                counts.applied += 1
            elif decision.outcome is DecisionOutcome.DUPLICATE:
                counts.duplicate += 1
            else:
                counts.unresolved += 1
        return ProfileImportPreview(
            preview_id=record.preview_id,
            batch_id=record.batch_id,
            candidate_hash=record.candidate_hash,
            provider=record.model_identity.provider,
            model=record.model_identity.model,
            summary=record.fusion_output.summary,
            decision_counts=counts,
            files=public_file_diffs(record.files),
            no_changes=not record.files,
            expires_at=record.expires_at,
        )

    def _public_job(
        self,
        record: ProfileImportJobRecord,
        *,
        now: datetime,
        include_preview: bool = False,
    ) -> ProfileImportJob:
        preview = None
        if include_preview and record.preview_id is not None:
            try:
                preview_record = self.store.load_preview(record.preview_id)
                preview = self._public_preview(preview_record, now=now)
            except ProfileImportPreviewExpiredError:
                preview = None
        return ProfileImportJob(
            job_id=record.job_id,
            batch_id=record.batch_id,
            status=record.status,
            stage=record.stage,
            provider=record.model_identity.provider,
            model=record.model_identity.model,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            attempt_count=record.attempt_count,
            preview_id=record.preview_id,
            error_code=record.error_code,
            error_message=record.error_message,
            can_retry=record.status
            in {
                ProfileImportJobStatus.CANCELLED,
                ProfileImportJobStatus.INTERRUPTED,
                ProfileImportJobStatus.FAILED,
            },
            preview=preview,
        )

    def _fail_job(
        self,
        record: ProfileImportJobRecord,
        error: ProfileImportError,
        *,
        now: datetime,
    ) -> None:
        record.status = ProfileImportJobStatus.FAILED
        record.error_code = error.code
        record.error_message = str(error)[:2048]
        record.updated_at = now
        record.finished_at = now
        self.store.update_job(record)

    def _mark_job_applied(self, preview_id: str, *, now: datetime) -> None:
        job = self.store.find_job_by_preview(preview_id)
        if job is None:
            return
        job.status = ProfileImportJobStatus.APPLIED
        job.updated_at = now
        job.finished_at = now
        self.store.update_job(job)

    def _candidate_hash(self, plans: list[InternalFilePlan], output: FusionOutput) -> str:
        return stable_hash(
            [
                self.model.provider,
                self.model.model,
                *(
                    value
                    for plan in sorted(plans, key=lambda item: item.target.value)
                    for value in (plan.target.value, plan.relative_path, plan.after_hash)
                ),
                output.candidate.user_md,
                output.candidate.memory_md,
                output.candidate.import_md or "",
            ]
        )

    def _assert_preview_roots(self, record: InternalPreviewRecord) -> None:
        if (
            record.agent_workspace_root_hash
            and record.agent_workspace_root_hash
            != _root_hash(self.paths.agent_workspace_dir)
        ) or (
            record.memory_workspace_root_hash
            and record.memory_workspace_root_hash
            != _root_hash(self.paths.memory_workspace_dir)
        ):
            raise ProfileImportStalePreviewError(
                "the profile storage roots changed after this import preview"
            )

    def _assert_receipt_roots(self, receipt: ProfileImportReceipt) -> None:
        if not self._receipt_roots_match(receipt):
            raise ProfileImportStalePreviewError(
                "the profile storage roots changed after this import was applied"
            )

    def _receipt_roots_match(self, receipt: ProfileImportReceipt) -> bool:
        return not (
            (
                receipt.agent_workspace_root_hash
                and receipt.agent_workspace_root_hash
                != _root_hash(self.paths.agent_workspace_dir)
            )
            or (
                receipt.memory_workspace_root_hash
                and receipt.memory_workspace_root_hash
                != _root_hash(self.paths.memory_workspace_dir)
            )
        )

    def _receipt_applied_context_matches(
        self,
        receipt: ProfileImportReceipt,
        *,
        user_hash: str,
        memory_hash: str,
        history_hash: str,
    ) -> bool:
        return bool(
            receipt.applied_user_hash
            and receipt.applied_memory_hash
            and receipt.applied_history_hash
            and receipt.applied_user_hash == user_hash
            and receipt.applied_memory_hash == memory_hash
            and receipt.applied_history_hash == history_hash
        )

    def _repair_applied_context_if_safe(
        self,
        receipt: ProfileImportReceipt,
    ) -> ProfileImportReceipt:
        """Fill crash-missing applied hashes only when the whole context still matches."""

        if receipt.status != "applied" or (
            receipt.applied_user_hash
            and receipt.applied_memory_hash
            and receipt.applied_history_hash
        ):
            return receipt
        if (
            not receipt.baseline_user_hash
            or not receipt.baseline_memory_hash
            or not receipt.baseline_history_hash
            or not self._receipt_roots_match(receipt)
            or not all_after_images_match(self.paths, receipt.files)
        ):
            return receipt

        (
            _current_user,
            _current_memory,
            user_hash,
            memory_hash,
            history,
            history_hash,
        ) = self._read_context()
        plans = {plan.target: plan for plan in receipt.files}
        expected_user_hash = (
            plans[DecisionTarget.USER].after_hash
            if DecisionTarget.USER in plans
            else receipt.baseline_user_hash
        )
        expected_memory_hash = (
            plans[DecisionTarget.MEMORY].after_hash
            if DecisionTarget.MEMORY in plans
            else receipt.baseline_memory_hash
        )
        if user_hash != expected_user_hash or memory_hash != expected_memory_hash:
            return receipt

        import_plan = plans.get(DecisionTarget.IMPORT)
        baseline_history_hash = history_hash
        if import_plan is not None:
            # Normal imports create one fresh batch-named history file. If an
            # unexpected pre-existing file was modified instead, its old
            # ordering cannot be reconstructed safely from the receipt.
            if import_plan.before_exists or not import_plan.after_exists:
                return receipt
            import_name = Path(import_plan.relative_path).name
            remaining_history = [
                entry for entry in history if entry.get("name") != import_name
            ]
            if len(remaining_history) != len(history) - 1:
                return receipt
            baseline_history_hash = stable_hash(
                part
                for entry in remaining_history
                for part in (
                    entry["name"],
                    image_hash(exists=True, content=entry["content"]),
                )
            )
        if baseline_history_hash != receipt.baseline_history_hash:
            return receipt
        if (
            receipt.applied_user_hash
            and receipt.applied_user_hash != user_hash
        ) or (
            receipt.applied_memory_hash
            and receipt.applied_memory_hash != memory_hash
        ) or (
            receipt.applied_history_hash
            and receipt.applied_history_hash != history_hash
        ):
            return receipt

        updated = receipt.model_copy(
            update={
                "applied_user_hash": user_hash,
                "applied_memory_hash": memory_hash,
                "applied_history_hash": history_hash,
            }
        )
        self.store.save_receipt(updated)
        return updated

    def _recover_locked(self, now: datetime) -> list[str]:
        recovered: list[str] = []
        for journal, undo in self.store.iter_journals():
            if journal.phase not in {"committed", "rolled_back"}:
                self._assert_receipt_roots(journal.receipt)
            batch_id = recover_transaction(
                paths=self.paths,
                store=self.store,
                journal=journal,
                undo=undo,
                now=now,
            )
            if not undo:
                receipt = self.store.load_receipt_by_batch(journal.batch_id)
                if receipt is not None:
                    self._repair_applied_context_if_safe(receipt)
            if not undo and journal.phase in {"published", "committed"}:
                with contextlib.suppress(ProfileImportError):
                    self.store.delete_raw(journal.batch_id)
            if batch_id is not None:
                recovered.append(batch_id)
        return recovered

    @contextlib.contextmanager
    def _operation_lock(self) -> Iterator[object]:
        manager = self._profile_lock_factory(self.paths.operation_lock_root)
        try:
            lock = manager.__enter__()
        except ProfileImportError:
            raise
        except Exception as exc:
            raise ProfileImportWriteError("could not acquire the profile operation lock") from exc
        try:
            yield lock
        finally:
            manager.__exit__(None, None, None)

    def _new_id(self) -> str:
        value = self._id_factory()
        if (
            len(value) != 32
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ProfileImportWriteError("profile import ID factory returned an invalid ID")
        return value
    ProfileImportJobNotFoundError,
    ProfileImportJob,
    ProfileImportJobRecord,
    ProfileImportJobStage,
    ProfileImportJobStatus,
