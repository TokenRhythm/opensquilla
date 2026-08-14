"""Unit tests for ``AttachmentStage`` driven directly (no full TurnRunner
stack).

Drives the stage through ``AttachmentStage.run`` with a recording
``AttachmentMessageBuilderPort`` fake. A raising-fake case exercises the
exception-propagation contract without the runtime wrapper.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.turn_runner.attachment_stage import (
    AttachmentStage,
    AttachmentStageInput,
)
from opensquilla.engine.turn_runner.outcome import StageOutcome


@dataclass
class _RecordingBuilder:
    return_value: list[Any] | None = None
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def build(
        self,
        message: str,
        attachments: list[dict],
        *,
        workspace_dir: str | Path | None = None,
        session_id: str | None = None,
    ) -> list[Any] | None:
        self.calls.append(
            {
                "message": message,
                "attachments": attachments,
                "workspace_dir": workspace_dir,
                "session_id": session_id,
            }
        )
        if self.raises is not None:
            raise self.raises("recording builder boom")
        return self.return_value


def _make_stage(
    *,
    builder: _RecordingBuilder | None = None,
) -> tuple[AttachmentStage, _RecordingBuilder]:
    builder = builder or _RecordingBuilder()
    return AttachmentStage(builder=builder), builder


@pytest.mark.parametrize("attachments_in", [None, []])
@pytest.mark.asyncio
async def test_no_attachments_returns_runtime_message_as_turn_input(
    attachments_in: list[dict] | None,
) -> None:
    """``attachments=None`` or ``[]`` -> builder returns ``None`` ->
    ``turn_input`` falls back to ``effective_runtime_message`` and
    ``extra_messages`` is ``None``. Stage normalizes ``None`` to ``[]``
    before invoking the port."""

    stage, builder = _make_stage(builder=_RecordingBuilder(return_value=None))
    inp = AttachmentStageInput(
        effective_runtime_message="hello",
        attachments=attachments_in,
    )

    outcome = await stage.run(inp)

    assert isinstance(outcome, StageOutcome)
    assert outcome.terminate is False
    assert outcome.output is not None
    assert outcome.output.extra_messages is None
    assert outcome.output.turn_input == "hello"
    assert builder.calls == [
        {
            "message": "hello",
            "attachments": [],
            "workspace_dir": None,
            "session_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_builder_returns_messages_clears_turn_input() -> None:
    """Builder returns a non-empty envelope -> ``turn_input`` becomes the
    empty string and the envelope is surfaced verbatim."""

    sentinel_envelope: list[Any] = [object()]
    stage, builder = _make_stage(
        builder=_RecordingBuilder(return_value=sentinel_envelope),
    )
    inp = AttachmentStageInput(
        effective_runtime_message="what is this?",
        attachments=[{"type": "image/png", "data": "AA=="}],
        workspace_dir="/workspace/main",
        session_id="session-a",
    )

    outcome = await stage.run(inp)

    assert outcome.output.extra_messages is sentinel_envelope
    assert outcome.output.turn_input == ""
    assert builder.calls == [
        {
            "message": "what is this?",
            "attachments": [{"type": "image/png", "data": "AA=="}],
            "workspace_dir": "/workspace/main",
            "session_id": "session-a",
        }
    ]


@pytest.mark.asyncio
async def test_empty_message_with_attachments() -> None:
    """Empty ``effective_runtime_message`` is forwarded verbatim and
    ``turn_input`` is still cleared when the builder returns an envelope."""

    sentinel_envelope: list[Any] = [object()]
    stage, _ = _make_stage(
        builder=_RecordingBuilder(return_value=sentinel_envelope),
    )
    inp = AttachmentStageInput(
        effective_runtime_message="",
        attachments=[{"type": "image/png", "data": "AA=="}],
    )

    outcome = await stage.run(inp)

    assert outcome.output.extra_messages is sentinel_envelope
    assert outcome.output.turn_input == ""


@pytest.mark.parametrize("exc_type", [ValueError, RuntimeError])
@pytest.mark.asyncio
async def test_builder_exception_propagates(
    exc_type: type[BaseException],
) -> None:
    """Both ``ValueError`` (legitimate validation failure) and arbitrary
    exceptions from the port propagate unchanged — the stage adds zero
    try/except."""

    stage, _ = _make_stage(builder=_RecordingBuilder(raises=exc_type))
    inp = AttachmentStageInput(
        effective_runtime_message="hi",
        attachments=[{"type": "image/png", "data": "AA=="}],
    )

    with pytest.raises(exc_type):
        await stage.run(inp)


@pytest.mark.asyncio
async def test_stage_name_and_output_frozen() -> None:
    """Pin the ``name`` identifier and the frozen-output contract."""

    assert AttachmentStage.name == "attachment_stage"
    stage, _ = _make_stage(builder=_RecordingBuilder(return_value=None))
    outcome = await stage.run(
        AttachmentStageInput(effective_runtime_message="hi", attachments=None)
    )
    output = outcome.output
    assert output is not None
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        output.turn_input = "tampered"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_builder_called_exactly_once_per_run() -> None:
    """The stage invokes the builder port exactly once per run."""

    stage, builder = _make_stage(builder=_RecordingBuilder(return_value=None))
    inp = AttachmentStageInput(
        effective_runtime_message="hi",
        attachments=[{"type": "image/png", "data": "AA=="}],
    )
    await stage.run(inp)
    assert len(builder.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_pre_router_stage_never_starts_materialization() -> None:
    stage, builder = _make_stage(builder=_RecordingBuilder(return_value=None))
    task = asyncio.create_task(
        stage.run(
            AttachmentStageInput(
                effective_runtime_message="hi",
                attachments=[{"type": "text/plain", "data": "eA=="}],
            )
        )
    )

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert builder.calls == []


class _StartedCancellableBuilder:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def build(self, *_args: Any, **_kwargs: Any) -> list[Any] | None:
        raise AssertionError("cancellable path was not used")

    def build_cancellable(
        self,
        *_args: Any,
        cancel_check: Any,
        **_kwargs: Any,
    ) -> list[Any] | None:
        self.started.set()
        try:
            while True:
                cancel_check()
                time.sleep(0.002)
        finally:
            self.stopped.set()


@pytest.mark.asyncio
async def test_cancellation_after_preparation_starts_stops_worker() -> None:
    builder = _StartedCancellableBuilder()
    task = asyncio.create_task(
        AttachmentStage(builder=builder).run(
            AttachmentStageInput(
                effective_runtime_message="hi",
                attachments=[{"type": "text/plain", "data": "eA=="}],
            )
        )
    )
    assert await asyncio.to_thread(builder.started.wait, 1.0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(builder.stopped.wait, 1.0)


@pytest.mark.asyncio
async def test_attachment_preparation_does_not_block_event_loop_ticker() -> None:
    class _SlowBuilder(_RecordingBuilder):
        def build(self, *args: Any, **kwargs: Any) -> list[Any] | None:
            time.sleep(0.08)
            return super().build(*args, **kwargs)

    stage_task = asyncio.create_task(
        AttachmentStage(builder=_SlowBuilder(return_value=None)).run(
            AttachmentStageInput(
                effective_runtime_message="hi",
                attachments=[{"type": "text/plain", "data": "eA=="}],
            )
        )
    )
    ticks = 0
    while not stage_task.done():
        ticks += 1
        await asyncio.sleep(0.005)
    await stage_task

    assert ticks >= 5


@pytest.mark.asyncio
async def test_attachment_preparation_deadline_stops_started_worker() -> None:
    builder = _StartedCancellableBuilder()

    with pytest.raises(TimeoutError, match="attachment preparation"):
        await AttachmentStage(builder=builder).run(
            AttachmentStageInput(
                effective_runtime_message="hi",
                attachments=[{"type": "text/plain", "data": "eA=="}],
                timeout_seconds=0.02,
            )
        )

    assert builder.started.is_set()
    assert await asyncio.to_thread(builder.stopped.wait, 1.0)


@pytest.mark.asyncio
async def test_attachment_preparation_bounds_executor_admission() -> None:
    class _CountingExecutor(concurrent.futures.ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=2)
            self.submitted = 0

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any):
            self.submitted += 1
            return super().submit(fn, *args, **kwargs)

    builder = _StartedCancellableBuilder()
    stage = AttachmentStage(builder=builder)
    stage._executor.shutdown(wait=True)
    executor = _CountingExecutor()
    stage._executor = executor
    tasks = [
        asyncio.create_task(
            stage.run(
                AttachmentStageInput(
                    effective_runtime_message="hi",
                    attachments=[{"type": "text/plain", "data": "eA=="}],
                )
            )
        )
        for _ in range(5)
    ]
    try:
        for _ in range(100):
            if executor.submitted == 4:
                break
            await asyncio.sleep(0.005)
        assert executor.submitted == 4
        await asyncio.sleep(0.02)
        assert executor.submitted == 4
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(executor.shutdown, True)
