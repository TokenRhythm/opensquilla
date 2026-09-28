from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from opensquilla.artifact_publication import (
    ArtifactPublicationAuthorization,
    ArtifactPublicationCandidate,
    ArtifactPublicationError,
    ArtifactPublicationRequest,
    read_publication_candidate,
)
from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.artifact_delivery import auto_publish_omitted_workspace_artifacts
from opensquilla.tools.builtin.artifacts import publish_artifact
from opensquilla.tools.types import (
    CallerKind,
    RetryableToolInputError,
    ToolContext,
    current_tool_context,
)

REPORT = b"<!doctype html><html><body>Validated report</body></html>"


class HostPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[ArtifactPublicationRequest, ArtifactPublicationCandidate]] = []
        self.overrides: dict[str, Any] = {}
        self.reject_reason: str | None = None
        self.before_return: Callable[[], None] | None = None

    async def authorize(
        self, request: ArtifactPublicationRequest, candidate: ArtifactPublicationCandidate
    ) -> ArtifactPublicationAuthorization:
        self.calls.append((request, candidate))
        if self.reject_reason is not None:
            raise ValueError(self.reject_reason)
        if request.path != "report.html" or candidate.payload != REPORT:
            raise ValueError("candidate does not match host-owned validation inputs")
        if self.before_return is not None:
            self.before_return()
        return replace(
            ArtifactPublicationAuthorization(
                session_id=request.session_id,
                session_key=request.session_key,
                execution_id=request.execution_id,
                sha256=candidate.sha256,
                validator_id="fixed-validator-sha256-v1",
                receipt_id="host-receipt-1",
            ),
            **self.overrides,
        )


@pytest.fixture
def protected(tmp_path: Path) -> tuple[ToolContext, HostPolicy, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.html"
    target.write_bytes(REPORT)
    policy = HostPolicy()
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:session-1",
        execution_id="run-1",
        artifact_publication_policy=policy,
    )
    return context, policy, target


async def _publish(ctx: ToolContext, **arguments: Any) -> dict[str, Any]:
    token = current_tool_context.set(ctx)
    try:
        raw = await publish_artifact(**{"path": "report.html", "bundle": "none", **arguments})
    finally:
        current_tool_context.reset(token)
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def _assert_nothing_published(ctx: ToolContext) -> None:
    assert not ctx.published_artifacts
    assert ctx.artifact_media_root is not None
    assert not Path(ctx.artifact_media_root).exists()


@pytest.mark.asyncio
async def test_protected_publish_uses_host_authorization(protected) -> None:
    ctx, policy, _ = protected
    result = await _publish(ctx)
    assert result["status"] == "published"
    assert len(policy.calls) == 1
    request, candidate = policy.calls[0]
    assert request.execution_id == "run-1"
    assert request.path == "report.html"
    assert request.name == "report.html"
    assert request.mime == "text/html"
    assert candidate.payload == REPORT
    assert result["artifact"]["sha256"] == candidate.sha256
    assert result["publicationValidation"]["receiptId"] == "host-receipt-1"
    assert "session_key" not in result["publicationValidation"]
    assert "local_path" not in result["artifact"]
    assert "workspace_path" not in result["artifact"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "source revision changed",
        "receipt digest differs",
        "validator version changed",
        "missing trusted receipts: /private/receipts.json",
        "validator timeout at http://private-host",
    ],
)
async def test_failed_host_validation_never_publishes(protected, reason: str) -> None:
    ctx, policy, _ = protected
    policy.reject_reason = reason
    with pytest.raises(RetryableToolInputError) as error:
        await _publish(ctx)
    assert "host validation" in str(error.value)
    assert reason not in str(error.value)
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": "another-session"},
        {"session_key": "another-key"},
        {"execution_id": "old-run"},
        {"sha256": "0" * 64},
        {"schema_version": "unsupported/2"},
        {"validator_id": ""},
        {"receipt_id": ""},
        {"receipt_id": "/private/receipt.json"},
        {"validator_id": "forged\nmetadata"},
    ],
)
async def test_authorization_is_bound_to_scope_hash_and_version(protected, overrides: dict) -> None:
    ctx, policy, _ = protected
    policy.overrides = overrides
    with pytest.raises(RetryableToolInputError, match="exact artifact bytes"):
        await _publish(ctx)
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
async def test_missing_execution_scope_is_not_an_unprotected_fallback(protected) -> None:
    ctx, policy, _ = protected
    ctx.execution_id = None
    with pytest.raises(RetryableToolInputError, match="execution scope"):
        await _publish(ctx)
    assert policy.calls == []
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"bundle": "auto"},
        {"bundle": "directory", "bundle_root": "."},
        {"bundle": "none", "bundle_root": "."},
        {"bundle": "unsupported"},
    ],
)
async def test_bundles_cannot_bypass_protected_publication(protected, arguments: dict) -> None:
    ctx, policy, _ = protected
    with pytest.raises(RetryableToolInputError, match="bundle='none'"):
        await _publish(ctx, **arguments)
    assert policy.calls == []
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
async def test_unapproved_path_and_forged_workspace_receipt_do_not_grant_authority(
    protected,
) -> None:
    ctx, _, target = protected
    (target.parent / "other.html").write_bytes(REPORT)
    (target.parent / "validation.json").write_text('{"ok":true,"receiptId":"host-receipt-1"}')
    with pytest.raises(RetryableToolInputError, match="host validation"):
        await _publish(ctx, path="other.html")
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", ["replace", "delete", "symlink"])
async def test_source_changes_after_authorization_cannot_change_published_bytes(
    protected, swap: str
) -> None:
    ctx, policy, target = protected

    def mutate() -> None:
        if swap == "replace":
            target.write_bytes(b"changed after validation")
        else:
            target.unlink()
            if swap == "symlink":
                outside = target.parent.parent / "private.html"
                outside.write_bytes(b"unvalidated private bytes")
                target.symlink_to(outside)

    policy.before_return = mutate
    result = await _publish(ctx)
    _, material = ArtifactStore(ctx.artifact_media_root).resolve_for_download(
        result["artifact"]["id"], session_id=ctx.artifact_session_id
    )
    assert material.read_bytes() == REPORT


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_current_turn", [False, True])
async def test_both_dedupe_paths_require_new_authorization(
    protected, clear_current_turn: bool
) -> None:
    ctx, policy, _ = protected
    first = await _publish(ctx)
    if clear_current_turn:
        ctx.published_artifacts.clear()
    policy.reject_reason = "validator revoked"
    with pytest.raises(RetryableToolInputError, match="host validation"):
        await _publish(ctx)
    policy.reject_reason = None
    again = await _publish(ctx)
    assert again["status"] == "already_published"
    assert again["artifact"]["id"] == first["artifact"]["id"]
    assert again["publicationValidation"]["receiptId"] == "host-receipt-1"
    assert len(policy.calls) == 3


