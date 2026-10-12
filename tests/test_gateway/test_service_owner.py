from __future__ import annotations

import asyncio

import pytest

from opensquilla.gateway.service_owner import OptionalServiceOwner


@pytest.mark.asyncio
async def test_start_once_is_single_flight_and_publishes_fencing_metadata() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[int, int]] = []
    descriptor: dict[str, object] = {}

    async def starter(*, generation: int, config_revision: int) -> str:
        calls.append((generation, config_revision))
        started.set()
        await release.wait()
        return "ready"

    owner = OptionalServiceOwner(
        "mcp",
        starter,
        config_revision=7,
        descriptor=descriptor,
    )
    first = asyncio.create_task(owner.start_once())
    second = asyncio.create_task(owner.start_once())
    await started.wait()
    assert descriptor["status"] == "starting"
    release.set()
    assert await first == "ready"
    assert await second == "ready"
    assert calls == [(1, 7)]
    assert descriptor["status"] == "ready"
    assert descriptor["config_revision"] == 7
    assert descriptor["owner_generation"] == 1


@pytest.mark.asyncio
async def test_quiesce_fences_a_never_returning_optional_service_and_late_ready() -> None:
    started = asyncio.Event()
    release_after_cancel = asyncio.Event()
    descriptor: dict[str, object] = {}

    async def starter(*, generation: int, config_revision: int) -> str:
        del generation, config_revision
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # Model an SDK call which catches cancellation and completes later.
            await release_after_cancel.wait()
            return "ready"
        raise AssertionError("starter unexpectedly returned")

    owner = OptionalServiceOwner(
        "channel",
        starter,
        config_revision=11,
        descriptor=descriptor,
    )
    task = asyncio.create_task(owner.start_once())
    await started.wait()

    await asyncio.wait_for(owner.quiesce(), timeout=0.5)
    assert descriptor["status"] == "stopping"
    assert descriptor["owner_generation"] == 2

    release_after_cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    # The late completion cannot resurrect readiness in the fenced owner.
    assert descriptor["status"] == "stopping"
    assert descriptor["owner_generation"] == 2


@pytest.mark.asyncio
async def test_close_calls_lifecycle_callbacks_with_new_generation() -> None:
    seen: list[tuple[str, int, int]] = []

    async def starter(*, generation: int, config_revision: int) -> str:
        seen.append(("start", generation, config_revision))
        return "ready"

    async def quiescer(*, generation: int, config_revision: int) -> None:
        seen.append(("quiesce", generation, config_revision))

    async def drainer(*, generation: int, config_revision: int) -> None:
        seen.append(("drain", generation, config_revision))

    async def closer(*, generation: int, config_revision: int) -> None:
        seen.append(("close", generation, config_revision))

    owner = OptionalServiceOwner(
        "cron",
        starter,
        config_revision=3,
        quiescer=quiescer,
        drainer=drainer,
        closer=closer,
    )
    assert await owner.start_once() == "ready"
    await owner.close()
    assert seen == [
        ("start", 1, 3),
        ("quiesce", 2, 3),
        ("drain", 2, 3),
        ("close", 3, 3),
    ]
    assert owner.state == "stopped"
    with pytest.raises(RuntimeError, match="closed"):
        await owner.start_once()
