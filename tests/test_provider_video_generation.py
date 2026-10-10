from __future__ import annotations

import errno
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from opensquilla.provider import video_generation
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationPending,
    VideoGenerationSubmissionUnknown,
    generate_openrouter_video,
    resume_openrouter_video,
)

_BASE = "https://openrouter.ai/api/v1"
_MODEL = "google/veo-3.1"
_JOB = "job-abc123"
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def _catalog(
    *, durations: list[int] | None = None, include_durations: bool = True
) -> httpx.Response:
    entry: dict[str, object] = {
        "id": _MODEL,
        "supported_aspect_ratios": ["16:9", "9:16"],
        "supported_resolutions": ["720p", "1080p"],
    }
    if include_durations:
        entry["supported_durations"] = durations if durations is not None else [5, 8]
    return httpx.Response(
        200,
        json={"data": [entry]},
    )


def _accepted(*, polling_url: str = f"/api/v1/videos/{_JOB}") -> httpx.Response:
    return httpx.Response(
        202,
        json={"id": _JOB, "polling_url": polling_url, "status": "pending"},
    )


def _completed() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": _JOB,
            "status": "completed",
            "generation_id": "gen-synthetic",
            "unsigned_urls": ["https://untrusted.example/video.mp4"],
        },
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(video_generation.httpx, "AsyncClient", fake_async_client)


async def _generate(path: Path, **overrides: object):
    parameters: dict[str, object] = {
        "base_url": _BASE,
        "api_key": "synthetic-test-key",
        "model": _MODEL,
        "prompt": "A quiet city street at dawn",
        "duration": 5,
        "max_duration_seconds": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "output_path": path,
        "timeout_seconds": 5,
        "poll_interval_seconds": 0.001,
    }
    parameters.update(overrides)
    return await generate_openrouter_video(**parameters)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_submits_once_polls_and_downloads_from_authenticated_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        requests.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-test-key"
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            assert json.loads(request.content) == {
                "model": _MODEL,
                "prompt": "A quiet city street at dawn",
                "duration": 5,
                "aspect_ratio": "16:9",
                "resolution": "720p",
            }
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            polls += 1
            if polls == 1:
                return httpx.Response(200, json={"id": _JOB, "status": "in_progress"})
            return _completed()
        if request.url.path == f"/api/v1/videos/{_JOB}/content":
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    output_path = tmp_path / "clip.mp4"
    result = await _generate(output_path)

    assert result.job_id == _JOB
    assert result.model == _MODEL
    assert result.output_path == output_path
    assert result.bytes_written == len(_MP4)
    assert result.generation_id == "gen-synthetic"
    assert output_path.read_bytes() == _MP4
    assert [request.method for request in requests] == ["GET", "POST", "GET", "GET", "GET"]
    assert all(request.url.host == "openrouter.ai" for request in requests)


@pytest.mark.asyncio
async def test_custom_compatible_endpoint_uses_its_own_job_api_without_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = "https://media-proxy.example/custom/video"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "media-proxy.example"
        assert request.headers["authorization"] == "Bearer synthetic-test-key"
        if request.method == "POST":
            assert request.url.path == "/custom/video/videos"
            return _accepted(polling_url=f"/custom/video/videos/{_JOB}")
        if request.url.path == f"/custom/video/videos/{_JOB}":
            return _completed()
        if request.url.path == f"/custom/video/videos/{_JOB}/content":
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    path = tmp_path / "custom.mp4"
    result = await _generate(path, base_url=base)
    assert result.job_id == _JOB
    assert path.read_bytes() == _MP4
    assert [request.method for request in requests] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
async def test_unsupported_model_option_is_rejected_before_paid_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _catalog(durations=[8])

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="durations 5") as raised:
        await _generate(tmp_path / "clip.mp4")
    assert raised.value.job_id is None
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("durations", "include_durations", "limit", "expected"),
    [
        ([12, 6, 4], True, 8, 4),
        (None, False, 3, 3),
    ],
)
async def test_omitted_duration_posts_explicit_bounded_model_duration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    durations: list[int] | None,
    include_durations: bool,
    limit: int,
    expected: int,
) -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/videos/models":
            return _catalog(durations=durations, include_durations=include_durations)
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    destination = tmp_path / "clip.mp4"
    await _generate(destination, duration=None, max_duration_seconds=limit)
    assert destination.read_bytes() == _MP4
    assert len(posts) == 1
    assert posts[0]["duration"] == expected
    assert expected <= limit


@pytest.mark.asyncio
@pytest.mark.parametrize("durations", [[12], []])
async def test_omitted_duration_without_supported_bounded_option_rejects_before_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durations: list[int]
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _catalog(durations=durations)

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="duration"):
        await _generate(tmp_path / "clip.mp4", duration=None)
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 4])
async def test_duration_limit_rejects_invalid_or_over_limit_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="duration"):
        await _generate(tmp_path / "clip.mp4", max_duration_seconds=limit)


@pytest.mark.asyncio
async def test_rejects_polling_url_on_another_origin_without_sending_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return _catalog()
        return _accepted(polling_url=f"https://outside.example/api/v1/videos/{_JOB}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="left the job endpoint") as raised:
        await _generate(tmp_path / "clip.mp4")
    assert raised.value.job_id == _JOB
    assert raised.value.recoverable is True
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "polling_url",
    [None, "", f"/api/v1/videos/{_JOB}?token=unsafe", "https://[invalid-ipv6"],
)
async def test_bad_polling_url_retains_job_id_for_canonical_get_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, polling_url: str | None
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            payload: dict[str, object] = {"id": _JOB, "status": "pending"}
            if polling_url is not None:
                payload["polling_url"] = polling_url
            return httpx.Response(202, json=payload)
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        if request.url.path == f"/api/v1/videos/{_JOB}/content":
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    destination = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationError) as raised:
        await _generate(destination)
    assert raised.value.job_id == _JOB
    assert raised.value.recoverable is True
    assert [request.method for request in requests] == ["GET", "POST"]

    result = await resume_openrouter_video(
        base_url=_BASE,
        api_key="synthetic-test-key",
        job_id=_JOB,
        model=_MODEL,
        output_path=destination,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    )
    assert result.job_id == _JOB
    assert destination.read_bytes() == _MP4
    assert [request.method for request in requests] == ["GET", "POST", "GET", "GET"]
    assert all(request.url.host == "openrouter.ai" for request in requests)


