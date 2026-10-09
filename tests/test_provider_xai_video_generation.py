"""Offline contract checks for xAI video submission, recovery, and downloads."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from opensquilla.provider import video_download
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationSubmissionUnknown,
)
from opensquilla.provider.xai_video_generation import (
    XAI_VIDEO_BASE_URL,
    generate_xai_video,
    resume_xai_video,
)

_MODEL = "grok-imagine-video-1.5"
_JOB = "synthetic-job-123"
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 12
_MEDIA_URL = "https://media.example.test/clip.mp4?signature=synthetic"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        video_download, "validate_http_url_for_fetch", lambda _url: ["93.184.216.34"]
    )
    monkeypatch.setattr(video_download, "pinned_transport", lambda *_args, **_kwargs: None)


async def _generate(path: Path, **overrides):
    settings = {
        "base_url": XAI_VIDEO_BASE_URL,
        "api_key": "synthetic-test-key",
        "model": _MODEL,
        "prompt": "A paper boat on a quiet river",
        "duration": None,
        "max_duration_seconds": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "output_path": path,
        "timeout_seconds": 5,
        "poll_interval_seconds": 0.001,
    }
    settings.update(overrides)
    return await generate_xai_video(**settings)


@pytest.mark.asyncio
async def test_generate_submits_polls_and_downloads_without_forwarding_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/v1/videos/generations"
            assert request.headers["Authorization"] == "Bearer synthetic-test-key"
            assert json.loads(request.content) == {
                "model": _MODEL,
                "prompt": "A paper boat on a quiet river",
                "duration": 8,
                "aspect_ratio": "16:9",
                "resolution": "720p",
            }
            return httpx.Response(200, json={"request_id": _JOB})
        if request.url.host == "api.x.ai":
            polls += 1
            return httpx.Response(
                200,
                json=(
                    {"status": "pending"}
                    if polls == 1
                    else {"status": "done", "video": {"url": _MEDIA_URL}}
                ),
            )
        assert str(request.url) == _MEDIA_URL
        assert "authorization" not in request.headers
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    result = await _generate(path)
    assert result.provider == "xai"
    assert result.job_id == _JOB
    assert result.bytes_written == len(_MP4)
    assert path.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_custom_api_root_preserves_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": _JOB})
        if request.url.path.endswith(f"/videos/{_JOB}"):
            return httpx.Response(200, json={"status": "done", "video": {"url": _MEDIA_URL}})
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    result = await _generate(
        tmp_path / "clip.mp4", base_url="https://proxy.example.test/custom/media/"
    )
    assert result.provider == "xai"
    assert paths[:2] == [
        "/custom/media/videos/generations",
        f"/custom/media/videos/{_JOB}",
    ]


@pytest.mark.asyncio
async def test_pending_job_resumes_without_second_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    first_poll = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_poll
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"request_id": _JOB})
        if request.url.host == "api.x.ai":
            if first_poll:
                first_poll = False
                return httpx.Response(503)
            return httpx.Response(200, json={"status": "done", "video": {"url": _MEDIA_URL}})
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationPending) as raised:
        await _generate(path)
    assert raised.value.job_id == _JOB
    result = await resume_xai_video(
        base_url=XAI_VIDEO_BASE_URL,
        api_key="synthetic-test-key",
        job_id=_JOB,
        model=_MODEL,
        output_path=path,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    )
    assert result.job_id == _JOB
    assert path.read_bytes() == _MP4
    assert [request.method for request in requests].count("POST") == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "https://evil.example/v1?key=synthetic"},
        {"base_url": "http://evil.example/v1"},
        {"model": "../unsafe"},
        {"duration": 16},
        {"duration": 9},
        {"model": "grok-imagine-video", "resolution": "1080p"},
    ],
)
async def test_invalid_options_fail_before_paid_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError):
        await _generate(tmp_path / "clip.mp4", **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["../other", "https://evil.example/x", ".", "x?key=y"])
async def test_resume_rejects_unsafe_job_id_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_id: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="request ID"):
        await resume_xai_video(
            base_url=XAI_VIDEO_BASE_URL,
            api_key="synthetic-test-key",
            job_id=job_id,
            model=_MODEL,
            output_path=tmp_path / "clip.mp4",
        )


@pytest.mark.asyncio
async def test_unsafe_download_redirect_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": _JOB})
        if request.url.host == "api.x.ai":
            return httpx.Response(200, json={"status": "done", "video": {"url": _MEDIA_URL}})
        assert "authorization" not in request.headers
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError):
        await _generate(tmp_path / "clip.mp4")
    assert [request.url.host for request in requests] == [
        "api.x.ai",
        "api.x.ai",
        "media.example.test",
    ]


@pytest.mark.asyncio
async def test_download_stream_limit_cleans_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": _JOB})
        if request.url.host == "api.x.ai":
            return httpx.Response(200, json={"status": "done", "video": {"url": _MEDIA_URL}})
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationError, match="byte limit"):
        await _generate(path, max_bytes=len(_MP4) - 1)
    assert not path.exists()
    assert list(tmp_path.glob(".video-*.tmp")) == []


@pytest.mark.asyncio
async def test_explicit_loopback_endpoint_can_serve_local_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    count = await video_download.download_video_url(
        url="http://127.0.0.1:18888/content/clip.mp4",
        output_path=path,
        job_id=_JOB,
        max_bytes=1024,
        deadline=time.monotonic() + 5,
        provider="synthetic",
        trusted_endpoint="http://127.0.0.1:18888/v1",
    )
    assert count == len(_MP4)
    assert path.read_bytes() == _MP4
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_loopback_download_must_match_explicit_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError):
        await video_download.download_video_url(
            url="http://127.0.0.1:18889/content/clip.mp4",
            output_path=tmp_path / "clip.mp4",
            job_id=_JOB,
            max_bytes=1024,
            deadline=time.monotonic() + 5,
            provider="synthetic",
            trusted_endpoint="http://127.0.0.1:18888/v1",
        )


@pytest.mark.asyncio
async def test_public_signed_download_rejects_private_ip(
    tmp_path: Path,
) -> None:
    with pytest.raises(VideoGenerationError, match="unsafe"):
        await video_download.download_video_url(
            url="https://127.0.0.1/internal/clip.mp4",
            output_path=tmp_path / "clip.mp4",
            job_id=_JOB,
            max_bytes=1024,
            deadline=time.monotonic() + 5,
            provider="synthetic",
        )
    assert not (tmp_path / "clip.mp4").exists()
