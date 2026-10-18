"""Shared artifact delivery helpers for channel surfaces."""

from __future__ import annotations

import contextlib
import inspect
import json
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import structlog

from opensquilla.artifact_archives import HtmlZipExporter, capture_saved_html
from opensquilla.artifacts import ArtifactStore, strip_artifact_markers_from_text
from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelSendResult,
    ChannelSendStatus,
    channel_capability_profile,
    normalize_channel_send_result,
)
from opensquilla.channels.delivery_store import (
    _artifact_delivery_binding,
    deliver_operation_with_outbox,
)
from opensquilla.channels.types import ChannelArtifactDeliveryRequest, IncomingMessage
from opensquilla.contracts.content_material import ContentExportError
from opensquilla.paths import media_root_from_config

log = structlog.get_logger(__name__)

_MARKDOWN_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\((?P<target>[^)]+)\)\s*$")
_LOOSE_IMAGE_LINE_RE = re.compile(r"^\s*(?:image|file)\s*:\s*(?P<target>\S+)\s*$", re.I)


def artifact_delivery_key(artifact: dict[str, Any]) -> str:
    # Content hashes and names are untrusted presentation data.  Different
    # artifacts may contain identical bytes and must retain their own identity.
    session_id, artifact_id = artifact.get("session_id"), artifact.get("id")
    if not isinstance(session_id, str) or not session_id:
        return ""
    if not isinstance(artifact_id, str) or not artifact_id:
        return ""
    return "artifact:" + json.dumps([session_id, artifact_id], separators=(",", ":"))


