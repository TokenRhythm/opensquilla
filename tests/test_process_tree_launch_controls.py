from __future__ import annotations

import asyncio
import contextlib

import pytest

from opensquilla import process_tree


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 2), ("1", 1), ("8", 8), ("0", 1), ("99", 8), ("invalid", 2)],
)
def test_windows_process_launch_limit_is_bounded(
    monkeypatch, raw: str | None, expected: int
) -> None:
    if raw is None:
        monkeypatch.delenv(process_tree._WINDOWS_PROCESS_LAUNCH_CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(process_tree._WINDOWS_PROCESS_LAUNCH_CONCURRENCY_ENV, raw)
    assert process_tree._windows_process_launch_limit() == expected


@pytest.mark.asyncio
async def test_windows_launch_admission_cancellation_does_not_lose_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(process_tree._WINDOWS_PROCESS_LAUNCH_CONCURRENCY_ENV, "1")
    semaphore, limit = process_tree._windows_launch_semaphore()
    assert limit == 1

    await semaphore.acquire()
    waiter = asyncio.create_task(semaphore.acquire())
    await asyncio.sleep(0)
    assert not waiter.done()
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter

    semaphore.release()
    assert semaphore._value == 1  # noqa: SLF001 - verify cancellation did not leak a permit
