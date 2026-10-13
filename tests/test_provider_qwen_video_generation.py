from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from opensquilla.provider import qwen_video_generation
from opensquilla.provider.qwen_video_generation import generate_qwen_video, resume_qwen_video
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationSubmissionUnknown,
)

_BASE = "http://127.0.0.1:19876/custom/api/v1"
_SIGNED_URL = "http://127.0.0.1:19876/signed/clip.mp4?token=synthetic"
_JOB = "synthetic-task-123"
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    original = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(qwen_video_generation.httpx, "AsyncClient", fake_client)


def _status(status: str) -> httpx.Response:
    output: dict[str, object] = {"task_id": _JOB, "task_status": status}
    if status == "SUCCEEDED":
        output["video_url"] = _SIGNED_URL
    return httpx.Response(200, json={"output": output})


async def _generate(output_path: Path, **overrides: object):
    kwargs: dict[str, object] = {
        "base_url": _BASE,
        "api_key": "synthetic-test-key",
        "model": "wan2.6-t2v",
        "prompt": "A quiet city street at dawn",
        "duration": 5,
        "max_duration_seconds": 8,
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "output_path": output_path,
        "timeout_seconds": 5,
        "poll_interval_seconds": 0.001,
    }
    kwargs.update(overrides)
    return await generate_qwen_video(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_submits_native_wan_request_polls_and_downloads_without_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        if request.method == "POST":
            assert request.url.path == (
                "/custom/api/v1/services/aigc/video-generation/video-synthesis"
            )
            assert request.headers["authorization"] == "Bearer synthetic-test-key"
            assert request.headers["x-dashscope-async"] == "enable"
            assert json.loads(request.content) == {
                "model": "wan2.6-t2v",
                "input": {"prompt": "A quiet city street at dawn"},
                "parameters": {"duration": 5, "size": "720*1280"},
            }
            return httpx.Response(200, json={"output": {"task_id": _JOB}})
        if request.url.path == f"/custom/api/v1/tasks/{_JOB}":
            assert request.headers["authorization"] == "Bearer synthetic-test-key"
            polls += 1
            return _status("RUNNING" if polls == 1 else "SUCCEEDED")
        if request.url.path == "/signed/clip.mp4":
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

    assert result.provider == "qwen"
    assert result.job_id == _JOB
    assert result.bytes_written == len(_MP4)
    assert output.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_token_plan_uses_same_native_api_with_its_selected_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"output": {"task_id": _JOB}})
        if request.url.path == f"/custom/api/v1/tasks/{_JOB}":
            return _status("SUCCEEDED")
        if request.url.path == "/signed/clip.mp4":
            assert "authorization" not in request.headers
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    result = await _generate(
        tmp_path / "clip.mp4",
        provider="qwen_token_plan",
        model="happyhorse-1.1-t2v",
        aspect_ratio="16:9",
    )

    assert result.provider == "qwen_token_plan"
    assert posted == [
        {
            "model": "happyhorse-1.1-t2v",
            "input": {"prompt": "A quiet city street at dawn"},
            "parameters": {"duration": 5, "resolution": "720P", "ratio": "16:9"},
        }
    ]


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
            return httpx.Response(200, json={"output": {"task_id": _JOB}})
        if request.url.path == f"/custom/api/v1/tasks/{_JOB}":
            if first_poll:
                first_poll = False
                return httpx.Response(503)
            return _status("SUCCEEDED")
        if request.url.path == "/signed/clip.mp4":
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

    result = await resume_qwen_video(
        base_url=_BASE,
        api_key="synthetic-test-key",
        job_id=_JOB,
        model="wan2.6-t2v",
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
        {"model": "../wan2.6-t2v"},
        {"duration": 16},
        {"model": "happyhorse-1.1-t2v", "duration": 2},
        {"model": "happyhorse-1.1-t2v", "duration": None, "max_duration_seconds": 2},
        {"duration": 9, "max_duration_seconds": 8},
        {"aspect_ratio": "1:1"},
        {"resolution": "4k"},
        {"base_url": "http://untrusted.example/api/v1"},
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
        await resume_qwen_video(
            base_url=_BASE,
            api_key="synthetic-test-key",
            job_id=job_id,
            model="wan2.6-t2v",
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
