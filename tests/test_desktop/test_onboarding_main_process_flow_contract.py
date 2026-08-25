import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _main_source() -> str:
    return (ROOT / "desktop/electron/src/main.ts").read_text(encoding="utf-8")


def _coordinator_source() -> str:
    return (ROOT / "desktop/electron/src/onboarding-flow-coordinator.ts").read_text(
        encoding="utf-8"
    )


def _section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def test_onboarding_uses_one_flow_scoped_source_of_truth() -> None:
    source = _main_source()
    coordinator = _coordinator_source()
    flow_type = _section(source, "interface OnboardingFlow", "interface DesktopSettingsPayload")
    run = _section(source, "async function runOnboarding", "async function pathExists")

    assert "extends CoordinatedOnboardingFlow" in flow_type
    for field in (
        "window: BrowserWindow | null",
        "resolve: ((credential: DesktopConnection) => void) | null",
        "reject: ((error: Error) => void) | null",
    ):
        assert field in flow_type
    for field in (
        "state: OnboardingFlowState",
        "savePayload: Payload | null",
        "savePromise: Promise<Result> | null",
    ):
        assert field in coordinator

    assert "private activeFlow: Flow | null = null" in coordinator
    assert "const onboardingFlows = new OnboardingFlowCoordinator" in source
    assert "activeOnboardingFlow" not in source
    assert "let resolveOnboarding" not in source
    assert "let rejectOnboarding" not in source
    assert run.index("await onboardingFlows.waitForAbandonedSave()") < run.index(
        "await loadPendingMigrationProviderSetup()"
    )
    assert "const window = new BrowserWindow({" in run
    assert "const flow: OnboardingFlow = {" in run
    assert "onboardingFlows.activate(flow)" in run
    assert "if (onboardingWindow === window) onboardingWindow = null" in run
    assert "if (flow.window === window) flow.window = null" in run


def test_onboarding_save_is_per_flow_single_flight_with_exact_payload_joining() -> None:
    source = _main_source()
    coordinator = _coordinator_source()
    request = coordinator[coordinator.index("  requestSave(") :]
    handler = _section(
        source,
        "ipcMain.handle('desktop:onboarding:save'",
        "ipcMain.handle('desktop:onboarding:cancel'",
    )

    assert "isDeepStrictEqual(flow.savePayload, payload)" in request
    assert "{ kind: 'joined', promise: flow.savePromise }" in request
    assert "{ kind: 'conflict' }" in request
    assert "const operation = Promise.resolve().then(perform)" in request
    assert request.index("flow.savePayload = payload") < request.index(
        "const operation = Promise.resolve().then(perform)"
    )
    assert request.index("const operation = Promise.resolve().then(perform)") < (
        request.index("flow.savePromise = savePromise")
    )
    assert "if (flow.savePromise !== savePromise) return" in request

    trust = "if (!flow || !trustedOnboardingIpc(event, flow))"
    request_save = "const request = onboardingFlows.requestSave("
    assert handler.index(trust) < handler.index(request_save)
    assert "'save_in_progress'" in handler
    assert "if (request.kind === 'conflict')" in handler
    assert "if (request.kind === 'inactive')" in handler
    assert "return await request.promise" in handler


def test_onboarding_save_preserves_recovery_and_writer_ordering() -> None:
    source = _main_source()
    save = _section(
        source,
        "async function performOnboardingSave(",
        "async function withRecoveryOperation",
    )

    telemetry_start = save.index("const telemetry = new OnboardingSaveTelemetry(")
    recovery_stage = save.index("'primary_recovery_inspect'")
    recovery = save.index("() => refreshPrimaryRecoveryAfterImportAttempt()")
    admission = save.index("beginDesktopWriterOperation('complete desktop onboarding')")
    writer_admitted = save.index("telemetry.markWriterAdmitted()")
    marker_stage = save.index("'pending_setup_read'")
    marker = save.index("() => readPendingMigrationProviderSetup()")
    settings_stage = save.index("telemetry.stage('settings_persist'")
    imported = save.index("await saveImportedDesktopCredential(")
    ordinary = save.index("await saveDesktopCredential(payload, true)")
    refresh_keychain = save.index("invalidateSecretStorageBackendCache()")
    settings_persisted = save.index("telemetry.markSettingsPersistedConfirmed()")
    finalize_stage = save.index("telemetry.stage('local_finalize'")
    locale = save.index("applyDesktopLocaleChoice(payload.locale)")
    clear_marker = save.index("await clearPendingMigrationProviderSetup()")
    finish = save.index("finishWriter()")
    handoff_stage = save.index("telemetry.stageSync('flow_handoff'")
    lifecycle_guard = save.index(
        "if (desktopWriters.closed || isQuitting || appExitPhase !== 'running')"
    )
    complete = save.index("if (!completeOnboardingFlow(flow, credential))")
    telemetry_finish = save.index("telemetry.finish()")

    assert telemetry_start < recovery_stage < recovery < admission < writer_admitted
    assert writer_admitted < marker_stage < marker < settings_stage
    assert settings_stage < refresh_keychain < imported < settings_persisted
    assert refresh_keychain < ordinary < settings_persisted
    assert settings_persisted < finalize_stage < locale < clear_marker < finish
    assert finish < handoff_stage < lifecycle_guard < complete < telemetry_finish
    assert "app.isPackaged" in save
    assert "(event, detail) => desktopLog(event, detail)" in save
    assert "return telemetry.recordReturned(" in save
    assert "if (flow.state === 'saving') flow.state = 'editing'" in save
    assert "throw error" in save


def test_onboarding_save_and_cancel_have_closed_flow_and_typed_failure_contracts() -> None:
    source = _main_source()
    error_codes = _section(
        source,
        "type OnboardingSaveErrorCode =",
        "type OnboardingSaveResult =",
    )
    complete = _section(
        source,
        "function completeOnboardingFlow(",
        "async function performOnboardingSave(",
    )
    cancel = _section(
        source,
        "ipcMain.handle('desktop:onboarding:cancel'",
        "// Keep the normal app-quit gateway drain single-flight.",
    )

    assert set(re.findall(r"'([^']+)'", error_codes)) == {
        "onboarding_inactive",
        "recovery_required",
        "save_in_progress",
        "lifecycle_deferred",
    }
    assert "!onboardingFlows.complete(flow)" in complete
    assert "const window = flow.window" in complete
    assert "onboardingWindow?.close()" not in complete

    trusted = "if (!flow || !trustedOnboardingIpc(event, flow))"
    abandon = "abandonOnboardingFlow(flow, new Error('OpenSquilla setup was cancelled.'), true)"
    assert cancel.index(trusted) < cancel.index(abandon) < cancel.index("app.quit()")
