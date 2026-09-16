from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from opensquilla.application.sandbox_runtime import (
    SandboxCapability,
    SandboxCapabilityUnavailableError,
    SandboxCommandPolicy,
    SandboxFilePolicy,
    SandboxNetworkPolicy,
    SandboxPolicyConflictError,
    SandboxPolicyDefaults,
    SandboxPolicyDocument,
    SandboxRunMode,
    SandboxRunModePreference,
    SandboxRuntime,
    SandboxRuntimeIdentityError,
    SandboxRuntimeOperation,
    SandboxRuntimeOperationKind,
    SandboxRuntimeOperationState,
    SandboxRuntimePackDocument,
    SandboxRuntimePolicy,
    SandboxRuntimeSource,
    SandboxSetupState,
    SandboxSetupStatus,
    SandboxSystemToolsPolicy,
)


def _setup_result(
    state: SandboxSetupState = SandboxSetupState.READY,
) -> SandboxSetupStatus:
    return SandboxSetupStatus(
        state=state,
        platform="test",
        message=f"Sandbox is {state.value}.",
    )


def _capability(*, available: bool = True) -> SandboxCapability:
    return SandboxCapability(
        available=available,
        backend="test",
        platform="test",
        code="ready" if available else "not_setup",
        reason="ready" if available else "not ready",
        setup_supported=True,
        restart_required=False,
        probe_version=1,
        capabilities=frozenset(),
    )


def _policy(*, policy_version: int = 0) -> SandboxPolicyDocument:
    return SandboxPolicyDocument(
        schema_version=2,
        policy_version=policy_version,
        files=SandboxFilePolicy(
            custom_deny_write_paths=(),
            recursive_delete_backup_enabled=True,
            backup_quota_bytes=3 * 1024**3,
        ),
        commands=SandboxCommandPolicy(
            require_approval_prefixes=(),
            auto_allow_prefixes=(),
            system_tools=SandboxSystemToolsPolicy.AUTO,
        ),
        network=SandboxNetworkPolicy(
            block_all_network=False,
            allow_domains=(),
            deny_domains=("telemetry.example",),
        ),
        runtimes=SandboxRuntimePolicy(
            enabled=True,
            python=True,
            node=True,
            git_bash=True,
        ),
    )


def _runtime_status() -> SandboxRuntimePackDocument:
    return SandboxRuntimePackDocument(
        schema_version=1,
        management_supported=True,
        target="test-target",
        catalog_version="1",
        source_order=(SandboxRuntimeSource.OSS,),
        components=(),
        next_poll_after_ms=5_000,
    )


def _operation(
    *,
    component_id: str = "python",
    operation_id: str = "operation-1",
) -> SandboxRuntimeOperation:
    return SandboxRuntimeOperation(
        operation_id=operation_id,
        component_id=component_id,
        kind=SandboxRuntimeOperationKind.INSTALL,
        state=SandboxRuntimeOperationState.QUEUED,
        progress_bytes=0,
        total_bytes=100,
        source=None,
        started_at_ms=1,
        updated_at_ms=1,
    )


class FakeSandboxSetupPort:
    def __init__(
        self,
        *,
        status: SandboxSetupStatus | None = None,
        ensured: SandboxSetupStatus | None = None,
        capability: SandboxCapability | None = None,
    ) -> None:
        self.status = status or _setup_result(SandboxSetupState.NOT_SETUP)
        self.ensured = ensured or self.status
        self.capability = capability or _capability(available=False)
        self.refreshes: list[bool] = []
        self.ensure_count = 0

    async def read_setup_status(self) -> SandboxSetupStatus:
        return self.status

    async def ensure_setup(self) -> SandboxSetupStatus:
        self.ensure_count += 1
        self.status = self.ensured
        return self.ensured

    async def read_capability(self, *, refresh: bool) -> SandboxCapability:
        self.refreshes.append(refresh)
        return self.capability


class FakeSandboxPolicyPort:
    def __init__(self, *, policy: SandboxPolicyDocument | None = None) -> None:
        self.policy = policy or _policy()
        self.defaults = SandboxPolicyDefaults(
            builtin_deny_write_paths=(),
            runtime_target=None,
            runtime_versions=(),
        )

    async def read_policy(self) -> SandboxPolicyDocument:
        return self.policy

    async def read_policy_defaults(self) -> SandboxPolicyDefaults:
        return self.defaults

    async def replace_policy(
        self,
        base_policy_version: int,
        policy: SandboxPolicyDocument,
    ) -> SandboxPolicyDocument:
        if base_policy_version != self.policy.policy_version:
            raise SandboxPolicyConflictError(base_policy_version, self.policy)
        self.policy = replace(policy, policy_version=self.policy.policy_version + 1)
        return self.policy


