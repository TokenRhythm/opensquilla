from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from opensquilla.provider import tokenrhythm_video_generation
from opensquilla.provider.tokenrhythm_video_generation import (
    generate_tokenrhythm_video,
    resume_tokenrhythm_video,
)
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationSubmissionUnknown,
)

_BASE = "http://127.0.0.1:19877/proxy/v1"
_SIGNED_URL = "http://127.0.0.1:19877/signed/video.mp4?token=synthetic"
_JOB = "synthetic-task-789"
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    original = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(tokenrhythm_video_generation.httpx, "AsyncClient", fake_client)


def _status(status: str) -> httpx.Response:
    payload: dict[str, object] = {"id": _JOB, "status": status}
    if status == "succeeded":
        payload["result"] = {"url": _SIGNED_URL}
    return httpx.Response(200, json=payload)


async def _generate(output_path: Path, **overrides: object):
    kwargs: dict[str, object] = {
        "base_url": _BASE,
        "api_key": "synthetic-test-key",
        "model": "wan3.0-video",
        "prompt": "A quiet city street at dawn",
        "duration": 8,
        "max_duration_seconds": 8,
        "aspect_ratio": "16:9",
        "resolution": "1080p",
        "output_path": output_path,
        "timeout_seconds": 5,
        "poll_interval_seconds": 0.001,
    }
    kwargs.update(overrides)
    return await generate_tokenrhythm_video(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_submits_native_request_polls_and_downloads_without_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        if request.method == "POST":
            assert request.url.path == "/proxy/v1/videos/generations"
            assert request.headers["authorization"] == "Bearer synthetic-test-key"
            assert json.loads(request.content) == {
                "model": "wan3.0-video",
                "content": [{"type": "text", "text": "A quiet city street at dawn"}],
                "resolution": "1080P",
                "duration": 8,
                "ratio": "16:9",
            }
            return httpx.Response(202, json={"id": _JOB, "status": "queued"})
        if request.url.path == f"/proxy/v1/videos/{_JOB}":
            assert request.headers["authorization"] == "Bearer synthetic-test-key"
            polls += 1
            return _status("processing" if polls == 1 else "succeeded")
        if request.url.path == "/signed/video.mp4":
            assert request.url.query == b"token=synthetic"
            assert "authorization" not in request.headers
            assert request.headers["accept"] == "video/mp4"
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    output = tmp_path / "clip.mp4"
    result = await _generate(output)

    assert result.provider == "tokenrhythm"
    assert result.job_id == _JOB
    assert result.bytes_written == len(_MP4)
    assert output.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_pending_job_resumes_with_get_without_another_paid_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    first_poll = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_poll
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"id": _JOB})
        if request.url.path == f"/proxy/v1/videos/{_JOB}":
            if first_poll:
                first_poll = False
                return httpx.Response(503)
            return _status("succeeded")
        if request.url.path == "/signed/video.mp4":
            assert "authorization" not in request.headers
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    output = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationPending) as pending:
        await _generate(output)
    assert pending.value.job_id == _JOB
    assert not output.exists()

    result = await resume_tokenrhythm_video(
        base_url=_BASE,
        api_key="synthetic-test-key",
        job_id=_JOB,
        model="wan3.0-video",
        output_path=output,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    )

    assert result.job_id == _JOB
    assert output.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "../wan3.0-video"},
        {"duration": 31},
        {"duration": 9, "max_duration_seconds": 8},
        {"aspect_ratio": "1:1"},
        {"resolution": "4k"},
        {"base_url": "http://untrusted.example/v1"},
    ],
)
async def test_invalid_request_fails_before_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError):
        await _generate(tmp_path / "clip.mp4", **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["../other", "id/next", "id?token=secret", ".."])
async def test_resume_rejects_unsafe_task_id_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_id: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="task ID"):
        await resume_tokenrhythm_video(
            base_url=_BASE,
            api_key="synthetic-test-key",
            job_id=job_id,
            model="wan3.0-video",
            output_path=tmp_path / "clip.mp4",
        )


@pytest.mark.asyncio
async def test_ambiguous_submission_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationSubmissionUnknown):
        await _generate(tmp_path / "clip.mp4")
    assert [request.method for request in requests] == ["POST"]
