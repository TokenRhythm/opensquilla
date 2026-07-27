"""Runtime helpers for artifact delivery backstops."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from opensquilla.artifact_validation import (
    ArtifactValidationError,
    is_pptx_candidate,
    validate_artifact_for_delivery,
)
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    DEFAULT_ARTIFACT_MAX_BYTES,
    INSTALLER_ARTIFACT_SUFFIXES,
    ArtifactBudgetError,
    ArtifactStore,
    _safe_filename,
    artifact_mime_for_name,
    artifact_payload,
    artifact_publish_max_bytes_for_name,
)
from opensquilla.tools.path_aliases import resolve_workspace_alias
from opensquilla.tools.types import ToolContext

log = logging.getLogger(__name__)

_DELIVERABLE_SUFFIXES = frozenset(
    {
        ".csv",
        ".htm",
        ".html",
        ".json",
        ".pdf",
        ".pptx",
        ".tsv",
        ".xlsx",
        *INSTALLER_ARTIFACT_SUFFIXES,
    }
)
_EXCLUDED_TOP_LEVEL_DIRS = frozenset({".claude", ".codex", ".omx", "memory"})


@dataclass(frozen=True)
class OmittedArtifactPublishResult:
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    failure_summaries: list[str] = field(default_factory=list)
    resolved_target_keys: list[str] = field(default_factory=list)


def artifact_delivery_publish_target_key(
    raw_target: str,
    *,
    workspace_dir: str | os.PathLike[str] | None,
) -> str | None:
    """Return the canonical identity used for workspace artifact delivery.

    Match ``publish_artifact`` for accepted path forms, while best-effort mapping
    a rejected foreign-platform sandbox spelling to the same corrected retry.
    An absolute identity also prevents an auto-published nested file from
    resolving a different same-named root file.
    """

    if not isinstance(raw_target, str) or not raw_target:
        return None
    try:
        raw_path = Path(raw_target)
        if workspace_dir is None:
            normalized = os.path.normcase(os.path.normpath(str(raw_path)))
        else:
            workspace = Path(workspace_dir).resolve(strict=False)
            alias_target = resolve_workspace_alias(raw_path, workspace)
            if alias_target is None and not raw_path.is_absolute():
                # A model can echo the sandbox path using the other platform's
                # separators (for example ``C:\\workspace\\...`` on a POSIX
                # gateway). The real publish tool may reject that foreign host
                # spelling, but delivery-failure identity should still match a
                # corrected retry for the same workspace-relative tail.
                for foreign_path in (
                    PureWindowsPath(raw_target),
                    PurePosixPath(raw_target),
                ):
                    alias_target = resolve_workspace_alias(foreign_path, workspace)
                    if alias_target is not None:
                        break
            target = (
                alias_target
                or (raw_path if raw_path.is_absolute() else workspace / raw_path)
            ).resolve(strict=False)
            normalized = os.path.normcase(os.path.normpath(str(target)))
    except (OSError, RuntimeError, ValueError):
        # Delivery notices are a best-effort backstop. A malformed model path
        # must never turn a tool error into a stream-consumer failure.
        return None
    return f"path:{normalized}"


def artifact_delivery_name_target_key(name: str) -> str:
    """Return the ArtifactStore-canonical public-name delivery identity."""

    return f"name:{_safe_filename(name)}"


def _text_mentions_written_file(final_text: str, record: dict[str, Any]) -> bool:
    text = final_text.casefold()
    candidates = {
        str(record.get("relative_path") or ""),
        str(record.get("path") or ""),
        str(record.get("name") or ""),
    }
    return any(candidate and candidate.casefold() in text for candidate in candidates)


def _published_artifact_keys(ctx: ToolContext) -> set[tuple[str, str]]:
    return {
        (
            str(artifact.get("sha256")),
            _safe_filename(str(artifact.get("name"))),
        )
        for artifact in ctx.published_artifacts
        if artifact.get("sha256") and artifact.get("name")
    }


def auto_publish_omitted_workspace_artifacts(
    ctx: ToolContext | None,
    *,
    final_text: str,
) -> OmittedArtifactPublishResult:
    """Publish deliverable files the model wrote but forgot to publish.

    This is intentionally conservative: a file must be written through a tracked
    workspace file tool during the current turn, have a deliverable suffix, and
    be named in the assistant's final text.
    """

    if ctx is None:
        return OmittedArtifactPublishResult()
    if not (
        ctx.workspace_dir
        and ctx.artifact_media_root
        and ctx.artifact_session_id
        and ctx.session_key
    ):
        return OmittedArtifactPublishResult()

    records = list(getattr(ctx, "workspace_file_writes", []) or [])
    if not records or not final_text.strip():
        return OmittedArtifactPublishResult()

    workspace = Path(ctx.workspace_dir).resolve()
    store = ArtifactStore(ctx.artifact_media_root)
    published: list[dict[str, Any]] = []
    failure_summaries: list[str] = []
    resolved_target_keys: list[str] = []
    seen_paths: set[Path] = set()
    known_artifact_keys = _published_artifact_keys(ctx)

    for record in records:
        if not record.get("created"):
            # Publish only files created during this turn; edits to existing
            # files are tracked for diagnostics and are not deliverables.
            continue
        target = Path(str(record.get("path") or "")).expanduser().resolve(strict=False)
        if target in seen_paths:
            continue
        seen_paths.add(target)
        try:
            target.relative_to(workspace)
        except ValueError:
            continue
        relative_path = target.relative_to(workspace)
        if relative_path.parts and relative_path.parts[0] in _EXCLUDED_TOP_LEVEL_DIRS:
            continue
        if target.suffix.casefold() not in _DELIVERABLE_SUFFIXES:
            continue
        if not target.is_file():
            continue
        if not _text_mentions_written_file(final_text, record):
            continue

        try:
            artifact_mime = artifact_mime_for_name(target.name)
            publish_max_bytes = artifact_publish_max_bytes_for_name(
                target.name,
                ctx.artifact_max_bytes
                if ctx.artifact_max_bytes is not None
                else DEFAULT_ARTIFACT_MAX_BYTES,
            )
            pptx_payload: bytes | None = None
            target_is_pptx = is_pptx_candidate(
                source_name=target.name,
                name=target.name,
                mime=artifact_mime,
            )
            if target_is_pptx:
                target_size = target.stat().st_size
                if publish_max_bytes is not None and target_size > publish_max_bytes:
                    raise ArtifactBudgetError(
                        "artifact exceeds per-file budget "
                        f"({target_size} > {publish_max_bytes})"
                    )
                pptx_payload = target.read_bytes()
                try:
                    validate_artifact_for_delivery(
                        pptx_payload,
                        source_name=target.name,
                        name=target.name,
                        mime=artifact_mime,
                        source="auto_publish_omitted",
                    )
                except ArtifactValidationError as exc:
                    failure_summaries.append(
                        f"auto-publish rejected {target.name}: {exc.user_message}"
                    )
                    continue

            target_sha256 = hashlib.sha256(
                pptx_payload if pptx_payload is not None else target.read_bytes()
            ).hexdigest()
            artifact_key = (target_sha256, _safe_filename(target.name))
            target_key = artifact_delivery_publish_target_key(
                str(target),
                workspace_dir=workspace,
            )
            name_key = artifact_delivery_name_target_key(target.name)
            if artifact_key in known_artifact_keys:
                for resolved_key in (target_key, name_key):
                    if (
                        resolved_key is not None
                        and resolved_key not in resolved_target_keys
                    ):
                        resolved_target_keys.append(resolved_key)
                continue
            existing = store.find_existing_ref(
                session_id=ctx.artifact_session_id,
                session_key=ctx.session_key,
                sha256=target_sha256,
                name=target.name,
                mime=artifact_mime,
            )
            if existing is not None and any(
                item.get("id") == existing.id for item in ctx.published_artifacts
            ):
                for resolved_key in (target_key, name_key):
                    if (
                        resolved_key is not None
                        and resolved_key not in resolved_target_keys
                    ):
                        resolved_target_keys.append(resolved_key)
                known_artifact_keys.add(artifact_key)
                continue
            if existing is None:
                disk_budget_bytes = (
                    ctx.artifact_disk_budget_bytes
                    if ctx.artifact_disk_budget_bytes is not None
                    else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES
                )
                if pptx_payload is not None:
                    ref = store.publish_bytes(
                        pptx_payload,
                        session_id=ctx.artifact_session_id,
                        session_key=ctx.session_key,
                        name=target.name,
                        mime=artifact_mime,
                        source="auto_publish_omitted",
                        max_bytes=publish_max_bytes,
                        disk_budget_bytes=disk_budget_bytes,
                    )
                else:
                    ref = store.publish_file(
                        target,
                        session_id=ctx.artifact_session_id,
                        session_key=ctx.session_key,
                        name=target.name,
                        mime=artifact_mime,
                        source="auto_publish_omitted",
                        max_bytes=publish_max_bytes,
                        disk_budget_bytes=disk_budget_bytes,
                    )
            else:
                ref = existing
            payload = artifact_payload(ref)
            ctx.published_artifacts.append(payload)
            published.append(payload)
            for resolved_key in (target_key, name_key):
                if resolved_key is not None and resolved_key not in resolved_target_keys:
                    resolved_target_keys.append(resolved_key)
            known_artifact_keys.add(artifact_key)
        except (ArtifactBudgetError, OSError, ValueError) as exc:
            failure_summaries.append(f"auto-publish failed for {target.name}: {exc}")
            log.warning(
                "artifact_delivery.auto_publish_failed path=%s error=%s",
                str(target),
                exc,
            )
    return OmittedArtifactPublishResult(
        artifacts=published,
        failure_summaries=failure_summaries,
        resolved_target_keys=resolved_target_keys,
    )