class FakeSandboxRunModePort:
    def __init__(
        self,
        mode: SandboxRunMode | str = SandboxRunMode.FULL,
        *,
        source: str = "default",
    ) -> None:
        self.preference = SandboxRunModePreference(SandboxRunMode.parse(mode), source)
        self.persisted: list[SandboxRunMode] = []

    async def read_run_mode(self) -> SandboxRunModePreference:
        return self.preference

    async def persist_run_mode(self, mode: SandboxRunMode) -> SandboxRunMode:
        self.persisted.append(mode)
        self.preference = SandboxRunModePreference(mode, "preference")
        return mode


class FakeSandboxRunModeEventsPort:
    def __init__(self) -> None:
        self.published: list[SandboxRunModePreference] = []

    async def publish_run_mode_changed(self, preference: SandboxRunModePreference) -> None:
        self.published.append(preference)


class FakeSandboxRuntimePackPort:
    def __init__(
        self,
        *,
        status: SandboxRuntimePackDocument,
        install_operation: SandboxRuntimeOperation | None = None,
        cancel_operation: SandboxRuntimeOperation | None = None,
        remove_operation: SandboxRuntimeOperation | None = None,
        discarded_status: SandboxRuntimePackDocument | None = None,
    ) -> None:
        self.status = status
        self.install_operation = install_operation
        self.cancel_operation = cancel_operation
        self.remove_operation = remove_operation
        self.discarded_status = discarded_status or status

    async def read_runtime_status(self) -> SandboxRuntimePackDocument:
        return self.status

    async def install_runtime(self, component_id: str) -> SandboxRuntimeOperation:
        if self.install_operation is None:
            raise AssertionError(f"No install operation configured for {component_id}")
        return self.install_operation

    async def cancel_runtime(
        self,
        component_id: str,
        operation_id: str,
    ) -> SandboxRuntimeOperation:
        if self.cancel_operation is None:
            raise AssertionError(
                f"No cancel operation configured for {component_id}:{operation_id}"
            )
        return self.cancel_operation

    async def remove_runtime(self, component_id: str) -> SandboxRuntimeOperation:
        if self.remove_operation is None:
            raise AssertionError(f"No remove operation configured for {component_id}")
        return self.remove_operation

    async def discard_runtime_download(
        self,
        component_id: str,
    ) -> SandboxRuntimePackDocument:
        _ = component_id
        return self.discarded_status


class FakeSandboxResumePort:
    def __init__(self, paused_sessions: set[str] | None = None) -> None:
        self.paused_sessions = set(paused_sessions or set())

    async def clear_pause(self, session_key: str) -> bool:
        if session_key not in self.paused_sessions:
            return False
        self.paused_sessions.remove(session_key)
        return True


def _application(
    *,
    setup: FakeSandboxSetupPort | None = None,
    policy: FakeSandboxPolicyPort | None = None,
    run_modes: FakeSandboxRunModePort | None = None,
    events: FakeSandboxRunModeEventsPort | None = None,
    runtime_packs: FakeSandboxRuntimePackPort | None = None,
    resume: FakeSandboxResumePort | None = None,
) -> SandboxRuntime:
    return SandboxRuntime(
        setup=setup
        or FakeSandboxSetupPort(
            status=_setup_result(),
            capability=_capability(),
        ),
        policy=policy or FakeSandboxPolicyPort(),
        run_modes=run_modes or FakeSandboxRunModePort(),
        run_mode_events=events or FakeSandboxRunModeEventsPort(),
        runtime_packs=runtime_packs
        or FakeSandboxRuntimePackPort(status=_runtime_status()),
        resume=resume or FakeSandboxResumePort(),
    )


@pytest.mark.asyncio
async def test_application_exposes_frozen_policy_values() -> None:
    policy = FakeSandboxPolicyPort(policy=_policy(policy_version=4))
    application = _application(policy=policy)

    result = await application.read_policy()

    assert result.policy_version == 4
    assert result.network.deny_domains == ("telemetry.example",)
    with pytest.raises(FrozenInstanceError):
        result.policy_version = 5  # type: ignore[misc]


