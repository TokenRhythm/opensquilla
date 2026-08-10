from __future__ import annotations

import asyncio
import math
import time

import pytest

from opensquilla.provider.admission import (
    ProviderAdmissionCapacityError,
    ProviderAdmissionController,
    ProviderAdmissionLeaseGuard,
    ProviderAdmissionSettings,
    ProviderAdmissionTimeoutError,
    get_shared_provider_admission_controller,
    reset_shared_provider_admission_controllers_for_tests,
)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, 0])
def test_settings_reject_nonfinite_or_nonpositive_queue_timeout(
    value: float,
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ProviderAdmissionSettings(queue_timeout_seconds=value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("global_max_in_flight", True),
        ("provider_default_max_in_flight", 1.5),
        ("deployment_default_max_in_flight", "2"),
        ("provider_limits", {"openrouter": True}),
        ("deployment_limits", {"openrouter/model": 1.5}),
        ("deployment_weights", {"openrouter/model": "2"}),
    ],
)
def test_settings_reject_bool_or_coerced_capacity_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ProviderAdmissionSettings(**{field: value})


def test_controller_rejects_cross_event_loop_reuse() -> None:
    controller = ProviderAdmissionController(ProviderAdmissionSettings())

    async def bind_once() -> None:
        lease = await controller.acquire(
            provider="openrouter",
            model="model",
            role="proposer",
        )
        lease.release()

    asyncio.run(bind_once())

    with pytest.raises(RuntimeError, match="across event loops"):
        asyncio.run(
            controller.acquire(
                provider="openrouter",
                model="model",
                role="aggregator",
            )
        )


@pytest.mark.asyncio
async def test_global_provider_and_deployment_caps_hold_under_50_workers() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=5,
            provider_default_max_in_flight=3,
            deployment_default_max_in_flight=2,
            queue_timeout_seconds=2,
        )
    )
    lock = asyncio.Lock()
    active_global = 0
    active_provider: dict[str, int] = {}
    active_deployment: dict[str, int] = {}
    observed = {"global": 0, "provider": 0, "deployment": 0}

    async def worker(index: int) -> None:
        nonlocal active_global
        provider = "openrouter" if index % 3 else "dashscope"
        model = f"model-{index % 4}"
        lease = await controller.acquire(
            provider=provider,
            model=model,
            role="proposer",
            absolute_deadline=time.monotonic() + 2,
        )
        endpoint = f"{provider}/{model}"
        try:
            async with lock:
                active_global += 1
                active_provider[provider] = active_provider.get(provider, 0) + 1
                active_deployment[endpoint] = active_deployment.get(endpoint, 0) + 1
                observed["global"] = max(observed["global"], active_global)
                observed["provider"] = max(observed["provider"], active_provider[provider])
                observed["deployment"] = max(observed["deployment"], active_deployment[endpoint])
            await asyncio.sleep(0.002)
        finally:
            async with lock:
                active_global -= 1
                active_provider[provider] -= 1
                active_deployment[endpoint] -= 1
            lease.release()

    await asyncio.gather(*(worker(index) for index in range(50)))

    assert observed["global"] <= 5
    assert observed["provider"] <= 3
    assert observed["deployment"] <= 2
    assert controller.snapshot() == {
        **controller.snapshot(),
        "global_in_flight": 0,
        "global_queued": 0,
        "active_leases": 0,
        "total_acquired": 50,
        "total_released": 50,
        "total_timeouts": 0,
        "total_cancelled": 0,
        "provider_in_flight": {},
        "deployment_in_flight": {},
    }


@pytest.mark.asyncio
async def test_provider_cap_is_shared_across_deployments() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=10,
            provider_default_max_in_flight=2,
            deployment_default_max_in_flight=10,
            queue_timeout_seconds=1,
        )
    )
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def worker(model: str) -> None:
        nonlocal active, maximum
        async with await controller.acquire(
            provider="openrouter",
            model=model,
            role="proposer",
        ):
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                release.set()
            await release.wait()
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(worker(f"model-{index}") for index in range(8)))

    assert maximum == 2
    assert controller.active_leases == 0