def dedupe_artifacts_for_channel_delivery(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        key = artifact_delivery_key(artifact)
        if key:
            if key in seen:
                continue
            seen.add(key)
        unique.append(artifact)
    return unique


def channel_safe_artifact_url(artifact: dict[str, Any]) -> str:
    for key in ("channel_download_url", "signed_download_url"):
        value = artifact.get(key)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate.lower().startswith(("https://", "http://")):
                return candidate
    return ""


def artifact_fallback_lines(artifacts: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for artifact in dedupe_artifacts_for_channel_delivery(artifacts):
        name = artifact.get("name") if isinstance(artifact.get("name"), str) else "artifact"
        issue = artifact.get("channel_delivery_issue")
        if isinstance(issue, str) and issue:
            lines.append(f"{name}: {issue}")
        if artifact.get("channel_delivery_denied"):
            continue
        target = channel_safe_artifact_url(artifact)
        if target:
            lines.append(f"Generated file: {name} -> {target}")
        else:
            lines.append(f"Generated file: {name} -> available in the OpenSquilla task")
    return lines


def _delivery_issue(result: ChannelSendResult) -> str:
    # Adapter reasons can contain a request repr, local paths or provider data.
    # Only known internal outcomes and fixed adapter limits become user text.
    if result.reason == "artifact delivery identity does not match its original session and route":
        return "Channel delivery materials do not match the stored receipt; no confirmed receipt."
    if re.fullmatch(
        r"artifact delivery [0-9a-f]+ has durable outcome "
        r"(?:pending|unknown|sent_unconfirmed); manual reconciliation is required before retry",
        result.reason,
    ):
        return "Channel delivery has no confirmed receipt; awaiting confirmation."
    if result.reason in {
        "DingTalk artifacts must not exceed 20 MB",
        "DingTalk artifact ZIP must not exceed 20 MB",
        "DingTalk cannot upload an empty artifact",
    }:
        return result.reason + "."
    if result.status is ChannelSendStatus.UNSUPPORTED:
        return "This channel does not support the prepared file delivery."
    if result.status is ChannelSendStatus.FAILED:
        return "Channel file delivery failed; no confirmed receipt."
    return "Channel delivery has no confirmed receipt; awaiting confirmation."


def strip_artifact_markers_from_channel_text(text: str) -> str:
    return strip_artifact_markers_from_text(text)


def _artifact_reference_names(artifacts: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for artifact in artifacts:
        name = artifact.get("name")
        if isinstance(name, str) and name:
            names.add(Path(name).name.lower())
    return names


def _image_reference_target_name(line: str) -> str:
    match = _MARKDOWN_IMAGE_LINE_RE.match(line) or _LOOSE_IMAGE_LINE_RE.match(line)
    if match is None:
        return ""
    target = match.group("target").strip().strip("'\"")
    target = target.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    return target.rsplit("/", 1)[-1].lower()


def strip_delivered_artifact_image_references(
    text: str,
    artifacts: list[dict[str, Any]],
) -> str:
    names = _artifact_reference_names(artifacts)
    if not names:
        return text
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        target_name = _image_reference_target_name(line)
        if target_name and target_name in names:
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def can_deliver_channel_files(channel: Any) -> bool:
    deliver_artifact = getattr(channel, "deliver_artifact", None)
    send_file = getattr(channel, "send_file", None)
    if not callable(deliver_artifact) and not callable(send_file):
        return False
    profile = channel_capability_profile(channel)
    if profile is not None:
        return profile.artifact_delivery or profile.native_file_upload or profile.media
    capabilities = getattr(channel, "capabilities", None)
    if isinstance(capabilities, (set, frozenset, list, tuple)):
        capability_set = set(capabilities)
        return bool(
            {
                ChannelCapabilities.ARTIFACT_DELIVERY,
                ChannelCapabilities.NATIVE_FILE_UPLOAD,
                ChannelCapabilities.MEDIA,
            }
            & capability_set
        )
    return True


@contextlib.contextmanager
def _named_artifact_delivery_path(source: Path, filename: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="opensquilla-artifact-") as tmp_dir:
        target = Path(tmp_dir) / Path(filename).name
        try:
            target.hardlink_to(source)
        except OSError:
            shutil.copy2(source, target)
        yield target


@contextlib.asynccontextmanager
async def _prepared_delivery_path(
    channel: Any, store: ArtifactStore, ref: Any, path: Path,
) -> AsyncIterator[tuple[Path, str, str, int]]:
    """Hold immutable, complete materials until the awaited adapter has consumed them."""
    is_html = ref.mime in {"text/html", "application/xhtml+xml"} or (
        Path(ref.name).suffix.lower() in {".html", ".htm", ".xhtml"}
    )
    if is_html:
        exporter = getattr(channel, "_artifact_zip_exporter", None)
        if not isinstance(exporter, HtmlZipExporter):
            exporter = HtmlZipExporter()
            # Normal adapters retain the bounded exporter for their lifetime.
            # Slot-only third-party adapters still work with a call-owned one.
            with contextlib.suppress(AttributeError, TypeError):
                setattr(channel, "_artifact_zip_exporter", exporter)

        async def validate() -> None:
            store.get_ref(artifact_id=ref.id, session_id=ref.session_id)

        material = await exporter.export(
            lambda: capture_saved_html(store, ref.id, ref.session_id),
            validate=validate, single_file_name=ref.name,
        )
        assert material.path is not None and material.cleanup is not None
        assert material.filename is not None
        try:
            yield (
                material.path, material.filename, material.media_type,
                material.path.stat().st_size,
            )
        finally:
            await material.cleanup()
        return
    with _named_artifact_delivery_path(path, ref.name) as delivery_path:
        yield delivery_path, ref.name, ref.mime, ref.size


async def deliver_artifacts_as_channel_files(
    channel: Any,
    msg: IncomingMessage,
    artifacts: list[dict[str, Any]],
    config: Any,
    *,
    expected_session_id: str | None = None,
    attempted_keys: set[str] | None = None,
    delivered_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    # The expected owner comes from the admitted turn/session, never from an
    # artifact marker supplied by the model or a persisted reply.  Suppress
    # unauthorized metadata too: returning its signed URL as a text fallback
    # would disclose the file even when native upload was correctly denied.
    if not isinstance(expected_session_id, str) or not expected_session_id:
        return []
    artifacts = [
        artifact for artifact in artifacts
        if artifact.get("session_id") == expected_session_id
    ]
    native_delivery = can_deliver_channel_files(channel)
    deliver_artifact = getattr(channel, "deliver_artifact", None)
    send_file = getattr(channel, "send_file", None)
    if not artifacts:
        return artifacts

    store = ArtifactStore(media_root_from_config(config))

    async def deliver_request(request: ChannelArtifactDeliveryRequest) -> Any:
        # The shared path journals contextual requests even for adapters that
        # only implement the legacy two-argument file API.  Bypass installed
        # wrappers here so one artifact has exactly one outbox record.
        if callable(deliver_artifact):
            raw = getattr(channel, "_delivery_raw_deliver_artifact", deliver_artifact)
            result = raw(request)
        else:
            raw = getattr(channel, "_delivery_raw_send_file", send_file)
            if not callable(raw):
                raise TypeError("channel does not implement file delivery")
            result = raw(request.inbound.channel_id, request.file_path)
        return await result if inspect.isawaitable(result) else result

    undelivered: list[dict[str, Any]] = []
    validated_keys: set[str] = set()
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        session_id = artifact.get("session_id")
        if not isinstance(artifact_id, str) or not isinstance(session_id, str):
            continue
        try:
            ref, path = store.resolve_for_download(artifact_id, session_id=expected_session_id)
        except Exception as exc:  # noqa: BLE001 - invalid refs must not become URL fallbacks.
            log.warning(
                "channel_artifact_delivery.reference_rejected",
                channel_type=type(channel).__name__,
                error_type=type(exc).__name__,
            )
            continue
        # A valid id does not authenticate adjacent model-supplied fields.
        # Build fallback metadata from the resolved ref too, so an attacker
        # cannot attach another session's signed URL or a sensitive fake name.
        artifact = ref.to_dict()
        key = artifact_delivery_key(artifact)
        if key in validated_keys:
            continue
        validated_keys.add(key)
        if not native_delivery or (not callable(deliver_artifact) and not callable(send_file)):
            undelivered.append(artifact)
            continue
        sending = False
        delivery_confirmed = False
        try:
            async with _prepared_delivery_path(channel, store, ref, path) as (
                delivery_path, delivery_name, delivery_mime, delivery_size,
            ):
                original_request = ChannelArtifactDeliveryRequest(
                    inbound=msg, artifact_id=ref.id, file_path=str(path),
                    name=ref.name, mime_type=ref.mime, size=ref.size, session_id=session_id,
                )
                # Keep the identity the previous HTML sender could calculate.
                # The outbox compares the actual ZIP metadata under this same
                # key and refuses incompatible replay instead of uploading twice.
                delivery_id = _artifact_delivery_binding(
                    str(getattr(channel, "_delivery_channel_name", "") or ""), original_request,
                )
                request = ChannelArtifactDeliveryRequest(
                    inbound=msg,
                    artifact_id=ref.id,
                    file_path=str(delivery_path),
                    name=delivery_name,
                    mime_type=delivery_mime,
                    size=delivery_size,
                    session_id=session_id,
                    delivery_id=delivery_id,
                )
                if attempted_keys is not None:
                    attempted_keys.add(key)
                sending = True
                result = await deliver_operation_with_outbox(
                    channel, "deliver_artifact", deliver_request, (request,), {}
                )
                capability = ChannelCapabilities.ARTIFACT_DELIVERY
                normalized = normalize_channel_send_result(
                    result,
                    capability=capability,
                    target_id=msg.channel_id,
                )
                if result is None or not normalized.is_delivered():
                    log.warning(
                        "channel_artifact_delivery.file_delivery_not_sent",
                        artifact_id=artifact_id,
                        channel_type=type(channel).__name__,
                        status="unconfirmed" if result is None else normalized.status.value,
                        reason=(
                            ""
                            if capability == ChannelCapabilities.ARTIFACT_DELIVERY
                            else normalized.reason
                        ),
                        retryable=normalized.retryable,
                    )
                    undelivered.append({
                        **artifact,
                        "channel_delivery_issue": (
                            "Channel delivery has no confirmed receipt; awaiting confirmation."
                            if result is None else _delivery_issue(normalized)
                        ),
                    })
                else:
                    delivery_confirmed = True
                    if delivered_keys is not None:
                        delivered_keys.add(key)
        except Exception as exc:  # noqa: BLE001 - preserve text fallback on delivery failure.
            log.warning(
                "channel_artifact_delivery.file_delivery_failed",
                artifact_id=artifact_id,
                channel_type=type(channel).__name__,
                error_type=type(exc).__name__,
            )
            if delivery_confirmed:
                # A local cleanup failure does not revoke an already confirmed
                # remote receipt or justify another upload.
                continue
            issue = (
                f"Could not prepare the complete HTML bundle: {exc}"
                if isinstance(exc, ContentExportError)
                else "Channel file delivery was denied."
                if isinstance(exc, PermissionError)
                else "Channel delivery has no confirmed receipt; awaiting confirmation."
                if sending
                else "Could not prepare verified delivery materials."
            )
            undelivered.append({
                **artifact, "channel_delivery_issue": issue,
                **({"channel_delivery_denied": True} if isinstance(exc, PermissionError) else {}),
            })
    return undelivered
