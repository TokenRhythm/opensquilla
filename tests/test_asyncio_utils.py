from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import Any

import pytest

from opensquilla.asyncio_utils import create_background_task, reset_contextvar_token


async def _return_value(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_create_background_task_returns_real_task() -> None:
    task = create_background_task(_return_value("done"))

    assert isinstance(task, asyncio.Task)
    assert await task == "done"


@pytest.mark.asyncio
async def test_create_background_task_closes_unconsumed_coroutine_for_stubbed_non_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()

    def fake_create_task(coro: Coroutine[Any, Any, Any]) -> object:
        return sentinel

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    coro = _return_value("unused")
    assert coro.cr_frame is not None

    result = create_background_task(coro)

    assert result is sentinel
    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_create_background_task_closes_unconsumed_coroutine_when_create_task_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CreateTaskError(RuntimeError):
        pass

    def fake_create_task(coro: Coroutine[Any, Any, Any]) -> object:
        raise CreateTaskError("task creation failed")

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    coro = _return_value("unused")
    assert coro.cr_frame is not None

    with pytest.raises(CreateTaskError, match="task creation failed"):
        create_background_task(coro)

    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_reset_contextvar_token_tolerates_cross_context_teardown() -> None:
    var: ContextVar[str | None] = ContextVar("asyncio_utils_cross_context_test", default=None)

    def issue_token() -> Any:
        return var.set("scoped")

    issuing_context = contextvars.copy_context()
    token = issuing_context.run(issue_token)

    async def teardown_in_foreign_context() -> None:
        # Simulates asyncio's async-generator finalizer, which closes an
        # abandoned generator in a fresh Context: the plain
        # ``var.reset(token)`` this helper wraps raises
        # ``ValueError: ... was created in a different Context`` there.
        reset_contextvar_token(var, token)

    await asyncio.create_task(
        teardown_in_foreign_context(), context=contextvars.copy_context()
    )

    # The issuing Context keeps its value (a foreign Context cannot reset it),
    # and the helper swallowed the ValueError instead of letting it escape the
    # scope teardown.
    assert issuing_context.run(var.get) == "scoped"

    # Same-Context reset still works — the helper is not a blanket no-op.
    same_context_token = var.set("transient")
    reset_contextvar_token(var, same_context_token)
    assert var.get("sentinel") == "sentinel"