@pytest.mark.asyncio
async def test_deployment_weight_reduces_effective_concurrency() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=8,
            provider_default_max_in_flight=8,
            deployment_default_max_in_flight=4,
            deployment_weights={"openrouter/expensive": 2},
            queue_timeout_seconds=1,
        )
    )
    active = 0
    maximum = 0

    async def worker() -> None:
        nonlocal active, maximum
        async with await controller.acquire(
            provider="openrouter",
            model="expensive",
            role="aggregator",
        ):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.005)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(12)))

    assert maximum == 2
    assert controller.snapshot()["global_in_flight"] == 0


@pytest.mark.asyncio
async def test_queue_timeout_is_typed_not_started_and_releases_partial_capacity() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=2,
            provider_default_max_in_flight=2,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=0.02,
        )
    )
    holder = await controller.acquire(
        provider="openrouter",
        model="same",
        role="proposer",
    )

    with pytest.raises(ProviderAdmissionTimeoutError) as raised:
        await controller.acquire(
            provider="openrouter",
            model="same",
            role="proposer_recovery",
            absolute_deadline=time.monotonic() + 1,
        )

    assert raised.value.code == "provider_admission_timeout"
    assert "physical dispatch" in str(raised.value)
    assert controller.snapshot()["global_in_flight"] == 1
    holder.release()
    assert controller.snapshot()["global_in_flight"] == 0
    assert controller.snapshot()["total_timeouts"] == 1


@pytest.mark.asyncio
async def test_absolute_deadline_shortens_queue_wait() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=10,
        )
    )
    holder = await controller.acquire(
        provider="openrouter",
        model="same",
        role="proposer",
    )
    started = time.monotonic()

    with pytest.raises(ProviderAdmissionTimeoutError):
        await controller.acquire(
            provider="openrouter",
            model="same",
            role="aggregator",
            absolute_deadline=started + 0.02,
        )

    assert time.monotonic() - started < 0.2
    holder.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_or_block_followers() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=1,
        )
    )
    holder = await controller.acquire(
        provider="openrouter",
        model="same",
        role="proposer",
    )
    cancelled = asyncio.create_task(
        controller.acquire(
            provider="openrouter",
            model="same",
            role="proposer_recovery",
        )
    )
    follower = asyncio.create_task(
        controller.acquire(
            provider="openrouter",
            model="same",
            role="aggregator",
        )
    )
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    holder.release()
    follower_lease = await asyncio.wait_for(follower, timeout=0.2)
    follower_lease.release()

    snapshot = controller.snapshot()
    assert snapshot["active_leases"] == 0
    assert snapshot["global_in_flight"] == 0
    assert snapshot["global_queued"] == 0
    assert snapshot["total_cancelled"] == 1


@pytest.mark.asyncio
async def test_queued_narrow_deployment_does_not_reserve_global_capacity() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=2,
            provider_default_max_in_flight=2,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=1,
        )
    )
    holder = await controller.acquire(
        provider="openrouter",
        model="busy",
        role="proposer",
    )
    queued = asyncio.create_task(
        controller.acquire(
            provider="openrouter",
            model="busy",
            role="proposer_recovery",
        )
    )
    await asyncio.sleep(0)

    assert controller.snapshot()["global_in_flight"] == 1
    assert controller.snapshot()["global_queued"] == 1
    holder.release()
    replacement = await asyncio.wait_for(queued, timeout=0.2)
    replacement.release()


@pytest.mark.asyncio
async def test_cancelled_holder_context_releases_capacity() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=1,
        )
    )
    entered = asyncio.Event()

    async def holder() -> None:
        async with await controller.acquire(
            provider="openrouter",
            model="same",
            role="proposer",
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(holder())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    replacement = await asyncio.wait_for(
        controller.acquire(
            provider="openrouter",
            model="same",
            role="aggregator",
        ),
        timeout=0.2,
    )
    replacement.release()
    assert controller.active_leases == 0


@pytest.mark.asyncio
async def test_controller_is_strict_fifo_without_starvation() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=2,
            provider_default_max_in_flight=2,
            deployment_default_max_in_flight=2,
        )
    )
    holder = await controller.acquire(
        provider="openrouter",
        model="same",
        role="holder",
        weight=2,
    )
    order: list[str] = []

    async def queued(name: str, weight: int) -> None:
        lease = await controller.acquire(
            provider="openrouter",
            model="same",
            role=name,
            weight=weight,
            absolute_deadline=time.monotonic() + 1,
        )
        order.append(name)
        await asyncio.sleep(0)
        lease.release()

    large = asyncio.create_task(queued("large", 2))
    small = asyncio.create_task(queued("small", 1))
    await asyncio.sleep(0)
    holder.release()
    await asyncio.gather(large, small)

    assert order == ["large", "small"]


