import pytest

from opensquilla.application.sandbox_runtime import (
    InMemorySandboxRuntimePort,
    SandboxRuntime,
)


@pytest.mark.asyncio
async def test_sandbox_runtime_keeps_policy_and_setup_wire_out_of_module() -> None:
    port = InMemorySandboxRuntimePort(
        setup_status={"state": "ready", "platform": "linux"},
        capability={"available": True},
        policy={"schemaVersion": 2, "policyVersion": 4},
    )
    runtime = SandboxRuntime(port)

    assert (await runtime.setup_status())["state"] == "ready"
    assert (await runtime.capability())["available"] is True
    assert (await runtime.policy())["policyVersion"] == 4


@pytest.mark.asyncio
async def test_sandbox_runtime_validates_mode_and_policy_version() -> None:
    runtime = SandboxRuntime(InMemorySandboxRuntimePort())

    with pytest.raises(ValueError, match="run mode"):
        await runtime.set_run_mode("unsafe")
    with pytest.raises(ValueError, match="policy version"):
        await runtime.update_policy(True, {})


@pytest.mark.asyncio
async def test_sandbox_runtime_updates_mode_through_port() -> None:
    runtime = SandboxRuntime(InMemorySandboxRuntimePort())

    result = await runtime.set_run_mode("safe")

    assert result == {"runMode": "safe", "source": "preference"}
