"""The turn scope stack must survive async-generator finalization.

Regression for issue #1569: an abandoned ``TurnRunner.run``-shaped generator
is finalized by asyncio's async-generator finalizer, which runs ``aclose()``
in a *fresh* asyncio Context.  The turn scope stack wrapped around the
generator (``managed_toolchain_state_scope``, ``runtime_pack_state_scope``,
``sandbox_policy_scope``, ``git_run_mode_scope``, ``task_process_scope``)
resets its ``ContextVar`` tokens in ``finally`` blocks; a reset issued in a
different Context raises ``ValueError``, the exception chain escaped the
finalizer task, and every ``chat.abort`` on a live gateway logged
``Task exception was never retrieved`` (5 aborts → 5 crashes).
"""

from __future__ import annotations

import asyncio
import gc
import logging
from pathlib import Path

import pytest

from opensquilla.git_runtime import git_run_mode_scope
from opensquilla.process_tree import task_process_scope
from opensquilla.run_mode import RunMode
from opensquilla.runtime_packs.manager import runtime_pack_state_scope
from opensquilla.sandbox.integration import sandbox_policy_scope
from opensquilla.sandbox.policy_models import SandboxPolicy as StoredSandboxPolicy
from opensquilla.skills.toolchains.manager import managed_toolchain_state_scope


class _SlowRunEvent:
    """Stand-in event so the generator has something to yield."""


async def _turn_shaped_generator(state_dir: str):
    """A generator shaped like ``TurnRunner.run``: scope stack + yields."""
    with (
        managed_toolchain_state_scope(state_dir),
        runtime_pack_state_scope(state_dir),
        sandbox_policy_scope(StoredSandboxPolicy()),
        git_run_mode_scope(RunMode.SAFE),
        task_process_scope(state_dir, session_key="s", task_id="t"),
    ):
        yield _SlowRunEvent()
        # Abandoned while suspended here, exactly like a turn generator whose
        # consumer went away without closing it.
        await asyncio.sleep(3600)
        yield _SlowRunEvent()


async def _advance_once(gen) -> None:
    await gen.__anext__()


@pytest.mark.asyncio
async def test_turn_scope_stack_survives_asyncgen_finalizer(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    state_dir = str(tmp_path / "state")
    gen = _turn_shaped_generator(state_dir)

    # First advance binds the scope stack's ContextVar tokens to *this*
    # task's Context, then leaves the generator suspended mid-turn.
    await _advance_once(gen)

    # Drop every reference: finalization now belongs to asyncio's
    # async-generator finalizer hook, which runs aclose() in a fresh Context.
    # "Task exception was never retrieved" is logged when the finished
    # finalizer task itself is collected, so both collections and the waits
    # must sit inside the capture window.
    del gen
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        gc.collect()  # schedule the finalizer task
        await asyncio.sleep(0.1)  # let aclose() run to completion
        gc.collect()  # collect the finished task → unretrieved logged here
        await asyncio.sleep(0.05)
    unretrieved = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "Task exception was never retrieved" in record.getMessage()
    ]
    assert unretrieved == [], (
        "scope stack crashed during async-generator finalization: "
        f"{[r.getMessage() for r in unretrieved]}"
    )
