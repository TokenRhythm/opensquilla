"""Shared artifact delivery helpers for channel surfaces."""

from __future__ import annotations

import contextlib
import inspect
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

from opensquilla.artifacts import ArtifactStore, strip_artifact_markers_from_text
from opensquilla.channels.contract import (
    ChannelCapabilities,
    channel_capability_profile,
    normalize_channel_send_result,
)
from opensquilla.channels.types import IncomingMessage
from opensquilla.paths import media_root_from_config

log = structlog.get_logger(__name__)

_MARKDOWN_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\((?P<target>[^)]+)\)\s*$")
_LOOSE_IMAGE_LINE_RE = re.compile(r"^\s*(?:image|file)\s*:\s*(?P<target>\S+)\s*$", re.I)


def artifact_delivery_key(artifact: dict[str, Any]) -> str:
    for field in (
        "sha256",
        "path",
        "channel_download_url",
        "signed_download_url",
        "download_url",
        "id",
        "name",
    ):
        value = artifact.get(field)
        if value:
            return f"{field}:{value}"
    return ""


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
        target = channel_safe_artifact_url(artifact)
        if target:
            lines.append(f"Generated file: {name} -> {target}")
        else:
            lines.append(f"Generated file: {name} -> available in WebUI")
    return lines


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
    send_file = getattr(channel, "send_file", None)
    if not callable(send_file):
        return False
    profile = channel_capability_profile(channel)
    if profile is not None:
        return profile.native_file_upload or profile.media
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


async def deliver_artifacts_as_channel_files(
    channel: Any,
    msg: IncomingMessage,
    artifacts: list[dict[str, Any]],
    config: Any,
) -> list[dict[str, Any]]:
    if not can_deliver_channel_files(channel):
        return artifacts
    send_file = getattr(channel, "send_file", None)
    if not callable(send_file) or not artifacts:
        return artifacts

    store = ArtifactStore(media_root_from_config(config))
    undelivered: list[dict[str, Any]] = []
    for artifact in dedupe_artifacts_for_channel_delivery(artifacts):
        artifact_id = artifact.get("id")
        session_id = artifact.get("session_id")
        if not isinstance(artifact_id, str) or not isinstance(session_id, str):
            undelivered.append(artifact)
            continue
        try:
            ref, path = store.resolve_for_download(artifact_id, session_id=session_id)
            with _named_artifact_delivery_path(path, ref.name) as delivery_path:
                result = send_file(msg.channel_id, str(delivery_path))
                if inspect.isawaitable(result):
                    result = await result
                normalized = normalize_channel_send_result(
                    result,
                    capability=ChannelCapabilities.NATIVE_FILE_UPLOAD,
                    target_id=msg.channel_id,
                )
                if not normalized.is_delivered():
                    log.warning(
                        "channel_artifact_delivery.file_delivery_not_sent",
                        artifact_id=artifact_id,
                        channel_type=type(channel).__name__,
                        status=normalized.status.value,
                        reason=normalized.reason,
                        retryable=normalized.retryable,
                    )
                    undelivered.append(artifact)
        except Exception as exc:  # noqa: BLE001 - preserve text fallback on delivery failure.
            log.warning(
                "channel_artifact_delivery.file_delivery_failed",
                artifact_id=artifact_id,
                channel_type=type(channel).__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            undelivered.append(artifact)
    return undelivered
