from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.engine import Agent, AgentConfig, ToolCall
from opensquilla.engine.types import DoneEvent, ToolResultEvent
from opensquilla.gateway.config import VideoGenerationConfig
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.provider import video_generation
from opensquilla.tools.builtin import media
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.policy_runtime import ToolSurfaceCapabilities, resolve_runtime_tool_surface
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext

_JOB = "synthetic-accepted-job"
_KEY = "synthetic-video-key"
_MODEL = "google/veo-3.1-fast"
_BASE = "https://openrouter.ai/api/v1"
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


class _VideoToolProvider:
    provider_name = "fake"

    def __init__(
        self, *, tool_name: str = "video_generate", arguments: dict[str, Any] | None = None
    ) -> None:
        self.calls = 0
        self.tool_name = tool_name
        self.arguments = arguments or {
            "prompt": "A paper kite above a field", "filename": "initial.mp4"
        }

    async def chat(self, _messages: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        self.calls += 1
        if self.calls == 1:
            yield ProviderToolStart(tool_use_id="video-call", tool_name=self.tool_name)
            yield ProviderToolEnd(
                tool_use_id="video-call",
                tool_name=self.tool_name,
                arguments=self.arguments,
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
        else:
            yield ProviderText(text="The video job can be checked again.")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _VideoHTTP:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.poll_started = asyncio.Event()
        self.poll_cancelled = asyncio.Event()
        self.ready = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {_KEY}"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": _MODEL}]})
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"id": _JOB, "status": "pending", "polling_url": f"/api/v1/videos/{_JOB}"},
            )
        if request.url.path.endswith("/content"):
            assert self.ready
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(_MP4)
            )
        assert request.url.path == f"/api/v1/videos/{_JOB}"
        if self.ready:
            return httpx.Response(200, json={"id": _JOB, "status": "completed"})
        self.poll_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.poll_cancelled.set()
            raise
        raise AssertionError("The initial poll should only finish through cancellation")


