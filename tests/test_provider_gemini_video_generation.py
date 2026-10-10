from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from opensquilla.provider import gemini_video_generation
from opensquilla.provider.gemini_video_generation import (
    GEMINI_VIDEO_BASE_URL,
    generate_gemini_video,
    resume_gemini_video,
)
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationSubmissionUnknown,
)

_MODEL = "veo-3.1-fast-generate-preview"
_JOB = f"models/{_MODEL}/operations/synthetic-job"
_VIDEO_URI = (
    "https://generativelanguage.googleapis.com/v1beta/files/synthetic-video:download?alt=media"
)
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    original = httpx.AsyncClient

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(gemini_video_generation.httpx, "AsyncClient", fake_client)


def _complete() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "name": _JOB,
            "done": True,
            "response": {
                "generateVideoResponse": {"generatedSamples": [{"video": {"uri": _VIDEO_URI}}]}
            },
        },
    )


async def _generate(output_path: Path, **overrides: object):
    kwargs: dict[str, object] = {
        "base_url": GEMINI_VIDEO_BASE_URL,
        "api_key": "synthetic-test-key",
        "model": _MODEL,
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
    return await generate_gemini_video(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_submits_once_polls_and_downloads_official_mp4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        assert request.headers.get("x-goog-api-key") == "synthetic-test-key"
        assert request.url.host == "generativelanguage.googleapis.com"
        if request.method == "POST":
            assert request.url.path == f"/v1beta/models/{_MODEL}:predictLongRunning"
            assert json.loads(request.content) == {
                "instances": [{"prompt": "A quiet city street at dawn"}],
                "parameters": {
                    "durationSeconds": "8",
                    "aspectRatio": "16:9",
                    "resolution": "1080p",
                },
            }
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            polls += 1
            return (
                httpx.Response(200, json={"name": _JOB, "done": False})
                if polls == 1
                else _complete()
            )
        if request.url.path.endswith("/files/synthetic-video:download"):
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    result = await _generate(path)
    assert result.provider == "gemini"
    assert result.job_id == _JOB
    assert result.model == _MODEL
    assert result.bytes_written == len(_MP4)
    assert path.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_custom_veo_endpoint_keeps_key_on_the_configured_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = "https://media-proxy.example/google/v1beta"
    uri = f"{base}/files/synthetic-video:download?alt=media"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "media-proxy.example"
        assert request.headers["x-goog-api-key"] == "synthetic-test-key"
        if request.method == "POST":
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            return httpx.Response(
                200,
                json={
                    "name": _JOB,
                    "done": True,
                    "response": {
                        "generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}
                    },
                },
            )
        if request.url.path.endswith("/files/synthetic-video:download"):
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    path = tmp_path / "custom.mp4"
    result = await _generate(path, base_url=base)
    assert result.provider == "gemini"
    assert path.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "resolution", "expected"),
    [(8, "720p", "4"), (6, "720p", "4"), (8, "1080p", "8")],
)
async def test_default_duration_chooses_shortest_valid_veo_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    limit: int,
    resolution: str,
    expected: str,
) -> None:
    posted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            return _complete()
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    await _generate(
        tmp_path / "clip.mp4", duration=None, max_duration_seconds=limit, resolution=resolution
    )
    assert len(posted) == 1
    assert posted[0]["parameters"]["durationSeconds"] == expected  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"duration": 5},
        {"duration": 4, "resolution": "1080p"},
        {"duration": None, "resolution": "1080p", "max_duration_seconds": 6},
        {"base_url": "http://evil.example/v1beta"},
        {"model": "gemini-3.1-flash"},
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
async def test_ambiguous_submission_failure_is_never_retried(
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
    "name",
    [
        "https://evil.example/operations/x",
        "models/other/operations/x",
        "models/veo-3.1-fast-generate-preview/operations/../evil",
        "models/veo-3.1-fast-generate-preview/operations/x?key=secret",
        "/v1beta/models/veo-3.1-fast-generate-preview/operations/x",
        f"models/{_MODEL}/operations/.",
        f"models/{_MODEL}/operations/..",
        "operations/.",
        "operations/..",
    ],
)
async def test_unsafe_operation_name_is_not_polled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"name": name})

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationSubmissionUnknown):
        await _generate(tmp_path / "clip.mp4")
    assert [request.method for request in requests] == ["POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["operations/.", "operations/.."])
async def test_resume_rejects_dot_operation_id_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_id: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="operation name"):
        await resume_gemini_video(
            base_url=GEMINI_VIDEO_BASE_URL,
            api_key="synthetic-test-key",
            job_id=job_id,
            model=_MODEL,
            output_path=tmp_path / "clip.mp4",
        )


@pytest.mark.asyncio
async def test_pending_poll_resumes_with_get_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    first_poll = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_poll
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            if first_poll:
                first_poll = False
                return httpx.Response(503)
            return _complete()
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationPending) as raised:
        await _generate(path)
    assert raised.value.job_id == _JOB
    prior_count = len(requests)
    result = await resume_gemini_video(
        base_url=GEMINI_VIDEO_BASE_URL,
        api_key="synthetic-test-key",
        job_id=_JOB,
        model=_MODEL,
        output_path=path,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    )
    assert result.job_id == _JOB
    assert path.read_bytes() == _MP4
    assert [request.method for request in requests[prior_count:]] == ["GET", "GET"]
    assert [request.method for request in requests].count("POST") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/video.mp4",
        "http://generativelanguage.googleapis.com/v1beta/files/video:download",
        "https://generativelanguage.googleapis.com/v1beta/models/x",
        "https://generativelanguage.googleapis.com@evil.example/v1beta/files/x",
        "https://generativelanguage.googleapis.com/v1beta/files/%2e%2e/models/x",
        "https://generativelanguage.googleapis.com/v1beta/files/../models/x",
    ],
)
async def test_download_uri_must_be_official_file_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uri: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"name": _JOB})
        payload = _complete().json()
        payload["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]["uri"] = uri
        return httpx.Response(200, json=payload)

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError):
        await _generate(tmp_path / "clip.mp4")
    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_google_storage_redirect_strips_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            return _complete()
        if request.url.host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == "synthetic-test-key"
            return httpx.Response(
                302, headers={"location": "https://storage.googleapis.com/bucket/clip.mp4?sig=x"}
            )
        assert request.url.host == "storage.googleapis.com"
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    await _generate(path)
    assert path.read_bytes() == _MP4
    assert [request.url.host for request in requests] == [
        "generativelanguage.googleapis.com",
        "generativelanguage.googleapis.com",
        "generativelanguage.googleapis.com",
        "storage.googleapis.com",
    ]


@pytest.mark.asyncio
async def test_non_google_redirect_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            return _complete()
        return httpx.Response(302, headers={"location": "https://evil.example/video.mp4"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationPending):
        await _generate(tmp_path / "clip.mp4")
    assert all(request.url.host == "generativelanguage.googleapis.com" for request in requests)


class _OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield _MP4
        yield b"x" * 64


@pytest.mark.asyncio
async def test_streamed_byte_limit_removes_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"name": _JOB})
        if request.url.path.endswith("/operations/synthetic-job"):
            return _complete()
        return httpx.Response(200, headers={"content-type": "video/mp4"}, stream=_OversizedStream())

    _install_transport(monkeypatch, handler)
    path = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationError, match="byte limit") as raised:
        await _generate(path, max_bytes=len(_MP4) + 8)
    assert raised.value.job_id == _JOB
    assert not path.exists()
    assert list(tmp_path.glob(".video-*.tmp")) == []