@pytest.mark.asyncio
async def test_cancelled_validation_never_publishes(
    protected, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, policy, _ = protected

    async def cancel(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(policy, "authorize", cancel)
    with pytest.raises(asyncio.CancelledError):
        await _publish(ctx)
    _assert_nothing_published(ctx)


def test_protected_auto_publish_backstop_cannot_bypass_policy(protected) -> None:
    ctx, policy, target = protected
    ctx.workspace_file_writes.append({"created": True, "path": str(target), "name": target.name})
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created report.html.")
    assert not result.artifacts
    assert result.failure_summaries and "host validation" in result.failure_summaries[0]
    assert policy.calls == []
    _assert_nothing_published(ctx)


def test_protected_turn_without_tracked_writes_has_no_spurious_delivery_failure(protected) -> None:
    ctx, policy, _ = protected
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="The report is ready.")
    assert not result.artifacts and not result.failure_summaries
    assert not result.resolved_target_keys
    assert policy.calls == []
    _assert_nothing_published(ctx)


@pytest.mark.parametrize("kind", ["not_created", "not_mentioned", "not_deliverable", "missing"])
def test_protected_backstop_ignores_non_candidate_writes(protected, kind: str) -> None:
    ctx, policy, target = protected
    if kind == "not_deliverable":
        target = target.with_suffix(".txt")
        target.write_bytes(REPORT)
    if kind == "missing":
        target.unlink()
    ctx.workspace_file_writes.append(
        {"created": kind != "not_created", "path": str(target), "name": target.name}
    )
    final_text = "All done." if kind == "not_mentioned" else f"Created {target.name}."
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text=final_text)
    assert not result.artifacts and not result.failure_summaries
    assert policy.calls == []
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("tracked", [False, True])
async def test_explicitly_published_protected_artifact_is_not_reported_missing(
    protected,
    tracked: bool,
) -> None:
    ctx, policy, target = protected
    await _publish(ctx)
    if tracked:
        ctx.workspace_file_writes.append(
            {"created": True, "path": str(target), "name": target.name}
        )
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="Created report.html.")
    assert not result.artifacts and not result.failure_summaries
    assert len(policy.calls) == 1
    assert len(ctx.published_artifacts) == 1
    if tracked:
        assert "name:report.html" in result.resolved_target_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["digest", "name"])
async def test_protected_backstop_does_not_suppress_real_omission_after_other_publication(
    protected,
    change: str,
) -> None:
    ctx, policy, target = protected
    await _publish(ctx)
    if change == "digest":
        target.write_bytes(b"new unapproved report")
    else:
        target = target.with_name("other.html")
        target.write_bytes(REPORT)
    ctx.workspace_file_writes.append({"created": True, "path": str(target), "name": target.name})
    result = auto_publish_omitted_workspace_artifacts(ctx, final_text=f"Created {target.name}.")
    assert not result.artifacts
    assert len(result.failure_summaries) == 1
    assert "host validation" in result.failure_summaries[0]
    assert len(policy.calls) == 1
    assert len(ctx.published_artifacts) == 1


def test_candidate_snapshot_rejects_directory_links_and_special_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.html").write_bytes(REPORT)
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactPublicationError):
        read_publication_candidate(workspace, workspace / "linked/report.html", 1000)
    with pytest.raises(ArtifactPublicationError):
        read_publication_candidate(workspace, workspace / "../outside/report.html", 1000)
    fifo = workspace / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ArtifactPublicationError, match="regular file"):
        read_publication_candidate(workspace, fifo, 1000)


def test_candidate_is_immutable_and_size_checked_before_validation(protected) -> None:
    ctx, policy, target = protected
    with pytest.raises(ArtifactPublicationError, match="size limit"):
        read_publication_candidate(target.parent, target, 1)
    candidate = read_publication_candidate(target.parent, target, 1000)
    with pytest.raises(FrozenInstanceError):
        candidate.payload = b"changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ArtifactPublicationCandidate(bytearray(b"mutable"))  # type: ignore[arg-type]
    assert policy.calls == []
    _assert_nothing_published(ctx)


@pytest.mark.asyncio
async def test_normal_context_keeps_legacy_publication_without_policy(protected) -> None:
    ctx, policy, _ = protected
    ctx.artifact_publication_policy = None
    result = await _publish(ctx, bundle="auto")
    assert result["status"] == "published"
    assert "publicationValidation" not in result
    assert "local_path" in result["artifact"]
    assert not policy.calls