def _context(tmp_path: Path, *, session_key: str = "synthetic:video-creator") -> ToolContext:
    return resolve_runtime_tool_surface(
        ToolContext(
            is_owner=False,
            caller_kind=CallerKind.WEB,
            workspace_dir=str(tmp_path / "workspace"),
            session_key=session_key,
            artifact_media_root=str(tmp_path / "artifacts"),
            artifact_session_id="synthetic-video-session",
            allowed_tools={"video_generate", "video_status"},
        ),
        capabilities=ToolSurfaceCapabilities(video_generation=True, video_status=True),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("video_generate", "video_status"):
        registered = get_default_registry().get(name)
        assert registered is not None
        registry.register(registered.spec, registered.handler)
    return registry


@pytest.mark.parametrize("ending", ["stop", "parent_timeout", "provider_timeout"])
async def test_accepted_video_survives_real_turn_cancellation_and_resumes_without_resubmission(
    ending: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", _KEY)
    monkeypatch.setattr(media, "_video_job_sessions", {})
    config = VideoGenerationConfig(enabled=True, primary=_MODEL)
    # Shorten the provider deadline for the pending control without changing
    # the settings contract or relying on a slow external service.
    if ending == "provider_timeout":
        config = config.model_copy(update={"timeout_seconds": 0.05})
    media.configure_video_generation(config)
    transport = _VideoHTTP()
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    original_generate = video_generation.generate_openrouter_video

    async def generate(**kwargs: Any) -> Any:
        return await original_generate(**kwargs, poll_interval_seconds=0.001)

    monkeypatch.setattr(video_generation, "generate_openrouter_video", generate)
    context = _context(tmp_path)
    registry = _registry()
    handler = build_tool_handler(registry, context)
    provider = _VideoToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(timeout=3, max_iterations=3),
        tool_definitions=registry.to_tool_definitions(context),
        tool_handler=handler,
        tool_registry=registry,
        tool_context=context,
        session_key=context.session_key,
    )
    events: list[Any] = []
    deadline: asyncio.Timeout | None = None

    async def consume() -> None:
        nonlocal deadline
        async with asyncio.timeout(None) as deadline:
            async for event in agent.run_turn("Generate the requested synthetic video."):
                events.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(transport.poll_started.wait(), timeout=1)
        receipt = media._video_job_receipt(_JOB)
        assert receipt is not None
        assert receipt.session_key == context.session_key
        assert receipt.credential_fingerprint == media._video_credential_fingerprint(_KEY)
        assert receipt.status_lock.locked()
        assert media.video_status_available(context)
        assert not media.video_status_available(_context(tmp_path, session_key="synthetic:foreign"))

        if ending == "stop":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "parent_timeout":
            assert deadline is not None
            deadline.reschedule(asyncio.get_running_loop().time() + 0.01)
            with pytest.raises(TimeoutError):
                await task
        else:
            await asyncio.wait_for(task, timeout=1)
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            assert not result.is_error
            assert json.loads(result.result)["status"] == "pending"
            assert json.loads(result.result)["job_id"] == _JOB
            assert any(isinstance(event, DoneEvent) for event in events)
        assert transport.poll_cancelled.is_set()
        assert not receipt.status_lock.locked()
        assert len(media._video_job_sessions) == 1
        assert not (tmp_path / "workspace" / "initial.mp4").exists()

        foreign_handler = build_tool_handler(
            registry, _context(tmp_path, session_key="synthetic:foreign")
        )
        before_foreign = len(transport.requests)
        denied = await foreign_handler(ToolCall("foreign-check", "video_status", {"job_id": _JOB}))
        assert denied.is_error
        assert json.loads(denied.content)["error_class"] == "ToolError"
        assert len(transport.requests) == before_foreign

        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-rotated-key")
        changed_credential = await handler(
            ToolCall("rotated-check", "video_status", {"job_id": _JOB})
        )
        assert changed_credential.is_error
        assert len(transport.requests) == before_foreign
        monkeypatch.setenv("OPENROUTER_API_KEY", _KEY)

        transport.ready = True
        resumed = await handler(
            ToolCall("resume-check", "video_status", {"job_id": _JOB, "filename": "resumed.mp4"})
        )
        assert not resumed.is_error
        payload = json.loads(resumed.content)
        assert payload["status"] == "ok"
        assert payload["job_id"] == _JOB
        assert Path(payload["path"]).read_bytes() == _MP4
        before_cached = len(transport.requests)
        cached = await handler(ToolCall("cached-check", "video_status", {"job_id": _JOB}))
        assert cached.content == resumed.content
        assert len(transport.requests) == before_cached
        assert len(context.published_artifacts) == 1
        assert sum(request.method == "POST" for request in transport.requests) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        media.configure_video_generation(None)


@pytest.mark.parametrize("tool_name", ["video_generate", "video_status"])
@pytest.mark.parametrize("repeat_stop", [False, True])
async def test_stop_during_video_publication_settles_once_before_concurrent_status(
    tool_name: str, repeat_stop: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", _KEY)
    monkeypatch.setattr(media, "_video_job_sessions", {})
    media.configure_video_generation(VideoGenerationConfig(enabled=True, primary=_MODEL))
    transport = _VideoHTTP()
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )
    original_generate = video_generation.generate_openrouter_video

    async def generate(**kwargs: Any) -> Any:
        return await original_generate(**kwargs, poll_interval_seconds=0.001)

    monkeypatch.setattr(video_generation, "generate_openrouter_video", generate)
    context = _context(tmp_path)
    registry = _registry()
    handler = build_tool_handler(registry, context)
    loop = asyncio.get_running_loop()
    publish_started = asyncio.Event()
    publish_finished = asyncio.Event()
    release_publication = threading.Event()
    original_publish = media._publish_generated_video_artifact
    publish_calls = 0

    def blocked_publish(*args: Any, **kwargs: Any) -> Any:
        nonlocal publish_calls
        publish_calls += 1
        loop.call_soon_threadsafe(publish_started.set)
        try:
            if not release_publication.wait(timeout=5):
                raise AssertionError("The publication worker was not released")
            return original_publish(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(publish_finished.set)

    monkeypatch.setattr(media, "_publish_generated_video_artifact", blocked_publish)
    initial_task: asyncio.Task[Any] | None = None
    turn_task: asyncio.Task[Any] | None = None
    status_task: asyncio.Task[Any] | None = None
    try:
        if tool_name == "video_status":
            initial_task = asyncio.create_task(
                handler(ToolCall("initial-request", "video_generate", {"prompt": "A paper kite"}))
            )
            await asyncio.wait_for(transport.poll_started.wait(), timeout=1)
            initial_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await initial_task
        transport.ready = True
        provider = _VideoToolProvider(
            tool_name=tool_name,
            arguments={"job_id": _JOB, "filename": "initial.mp4"}
            if tool_name == "video_status" else None,
        )
        agent = Agent(
            provider=provider,
            config=AgentConfig(timeout=3, max_iterations=3),
            tool_definitions=registry.to_tool_definitions(context),
            tool_handler=handler,
            tool_registry=registry,
            tool_context=context,
            session_key=context.session_key,
        )

        async def consume() -> None:
            async for _event in agent.run_turn("Deliver the synthetic video."):
                pass

        turn_task = asyncio.create_task(consume())
        await asyncio.wait_for(publish_started.wait(), timeout=1)
        assert Path(tmp_path / "workspace" / "initial.mp4").read_bytes() == _MP4
        turn_task.cancel()
        status_task = asyncio.create_task(
            handler(ToolCall("concurrent-check", "video_status", {
                "job_id": _JOB, "filename": "concurrent.mp4",
            }))
        )
        await asyncio.sleep(0.02)
        receipt = media._video_job_receipt(_JOB)
        assert receipt is not None
        assert receipt.completed_result is not None
        assert receipt.completed_payload is None
        assert receipt.status_lock.locked()
        assert not status_task.done()
        assert publish_calls == 1
        assert sum(request.url.path.endswith("/content") for request in transport.requests) == 1

        if repeat_stop:
            turn_task.cancel()
            await asyncio.sleep(0.02)
            assert receipt.status_lock.locked()
            assert not status_task.done()

        release_publication.set()
        await asyncio.wait_for(publish_finished.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=1)
        resumed = await asyncio.wait_for(status_task, timeout=1)
        assert not resumed.is_error
        assert json.loads(resumed.content)["status"] == "ok"
        assert json.loads(resumed.content)["path"].endswith("/initial.mp4")
        receipt = media._video_job_receipt(_JOB)
        assert receipt is not None
        assert receipt.completed_payload == resumed.content
        assert not receipt.status_lock.locked()
        assert len(context.published_artifacts) == 1
        artifacts = ArtifactStore(tmp_path / "artifacts").list_refs(
            session_id="synthetic-video-session", limit=10
        )
        assert artifacts.total_count == 1
        assert publish_calls == 1
        assert sum(request.method == "POST" for request in transport.requests) == 1
        assert sum(request.url.path.endswith("/content") for request in transport.requests) == 1
        assert not (tmp_path / "workspace" / "concurrent.mp4").exists()
    finally:
        release_publication.set()
        tasks = [task for task in (initial_task, turn_task, status_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if publish_started.is_set():
            await asyncio.wait_for(publish_finished.wait(), timeout=1)
        media.configure_video_generation(None)