@pytest.mark.asyncio
async def test_ambiguous_post_failure_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return _catalog()
        posts += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationSubmissionUnknown, match="do not resubmit") as raised:
        await _generate(tmp_path / "clip.mp4")
    assert raised.value.job_id is None
    assert posts == 1


@pytest.mark.asyncio
async def test_temporary_poll_failure_can_resume_by_get_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    methods: list[str] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        methods.append(request.method)
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            polls += 1
            if polls == 1:
                raise httpx.ReadTimeout("synthetic poll timeout", request=request)
            return _completed()
        if request.url.path == f"/api/v1/videos/{_JOB}/content":
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    destination = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationPending) as raised:
        await _generate(destination)
    assert raised.value.job_id == _JOB
    assert raised.value.recoverable is True
    before_resume = len(methods)

    result = await resume_openrouter_video(
        base_url=_BASE,
        api_key="synthetic-test-key",
        job_id=_JOB,
        model=_MODEL,
        output_path=destination,
        timeout_seconds=5,
        poll_interval_seconds=0.001,
    )
    assert result.job_id == _JOB
    assert destination.read_bytes() == _MP4
    assert methods[before_resume:] == ["GET", "GET"]
    assert methods.count("POST") == 1


class _Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield _MP4
        yield b"x" * 128


class _CompetingFileChunks(httpx.AsyncByteStream):
    def __init__(self, destination: Path) -> None:
        self.destination = destination

    async def __aiter__(self):
        yield _MP4
        self.destination.write_bytes(b"other turn's file")


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_links_supported", [True, False])
async def test_concurrent_output_creation_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hard_links_supported: bool
) -> None:
    destination = tmp_path / "clip.mp4"
    if not hard_links_supported:

        def unavailable_link(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EOPNOTSUPP, "synthetic hard-link limitation")

        monkeypatch.setattr(video_generation.os, "link", unavailable_link)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            stream=_CompetingFileChunks(destination),
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="already exists") as raised:
        await _generate(destination)
    assert raised.value.job_id == _JOB
    assert destination.read_bytes() == b"other turn's file"
    assert list(tmp_path.glob(".video-*.tmp")) == []


@pytest.mark.asyncio
async def test_exclusive_copy_fallback_publishes_when_hard_links_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unavailable_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "synthetic hard-link limitation")

    monkeypatch.setattr(video_generation.os, "link", unavailable_link)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
        )

    _install_transport(monkeypatch, handler)
    destination = tmp_path / "clip.mp4"
    result = await _generate(destination)
    assert result.bytes_written == len(_MP4)
    assert destination.read_bytes() == _MP4
    assert list(tmp_path.glob(".video-*.tmp")) == []


@pytest.mark.asyncio
async def test_streamed_byte_limit_removes_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        return httpx.Response(200, headers={"content-type": "video/mp4"}, stream=_Chunks())

    _install_transport(monkeypatch, handler)
    destination = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationError, match="byte limit") as raised:
        await _generate(destination, max_bytes=len(_MP4) + 16)
    assert raised.value.job_id == _JOB
    assert not destination.exists()
    assert list(tmp_path.glob(".video-*.tmp")) == []


@pytest.mark.asyncio
async def test_failed_job_is_terminal_and_does_not_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        return httpx.Response(
            200, json={"id": _JOB, "status": "failed", "error": "Synthetic policy refusal"}
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="Synthetic policy refusal") as raised:
        await _generate(tmp_path / "clip.mp4")
    assert raised.value.job_id == _JOB
    assert raised.value.recoverable is False
    assert f"/api/v1/videos/{_JOB}/content" not in paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "content", "message"),
    [
        ("text/html", b"<html>wrong</html>", "unsupported type"),
        ("video/mp4", b"not a valid mp4", "invalid MP4"),
    ],
)
async def test_invalid_video_response_is_not_published(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content_type: str,
    content: bytes,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        return httpx.Response(
            200, headers={"content-type": content_type}, stream=httpx.ByteStream(content)
        )

    _install_transport(monkeypatch, handler)
    destination = tmp_path / "clip.mp4"
    with pytest.raises(VideoGenerationError, match=message):
        await _generate(destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".video-*.tmp")) == []


@pytest.mark.asyncio
async def test_redirected_content_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/videos/models":
            return _catalog()
        if request.method == "POST":
            return _accepted()
        if request.url.path == f"/api/v1/videos/{_JOB}":
            return _completed()
        return httpx.Response(302, headers={"location": "https://outside.example/video.mp4"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="HTTP 302"):
        await _generate(tmp_path / "clip.mp4")
    assert all(request.url.host == "openrouter.ai" for request in requests)


@pytest.mark.asyncio
async def test_invalid_endpoint_and_existing_output_fail_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    with pytest.raises(VideoGenerationError, match="base URL"):
        await _generate(tmp_path / "clip.mp4", base_url="https://outside.example/api/v1?token=x")
    destination = tmp_path / "clip.mp4"
    destination.write_bytes(b"existing")
    with pytest.raises(VideoGenerationError, match="new .mp4"):
        await _generate(destination)
    assert destination.read_bytes() == b"existing"
