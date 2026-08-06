"""MetaOrchestrator persistence offload tests.

The orchestrator's ``_to_thread`` used to be a synchronous call disguised
as thread offload, so every MetaRunWriter commit (busy_timeout=5000) ran
on the event loop. These tests pin that writer calls issued from async
context actually leave the loop thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from opensquilla.skills.meta.orchestrator import MetaOrchestrator, _to_thread
from opensquilla.skills.meta.types import MetaPlan, MetaStep


async def test_to_thread_runs_fn_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    worker_thread = await _to_thread(threading.get_ident)
    assert worker_thread != loop_thread


async def test_to_thread_forwards_args_and_kwargs() -> None:
    def combine(a: int, *, b: int) -> int:
        return a + b

    assert await _to_thread(combine, 2, b=3) == 5


async def test_to_thread_preserves_worker_exception() -> None:
    def fail() -> None:
        raise RuntimeError("writer failed")

    with pytest.raises(RuntimeError, match="writer failed"):
        await _to_thread(fail)


class _BlockingBeginRunWriter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._root_ids: list[str] = []

    def begin_run_sync(self, *, root_id: str) -> str:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release begin_run_sync")
        with self._lock:
            self._root_ids.append(root_id)
        self.finished.set()
        return root_id

    def purge(self) -> None:
        with self._lock:
            self._root_ids.clear()

    def root_ids(self) -> list[str]:
        with self._lock:
            return list(self._root_ids)


async def test_to_thread_drains_writer_before_repeated_cancellation_propagates() -> None:
    writer = _BlockingBeginRunWriter()
    call = asyncio.create_task(
        _to_thread(writer.begin_run_sync, root_id="root"),
    )
    assert await asyncio.to_thread(writer.started.wait, 1)

    try:
        call.cancel()
        await asyncio.sleep(0)
        assert not call.done()

        call.cancel()
        await asyncio.sleep(0)
        assert not call.done()
    finally:
        writer.release.set()

    with pytest.raises(asyncio.CancelledError):
        await call

    assert writer.finished.is_set()
    writer.purge()
    await asyncio.sleep(0)
    assert writer.root_ids() == []


class _ThreadRecordingWriter:
    """Minimal writer double that records the calling thread per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.begin_kwargs: dict[str, Any] = {}

    def begin_step_sync(self, **kwargs: Any) -> None:
        self.calls.append(("begin_step_sync", threading.get_ident()))
        self.begin_kwargs = kwargs

    def finish_step_sync(self, **_kwargs: Any) -> None:
        self.calls.append(("finish_step_sync", threading.get_ident()))

    def on_step_failover_sync(self, **_kwargs: Any) -> None:
        self.calls.append(("on_step_failover_sync", threading.get_ident()))


async def test_step_persistence_hooks_offload_writer_calls() -> None:
    async def _unused_runner(_system_prompt: str, _user_message: str) -> Any:
        raise AssertionError("agent_runner must not be invoked")

    writer = _ThreadRecordingWriter()
    plan = MetaPlan(
        name="demo",
        triggers=("t",),
        priority=0,
        steps=(MetaStep(id="s1", skill="x", kind="agent"),),
    )
    orch = MetaOrchestrator(agent_runner=_unused_runner, skill_loader=object())
    on_begin, on_finish, on_failover = orch._step_persistence_hooks(
        run_id="r1",
        plan=plan,
        writer=writer,  # type: ignore[arg-type]
        usage_scope_prefix="r1",
    )
    assert on_begin is not None
    assert on_finish is not None
    assert on_failover is not None

    await on_begin("s1", "x", {})
    await on_finish("s1", "ok", "out", None)
    await on_failover("s1", "s2", "boom")

    loop_thread = threading.get_ident()
    assert [name for name, _ in writer.calls] == [
        "begin_step_sync",
        "finish_step_sync",
        "on_step_failover_sync",
    ]
    assert all(thread_id != loop_thread for _, thread_id in writer.calls)


async def test_step_persistence_strips_current_run_receipt_proof_only() -> None:
    async def _unused_runner(_system_prompt: str, _user_message: str) -> Any:
        raise AssertionError("agent_runner must not be invoked")

    writer = _ThreadRecordingWriter()
    plan = MetaPlan(
        name="demo",
        triggers=("t",),
        priority=0,
        steps=(MetaStep(id="audit", skill="audit", kind="skill_exec"),),
    )
    orch = MetaOrchestrator(agent_runner=_unused_runner, skill_loader=object())
    on_begin, _on_finish, _on_failover = orch._step_persistence_hooks(
        run_id="r1",
        plan=plan,
        writer=writer,  # type: ignore[arg-type]
        usage_scope_prefix="r1",
    )
    assert on_begin is not None

    rendered_inputs = {
        "run_dir": "/synthetic/run",
        "runtime": {
            "paid_submission_dispositions": '{"shot1_image":"receipt"}',
            "paid_submission_receipt_proofs": {
                "shot1_image": f"sha256:{'a' * 64}",
            },
            "fallback_outputs": {"1": ""},
        },
        "__opensquilla_paid_submission_receipt_proofs_v1__": "must-not-persist",
    }
    await on_begin("audit", "audit", rendered_inputs)

    assert writer.begin_kwargs["rendered_inputs"] == {
        "run_dir": "/synthetic/run",
        "runtime": {
            "paid_submission_dispositions": '{"shot1_image":"receipt"}',
            "fallback_outputs": {"1": ""},
        },
    }
    # The scheduler/executor-owned input remains intact for this live audit.
    assert "paid_submission_receipt_proofs" in rendered_inputs["runtime"]