@pytest.mark.asyncio
async def test_setup_use_cases_return_domain_values_and_forward_refresh() -> None:
    setup = FakeSandboxSetupPort(
        status=_setup_result(SandboxSetupState.NOT_SETUP),
        ensured=_setup_result(),
        capability=_capability(),
    )
    application = _application(setup=setup)

    assert (await application.inspect_setup()).state is SandboxSetupState.NOT_SETUP
    assert (await application.prepare()).state is SandboxSetupState.READY
    assert (await application.inspect_capability(refresh=True)).available is True
    assert setup.ensure_count == 1
    assert setup.refreshes == [True]


@pytest.mark.asyncio
async def test_run_mode_validates_then_persists_then_broadcasts() -> None:
    trace: list[str] = []

    class TracedSetup(FakeSandboxSetupPort):
        async def read_capability(self, *, refresh: bool) -> SandboxCapability:
            trace.append("validate")
            return await super().read_capability(refresh=refresh)

    class TracedRunModes(FakeSandboxRunModePort):
        async def persist_run_mode(self, mode: SandboxRunMode) -> SandboxRunMode:
            trace.append("persist")
            return await super().persist_run_mode(mode)

    class TracedEvents(FakeSandboxRunModeEventsPort):
        async def publish_run_mode_changed(self, preference) -> None:
            trace.append("broadcast")
            await super().publish_run_mode_changed(preference)

    setup = TracedSetup(status=_setup_result(), capability=_capability())
    run_modes = TracedRunModes()
    events = TracedEvents()
    application = _application(setup=setup, run_modes=run_modes, events=events)

    result = await application.select_run_mode("safe")

    assert result == SandboxRunModePreference(SandboxRunMode.SAFE, "preference")
    assert trace == ["validate", "persist", "broadcast"]
    assert run_modes.persisted == [SandboxRunMode.SAFE]
    assert events.published == [result]


@pytest.mark.asyncio
async def test_safe_mode_failure_does_not_persist_or_broadcast() -> None:
    setup = FakeSandboxSetupPort(
        status=_setup_result(SandboxSetupState.NOT_SETUP),
        capability=_capability(available=False),
    )
    run_modes = FakeSandboxRunModePort()
    events = FakeSandboxRunModeEventsPort()
    application = _application(setup=setup, run_modes=run_modes, events=events)

    with pytest.raises(SandboxCapabilityUnavailableError):
        await application.select_run_mode("safe")

    assert run_modes.persisted == []
    assert events.published == []


@pytest.mark.asyncio
async def test_policy_compare_and_swap_returns_frozen_current_policy() -> None:
    policy = FakeSandboxPolicyPort(policy=_policy(policy_version=3))
    application = _application(policy=policy)

    with pytest.raises(SandboxPolicyConflictError) as exc_info:
        await application.replace_policy(
            2,
            _policy(policy_version=2),
        )

    assert exc_info.value.current_policy.policy_version == 3
    with pytest.raises(FrozenInstanceError):
        exc_info.value.expected_version = 4  # type: ignore[misc]


@pytest.mark.asyncio
async def test_runtime_mutation_rejects_changed_operation_identity() -> None:
    runtime_packs = FakeSandboxRuntimePackPort(
        status=_runtime_status(),
        cancel_operation=_operation(component_id="node", operation_id="replacement"),
    )
    application = _application(runtime_packs=runtime_packs)

    with pytest.raises(SandboxRuntimeIdentityError) as exc_info:
        await application.cancel_runtime_pack_install("python", "operation-1")

    assert exc_info.value.expected_component_id == "python"
    assert exc_info.value.actual_component_id == "node"
    assert exc_info.value.expected_operation_id == "operation-1"
    assert exc_info.value.actual_operation_id == "replacement"


@pytest.mark.asyncio
async def test_resume_is_idempotent() -> None:
    resume = FakeSandboxResumePort({"agent:main:webchat:test"})
    application = _application(resume=resume)

    first = await application.resume_paused_session("agent:main:webchat:test")
    second = await application.resume_paused_session("agent:main:webchat:test")

    assert first.resumed is True
    assert second.resumed is False
    assert second.autonomous_paused is False


@pytest.mark.asyncio
async def test_application_rejects_invalid_mode_and_policy_version() -> None:
    application = _application()

    with pytest.raises(ValueError, match="run mode"):
        await application.select_run_mode("unsafe")
    with pytest.raises(ValueError, match="policy version"):
        await application.replace_policy(True, _policy())