@pytest.mark.asyncio
async def test_cleanup_guard_holds_capacity_and_runs_feedback_before_drain() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=1,
        )
    )
    first = await controller.acquire(
        provider="openrouter",
        model="same",
        role="aggregator",
    )
    order: list[str] = []

    def observe_cleanup(
        future: asyncio.Future[object],
        _: str,
    ) -> None:
        def defer_observation(_: asyncio.Future[object]) -> None:
            asyncio.get_running_loop().call_soon(
                order.append,
                "cleanup_observed",
            )

        future.add_done_callback(defer_observation)

    guard = ProviderAdmissionLeaseGuard(
        first,
        pending_cleanup_tracker=observe_cleanup,
        before_release=lambda: order.append("health_bench"),
    )
    cleanup = asyncio.get_running_loop().create_future()
    guard.track_cleanup(cleanup, "aggregator_deferred_close")
    guard.finish()

    async def follower() -> None:
        lease = await controller.acquire(
            provider="openrouter",
            model="same",
            role="next_turn",
        )
        order.append("next_chat")
        lease.release()

    next_turn = asyncio.create_task(follower())
    await asyncio.sleep(0)
    assert next_turn.done() is False
    assert controller.snapshot()["global_in_flight"] == 1
    assert guard.pending_cleanup_count == 1

    cleanup.set_result(True)
    await asyncio.wait_for(next_turn, timeout=0.2)

    assert order == ["cleanup_observed", "health_bench", "next_chat"]
    assert guard.released is True
    assert controller.snapshot()["global_in_flight"] == 0


@pytest.mark.asyncio
async def test_cleanup_guard_releases_after_tracker_registration_failure() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(global_max_in_flight=1)
    )
    lease = await controller.acquire(
        provider="openrouter",
        model="same",
        role="aggregator",
    )

    def broken_tracker(_: asyncio.Future[object], __: str) -> None:
        raise RuntimeError("synthetic cleanup observer failure")

    guard = ProviderAdmissionLeaseGuard(
        lease,
        pending_cleanup_tracker=broken_tracker,
    )
    cleanup = asyncio.get_running_loop().create_future()
    with pytest.raises(RuntimeError, match="synthetic cleanup observer failure"):
        guard.track_cleanup(cleanup, "aggregator_deferred_close")
    guard.finish()
    assert controller.active_leases == 1

    cleanup.set_result(True)
    for _ in range(10):
        await asyncio.sleep(0)
        if controller.active_leases == 0:
            break

    assert guard.released is True
    assert controller.active_leases == 0


@pytest.mark.asyncio
async def test_shared_controller_does_not_create_capacity_islands() -> None:
    reset_shared_provider_admission_controllers_for_tests()
    settings = ProviderAdmissionSettings(global_max_in_flight=3)

    first = get_shared_provider_admission_controller(settings)
    second = get_shared_provider_admission_controller(settings)

    assert first is second
    reset_shared_provider_admission_controllers_for_tests()


@pytest.mark.asyncio
async def test_weight_larger_than_capacity_is_typed_rejection() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=2,
            provider_default_max_in_flight=2,
            deployment_default_max_in_flight=2,
            deployment_weights={"openrouter/oversized": 3},
        )
    )

    with pytest.raises(ProviderAdmissionCapacityError) as raised:
        await controller.acquire(
            provider="openrouter",
            model="oversized",
            role="aggregator",
        )

    assert raised.value.code == "provider_admission_capacity"
    assert controller.active_leases == 0

    with pytest.raises(ProviderAdmissionCapacityError):
        await controller.acquire(
            provider="openrouter",
            model="normal",
            role="aggregator",
            weight=True,
        )


@pytest.mark.asyncio
async def test_admission_error_message_never_contains_model_or_secret() -> None:
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=0.001,
        )
    )
    secret = "sk-secret-value"
    holder = await controller.acquire(
        provider="openrouter",
        model=secret,
        role="proposer",
    )
    try:
        with pytest.raises(ProviderAdmissionTimeoutError) as raised:
            await controller.acquire(
                provider="openrouter",
                model=secret,
                role="aggregator",
            )
        assert secret not in str(raised.value)
    finally:
        holder.release()
