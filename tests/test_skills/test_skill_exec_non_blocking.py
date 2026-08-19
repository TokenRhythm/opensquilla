"""Regression test for skill_exec event-loop friendliness.

A skill_exec step that runs a long subprocess must NOT block the
caller's asyncio event loop. The HTTP gateway and the meta orchestrator
share that loop; a synchronous ``subprocess.run`` (the prior
implementation) would freeze ``/healthz`` / ``/control/`` while a wrapped
CLI polled a remote API for minutes.

This test reproduces the scenario with a 2-second sleep subprocess and
verifies a parallel coroutine still gets scheduled (within a 200ms slop)
while the subprocess is running.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from opensquilla.skills.meta.executors.skill_exec import run_skill_exec_step
from opensquilla.skills.meta.types import MetaStep
from opensquilla.skills.types import SkillLayer, SkillSpec


def _spec(base_dir: Path, command: str, *, timeout: float = 30.0) -> SkillSpec:
    return SkillSpec(
        name="slow-skill",
        description="test",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        base_dir=str(base_dir),
        entrypoint={"command": command, "parse": "text", "timeout": timeout},
    )


class _Loader:
    def __init__(self, spec: SkillSpec) -> None:
        self._spec = spec

    def get_by_name(self, name: str) -> SkillSpec | None:
        return self._spec if name == self._spec.name else None


@pytest.mark.asyncio
async def test_skill_exec_does_not_block_event_loop(tmp_path: Path) -> None:
    """A 2s sleep subprocess must coexist with a 50ms-tick coroutine.

    If skill_exec is event-loop-friendly, the tick coroutine runs ~40
    times during the 2-second subprocess. If skill_exec blocks (the bug
    we fixed), the tick coroutine cannot run AT ALL during the
    subprocess and finishes with ~0 ticks.
    """
    sleep_script = tmp_path / "sleep_two.py"
    sleep_script.write_text("import time; time.sleep(2.0)\n", encoding="utf-8")
    spec = _spec(tmp_path, f"{sys.executable} {sleep_script}")
    step = MetaStep(id="s1", kind="skill_exec", skill="slow-skill")

    tick_count = 0

    async def ticker() -> None:
        nonlocal tick_count
        # Stop ticking once the subprocess is clearly done.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            tick_count += 1
            await asyncio.sleep(0.05)

    start = time.monotonic()
    _, _ = await asyncio.gather(
        run_skill_exec_step(
            step,
            effective_skill="slow-skill",
            inputs={},
            outputs={},
            skill_loader=_Loader(spec),
            workspace_dir=str(tmp_path),
        ),
        ticker(),
    )
    wall = time.monotonic() - start

    # Sanity: the test really waited ~2s (not blocked something out).
    assert wall < 4.5, f"unexpectedly slow run: {wall:.2f}s"
    # The ticker must have advanced many times during the 2s sleep —
    # a blocking subprocess.run would have yielded 0 ticks until done.
    # ~40 ticks ideal; ≥20 is a comfortable lower bound for noisy CI.
    assert tick_count >= 20, (
        f"event loop was blocked during skill_exec subprocess: "
        f"only {tick_count} ticks in {wall:.2f}s (expected ≥20)"
    )


@pytest.mark.skipif(os.name != "posix", reason="deep process-group check is POSIX-specific")
@pytest.mark.asyncio
async def test_skill_exec_timeout_cleans_deep_owned_tree_and_registry(tmp_path: Path) -> None:
    from opensquilla import process_tree

    leaked = tmp_path / "synthetic-descendant-leaked"
    child = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(1.0); "
        f"pathlib.Path({str(leaked)!r}).write_text('unexpected')"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(5.0)\n",
        encoding="utf-8",
    )
    spec = _spec(
        tmp_path,
        f"{shlex.quote(sys.executable)} {shlex.quote(str(parent))}",
        timeout=0.2,
    )

    with process_tree.task_process_scope(
        tmp_path,
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            await run_skill_exec_step(
                MetaStep(id="timeout", kind="skill_exec", skill=spec.name),
                effective_skill=spec.name,
                inputs={},
                outputs={},
                skill_loader=_Loader(spec),
                workspace_dir=str(tmp_path),
            )

    await asyncio.sleep(1.1)
    assert leaked.exists() is False
    assert process_tree._load_owner_records(tmp_path) == ()
