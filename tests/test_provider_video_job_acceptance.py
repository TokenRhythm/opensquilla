from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.provider import (
    gemini_video_generation,
    qwen_video_generation,
    tokenrhythm_video_generation,
    video_generation,
    xai_video_generation,
)
from opensquilla.provider.video_generation import (
    VideoGenerationError,
    VideoGenerationSubmissionUnknown,
)

_JOB = "accepted-synthetic-job"
_BASE = "https://video.example.test/v1"
_ROUTES = [
    (video_generation.generate_openrouter_video, "google/veo-3.1", {"id": _JOB}),
    (xai_video_generation.generate_xai_video, "grok-imagine-video-1.5", {"request_id": _JOB}),
    (
        gemini_video_generation.generate_gemini_video,
        "veo-3.1-generate-preview",
        {"name": f"operations/{_JOB}"},
    ),
    (qwen_video_generation.generate_qwen_video, "wan2.6-t2v", {"output": {"task_id": _JOB}}),
    (
        partial(qwen_video_generation.generate_qwen_video, provider="qwen_token_plan"),
        "wan2.6-t2v",
        {"output": {"task_id": _JOB}},
    ),
    (tokenrhythm_video_generation.generate_tokenrhythm_video, "wan3.0-video", {"id": _JOB}),
]


def _parameters(path: Path, model: str) -> dict[str, Any]:
    return {
        "base_url": _BASE,
        "api_key": "synthetic-video-key",
        "model": model,
        "prompt": "A paper kite above a field",
        "duration": 8,
        "max_duration_seconds": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "output_path": path,
        "timeout_seconds": 5,
        "poll_interval_seconds": 0.001,
    }


@pytest.mark.parametrize("generate,model,accepted_payload", _ROUTES)
async def test_all_adapters_report_acceptance_before_cancellable_polling(
    generate: Any,
    model: str,
    accepted_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    accepted: list[str] = []
    poll_started = asyncio.Event()
    poll_cancelled = asyncio.Event()
    requests: list[httpx.Request] = []
    payload = {**accepted_payload, "status": "pending", "polling_url": f"/v1/videos/{_JOB}"}

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json=payload)
        poll_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            poll_cancelled.set()
            raise
        raise AssertionError("Polling should remain blocked until cancellation")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    task = asyncio.create_task(
        generate(**_parameters(tmp_path / "clip.mp4", model), on_job_accepted=accepted.append)
    )
    try:
        await asyncio.wait_for(poll_started.wait(), timeout=1)
        expected_id = f"operations/{_JOB}" if "name" in payload else _JOB
        assert accepted == [expected_id]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert poll_cancelled.is_set()
    assert [request.method for request in requests] == ["POST", "GET"]
    assert not (tmp_path / "clip.mp4").exists()


@pytest.mark.parametrize("generate,model,_payload", _ROUTES)
async def test_invalid_submission_id_never_reports_acceptance(
    generate: Any,
    model: str,
    _payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    accepted: list[str] = []
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(202, json={})), **kwargs
        ),
    )
    with pytest.raises(VideoGenerationSubmissionUnknown):
        await generate(
            **_parameters(tmp_path / "clip.mp4", model), on_job_accepted=accepted.append
        )
    assert accepted == []


async def test_openrouter_reports_accepted_id_before_polling_url_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    accepted: list[str] = []
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(202, json={"id": _JOB, "polling_url": "/wrong"})
            ),
            **kwargs,
        ),
    )
    with pytest.raises(VideoGenerationError) as raised:
        await video_generation.generate_openrouter_video(
            **_parameters(tmp_path / "clip.mp4", "google/veo-3.1"),
            on_job_accepted=accepted.append,
        )
    assert raised.value.job_id == _JOB
    assert raised.value.recoverable is True
    assert accepted == [_JOB]
