from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.config import VideoGenerationConfig
from opensquilla.provider.video_generation import VideoGenerationPending
from opensquilla.provider.video_generation_credentials import VideoGenerationCredential
from opensquilla.provider.video_generation_policy import VIDEO_GENERATION_OFFICIAL_BASE_URLS
from opensquilla.tools.builtin import media
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context

_KEY = "synthetic-video-credential"
_JOB = "synthetic-video-binding-job"
_SESSION = "synthetic:video-credentials"


@pytest.fixture(autouse=True)
def _isolate_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "_video_job_sessions", {})


def _config() -> VideoGenerationConfig:
    return VideoGenerationConfig(enabled=True, primary="google/veo-3.1-fast")


def _cached_receipt(*, credential_env: str = "") -> media._VideoJobReceipt:
    return media._VideoJobReceipt(
        session_key=_SESSION,
        credential_fingerprint=media._video_credential_fingerprint(_KEY),
        provider="openrouter",
        model="google/veo-3.1-fast",
        base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["openrouter"],
        native_job_id=_JOB,
        credential_env=credential_env,
        completed_payload=json.dumps({"status": "ok", "job_id": _JOB}),
    )


def test_video_fingerprints_change_with_credentials_and_process_salt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = media._video_credential_fingerprint(_KEY)
    assert fingerprint == media._video_credential_fingerprint(_KEY)
    assert fingerprint != media._video_credential_fingerprint("synthetic-replacement-key")
    assert len(bytes.fromhex(fingerprint)) == 32
    assert _KEY not in fingerprint
    monkeypatch.setattr(media, "_VIDEO_JOB_FINGERPRINT_SALT", b"\x00" * 32)
    assert fingerprint != media._video_credential_fingerprint(_KEY)


@pytest.mark.asyncio
async def test_video_acceptance_reuses_fingerprint_computed_before_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_config(), VideoGenerationCredential(available=True, api_key=_KEY)),
    )
    calls: list[int] = []
    original = media._video_credential_fingerprint

    def fingerprint(api_key: str) -> str:
        calls.append(threading.get_ident())
        return original(api_key)

    async def generate(**kwargs: Any) -> None:
        assert len(calls) == 1
        kwargs["on_job_accepted"](_JOB)
        assert len(calls) == 1
        receipt = media._video_job_receipt(_JOB)
        assert receipt is not None
        assert receipt.session_key == _SESSION
        assert _KEY not in repr(vars(receipt))
        raise VideoGenerationPending("synthetic pending", job_id=_JOB)

    monkeypatch.setattr(media, "_video_credential_fingerprint", fingerprint)
    monkeypatch.setattr(media, "_video_adapter", lambda _provider: (generate, None))
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.WEB, session_key=_SESSION, workspace_dir=str(tmp_path))
    )
    try:
        result = json.loads(await media.video_generate("A synthetic paper kite"))
    finally:
        current_tool_context.reset(token)
    assert result["status"] == "pending"
    assert calls and all(thread_id != threading.get_ident() for thread_id in calls)


@pytest.mark.asyncio
async def test_video_cancelled_during_fingerprint_never_submits_a_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_config(), VideoGenerationCredential(available=True, api_key=_KEY)),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[str] = []

    def fingerprint(_api_key: str) -> str:
        started.set()
        try:
            assert release.wait(5)
            return "synthetic-fingerprint"
        finally:
            finished.set()

    async def generate(**_kwargs: Any) -> None:
        calls.append("generate")
        raise AssertionError("Cancelled preparation must not submit a video")

    def adapter(_provider: str) -> tuple[Any, Any]:
        calls.append("adapter")
        return generate, None

    monkeypatch.setattr(media, "_video_credential_fingerprint", fingerprint)
    monkeypatch.setattr(media, "_video_adapter", adapter)
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.WEB, session_key=_SESSION, workspace_dir=str(tmp_path))
    )
    task = asyncio.create_task(media.video_generate("A synthetic paper kite"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 5)
        current_tool_context.reset(token)
    assert calls == []
    assert media._video_job_sessions == {}


@pytest.mark.parametrize(
    ("candidate", "remembered", "expected_calls", "allowed"),
    [
        (_KEY, "", 1, True),
        ("synthetic-rotated-key", "", 1, False),
        ("synthetic-new-route-key", _KEY, 2, True),
        ("synthetic-rotated-key", "synthetic-rotated-key", 1, False),
    ],
)
@pytest.mark.asyncio
async def test_video_status_computes_each_candidate_once_outside_event_loop(
    candidate: str,
    remembered: str,
    expected_calls: int,
    allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _cached_receipt(credential_env="SYNTHETIC_VIDEO_KEY" if remembered else "")
    media._video_job_sessions[_JOB] = receipt
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_config(), VideoGenerationCredential(available=True, api_key=candidate)),
    )
    monkeypatch.setattr(media, "environment_value", lambda _name: remembered)
    calls: list[tuple[str, int]] = []
    original = media._video_credential_fingerprint

    def fingerprint(api_key: str) -> str:
        calls.append((api_key, threading.get_ident()))
        return original(api_key)

    monkeypatch.setattr(media, "_video_credential_fingerprint", fingerprint)
    token = current_tool_context.set(ToolContext(caller_kind=CallerKind.WEB, session_key=_SESSION))
    try:
        if allowed:
            assert await media.video_status(_JOB) == receipt.completed_payload
        else:
            with pytest.raises(ToolError, match="unavailable in this session"):
                await media.video_status(_JOB)
    finally:
        current_tool_context.reset(token)
    assert len(calls) == expected_calls
    assert all(thread_id != threading.get_ident() for _, thread_id in calls)
    assert _KEY not in repr(vars(media._video_job_sessions[_JOB]))


@pytest.mark.parametrize("session_key", [None, "synthetic:foreign-video-session"])
@pytest.mark.asyncio
async def test_video_status_rejects_foreign_sessions_before_deriving_credentials(
    session_key: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media._video_job_sessions[_JOB] = _cached_receipt()

    def unexpected_fingerprint(_api_key: str) -> str:
        raise AssertionError("Unauthorized sessions must not invoke the KDF")

    monkeypatch.setattr(media, "_video_credential_fingerprint", unexpected_fingerprint)
    token = current_tool_context.set(
        ToolContext(caller_kind=CallerKind.WEB, session_key=session_key)
    )
    try:
        with pytest.raises(ToolError, match="unavailable in this session"):
            await media.video_status(_JOB)
    finally:
        current_tool_context.reset(token)


def test_owner_can_resume_unknown_video_job_without_a_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_fingerprint(_api_key: str) -> str:
        raise AssertionError("Owner authorization does not require a retained receipt")

    monkeypatch.setattr(media, "_video_credential_fingerprint", unexpected_fingerprint)
    token = current_tool_context.set(ToolContext(is_owner=True))
    try:
        assert media._video_job_access_allowed(_JOB, _KEY, "openrouter")
    finally:
        current_tool_context.reset(token)
