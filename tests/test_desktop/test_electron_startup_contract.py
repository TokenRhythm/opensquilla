import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_desktop_resume_is_visible_first_and_single_flight() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    resume = _section(
        main_ts,
        "async function openOrResumeDesktopApp",
        "function stopGateway",
    )

    assert "let gatewayStartPromise: Promise<GatewayState> | null = null" in main_ts
    assert "startupInProgress" not in main_ts
    assert "function ensureGatewayStarted(): Promise<GatewayState>" in main_ts
    assert "gatewayStartPromise = startGatewayWithPortRecovery().finally" in main_ts
    assert "gatewayStartPromise = null" in main_ts
    assert (
        "function isCurrentWindowAtControlUi(window: BrowserWindow, gatewayUrl: string): boolean"
        in main_ts
    )

    assert resume.index("await createMainWindow()") < resume.index("ensureGatewayStarted()")
    assert "focusMainWindow()" in resume
    assert "reuseHealthyGatewayState(" in resume
    assert "loadControlUiIntoCurrentWindow(" in resume


def test_desktop_gateway_completion_uses_current_live_window() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    load_current = _section(
        main_ts,
        "async function loadControlUiIntoCurrentWindow",
        "async function openOrResumeDesktopApp",
    )

    assert "function currentMainWindow(): BrowserWindow | null" in main_ts
    assert "const window = currentMainWindow()" in load_current
    assert "if (!window) return" in load_current
    assert "if (window.isDestroyed()) return" in load_current
    assert "isCurrentWindowAtControlUi(window, gatewayUrl)" in load_current
    guard_index = load_current.index("isCurrentWindowAtControlUi(window, gatewayUrl)")
    load_index = load_current.index("await loadControlUi(window, gatewayUrl)")
    assert guard_index < load_index
    assert "current.pathname === '/control'" in main_ts
    assert "current.pathname.startsWith('/control/')" in main_ts
    assert "if (mainWindow === window) mainWindow = null" in main_ts


def test_desktop_opens_directly_on_the_new_task_route() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    load_control = _section(
        main_ts,
        "async function loadControlUi(",
        "function isAllowedMainWindowNavigation",
    )

    assert "const url = `${gatewayUrl}/control/chat/new`" in load_control
    assert "const url = `${gatewayUrl}/control/chat`" not in load_control


def test_desktop_owned_gateway_is_unconditionally_loopback_bound() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start_gateway = _section(
        main_ts,
        "async function startGateway(): Promise<GatewayState>",
        "async function startGatewayWithPortRecovery",
    )

    # ``gateway run`` treats the default-looking ``--bind 127.0.0.1`` as
    # unspecified so CLI users can inherit the TOML host.  Desktop must use
    # the higher-precedence ``--listen`` flag; otherwise a legacy
    # ``host = \"0.0.0.0\"`` silently makes the desktop-owned Gateway public.
    assert "'--listen', '127.0.0.1'" in start_gateway
    assert "'--bind', '127.0.0.1'" not in start_gateway
    assert "OPENSQUILLA_GATEWAY_HOST" not in start_gateway
    assert "OPENSQUILLA_LISTEN" not in start_gateway
    assert "'0.0.0.0'" not in start_gateway


def test_desktop_artifact_bridge_credentials_reach_only_the_owned_gateway_child() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")
    child_environment = _section(
        main_ts,
        "function desktopChildEnvironment",
        "// ── Legacy home import detection",
    )
    start_gateway = _section(
        main_ts,
        "async function startGateway(): Promise<GatewayState>",
        "async function startGatewayWithPortRecovery",
    )

    assert "delete environment[DESKTOP_ARTIFACT_BRIDGE_URL_ENV]" in child_environment
    assert "delete environment[DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV]" in child_environment
    assert "await desktopArtifactBridgeLoopback.start()" in start_gateway
    assert "...artifactBridgeEnvironment" in start_gateway
    assert start_gateway.index("await desktopArtifactBridgeLoopback.start()") < start_gateway.index(
        "const port = await findGatewayPort()"
    )
    assert "OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_URL" not in preload
    assert "OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN" not in preload


def test_desktop_activation_and_second_instance_share_safe_reveal_helper() -> None:
    main_ts = _read("desktop/electron/src/main.ts")

    assert "if (process.platform !== 'darwin') app.quit()" in main_ts
    assert "app.on('activate', () => {\n  revealDesktopApp()" in main_ts
    assert "function revealDesktopApp(): void" in main_ts
    activation = _section(
        main_ts,
        "async function activateMainWindow(",
        "function revealDesktopApp(): void",
    )
    reveal = _section(
        main_ts,
        "function revealDesktopApp(): void",
        "async function promptForMainWindowClose",
    )
    assert "if (!canRevealDesktopApp(appExitPhase))" in activation
    assert "focusMainWindow()" in activation
    assert "await openOrResumeDesktopApp()" in activation
    assert "app.focus({ steal: true })" in activation
    assert "activateMainWindow('desktop-reveal')" in reveal
    # second-instance reveals the app via the shared helper (a diagnostic log
    # line precedes the resume call — see the #446 relaunch-retry contract).
    second_instance = _section(
        main_ts,
        "app.on('second-instance',",
        "void app.whenReady().then",
    )
    assert "revealDesktopApp()" in second_instance
    assert "void app.whenReady().then" in main_ts
    assert "void openOrResumeDesktopApp()" in _section(
        main_ts,
        "void app.whenReady().then",
        "})\n}",
    )


def test_desktop_deep_link_protocol_is_registered_and_safely_activated() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    package = json.loads(_read("desktop/electron/package.json"))

    assert package["build"]["protocols"] == [
        {
            "name": "OpenSquilla",
            "schemes": ["opensquilla"],
        }
    ]
    assert (
        "from './desktop-deep-link.js'"
        in main_ts
    )
    assert "async function activateMainWindow(" in main_ts
    assert "function handleDeepLink(rawUrl: unknown" in main_ts
    assert "parseDesktopDeepLink(rawUrl)" in main_ts
    assert "desktopDeepLinkArguments(commandLine)" in main_ts
    assert "app.setAsDefaultProtocolClient(DESKTOP_DEEP_LINK_SCHEME)" in main_ts

    open_url = _section(
        main_ts,
        "app.on('open-url'",
        "desktopLog('launch',",
    )
    assert "event.preventDefault()" in open_url
    assert "handleDeepLink(rawUrl, 'open-url')" in open_url
    assert main_ts.index("app.on('open-url'") < main_ts.index("void app.whenReady().then")

    second_instance = _section(
        main_ts,
        "app.on('second-instance'",
        "void app.whenReady().then",
    )
    assert "commandLine" in second_instance
    assert "handleDeepLinksFromCommandLine(commandLine, 'second-instance')" in (
        second_instance
    )

    initial_argv = _section(
        main_ts,
        "if (process.platform === 'win32') {\n    handleDeepLinksFromCommandLine",
        "app.on('second-instance'",
    )
    assert "process.argv" in initial_argv
    assert "'initial-argv'" in initial_argv
    assert "pendingDesktopDeepLinkOpen" in main_ts
    assert "desktopDeepLinkActivationReady" in main_ts


def test_desktop_window_close_has_a_visible_background_recovery_surface() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")
    lifecycle = _read("desktop/electron/src/desktop-window-lifecycle.ts")
    package = json.loads(_read("desktop/electron/package.json"))
    ci = _read(".github/workflows/ci.yml")

    close_handler = _section(
        main_ts,
        "function handleMainWindowClose",
        "function installEditingContextMenu",
    )
    tray = _section(main_ts, "function createWindowsTray", "function hideMainWindow")
    hide = _section(main_ts, "function hideMainWindow", "function revealDesktopApp")
    ready = _section(main_ts, "void app.whenReady().then", "})\n}")

    assert "window.on('close', (event) => handleMainWindowClose(window, event))" in main_ts
    session_end = _section(
        main_ts,
        "window.on('session-end', () => {",
        "window.once('ready-to-show'",
    )
    query_session_end = _section(
        main_ts,
        "window.on('query-session-end', () => {",
        "window.on('session-end', () => {",
    )
    assert "windowsSessionEndPreviousPhase = appExitPhase" in main_ts
    assert "windowsSessionEndResetTimer = setTimeout" in query_session_end
    assert "systemSessionEnding = false" in query_session_end
    assert "setAppExitPhase(previousPhase" in query_session_end
    assert "windowsSessionEndResetTimer.unref()" in query_session_end
    assert "if (isQuitting" not in query_session_end
    assert "clearTimeout(windowsSessionEndResetTimer)" in session_end
    assert "isQuitting = true" in session_end
    assert "destroyWindowsTray()" in session_end
    assert "stopGateway()" in session_end
    assert "mainWindowCloseAction({" in close_handler
    assert "windowsTrayReady: windowsTray !== null" in close_handler
    assert "event.preventDefault()" in close_handler
    assert "hideMainWindow(window)" in close_handler
    assert "app.quit()" in close_handler

    assert "const tray = new Tray(appIconPath())" in tray
    assert "tray.on('click', () => revealDesktopApp())" in tray
    assert "label: desktopT('tray.quit')" in main_ts
    assert "click: () => app.quit()" in main_ts
    assert ready.index("createWindowsTray()") < ready.index("openOrResumeDesktopApp()")

    assert "window.webContents.send('desktop:window:hidden')" in hide
    assert hide.index("desktop:window:hidden") < hide.index("window.hide()")
    assert "onWindowHidden: (callback: () => void)" in preload
    assert "ipcRenderer.on('desktop:window:hidden', listener)" in preload

    assert "platform === 'darwin' || platform === 'win32' ? 'background' : 'quit'" in lifecycle
    assert "platform === 'win32' && context.windowsTrayReady" in lifecycle
    assert "if (!backgroundSupported) return 'quit'" in lifecycle
    assert package["scripts"]["test:window-lifecycle"].endswith(
        "scripts/test-desktop-window-lifecycle.mjs"
    )
    assert "node scripts/test-desktop-window-lifecycle.mjs" in ci


def test_desktop_explicit_exit_cannot_be_converted_back_to_window_hiding() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    lifecycle = _read("desktop/electron/src/desktop-window-lifecycle.ts")
    before_quit = _section(main_ts, "app.on('before-quit'", "function shutdownFromSignal")

    assert "context.systemSessionEnding || context.exitPhase === 'committed'" in lifecycle
    assert "if (context.exitPhase !== 'running') return 'hide'" in lifecycle
    assert "return phase === 'running'" in lifecycle
    assert "setAppExitPhase('deferred'" in before_quit
    assert "setAppExitPhase('draining'" in before_quit
    assert "setAppExitPhase('committed'" in before_quit
    assert "setAppExitPhase('running', 'Gateway quit drain failed safely')" in before_quit
    assert "if (systemSessionEnding)" in before_quit
    system_exit = _section(before_quit, "if (systemSessionEnding)", "// An updater drain")
    assert "event.preventDefault()" not in system_exit
    assert "destroyWindowsTray()" in system_exit
    assert "stopGateway()" in system_exit


def test_desktop_retry_waits_for_all_owned_gateways_and_fails_closed() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    retry = _section(
        main_ts,
        "ipcMain.handle('desktop:boot:retry'",
        "ipcMain.handle('desktop:boot:quit'",
    )

    # The Control UI "Restart runtime" action forces a real restart: an in-flight
    # start is joined, otherwise every lifecycle-owned gateway is torn down and
    # awaited before respawn rather than reused. The boot page has a separate
    # non-destructive resume path below.
    assert "if (gatewayStartPromise)" in retry
    join_call = "const exited = await stopAndJoinAllLifecycleOwnedGateways()"
    assert join_call in retry
    assert "if (!exited)" in retry
    assert "stopGateway()" not in retry
    assert "waitForGatewayProcessExit(" not in retry

    failure = _section(retry, "if (!exited)", "clearReusableGatewayState()")
    assert "gatewayState.status = 'error'" in failure
    assert "gatewayState.error = message" in failure
    assert "desktopLog('gateway_restart_wait_timeout'" in failure
    assert "sendBootError(message)" in failure
    assert "await restoreMainWindowToBootPage()" in failure
    assert "return { ok: false, error: message }" in failure
    assert "clearReusableGatewayState()" not in failure
    assert "openOrResumeDesktopApp()" not in failure

    join_index = retry.index(join_call)
    failure_index = retry.index("if (!exited)", join_index)
    clear_index = retry.index("clearReusableGatewayState()", failure_index)
    open_index = retry.index("openOrResumeDesktopApp()", clear_index)
    assert join_index < failure_index < clear_index < open_index

    success = retry[clear_index:]
    assert success.count("clearReusableGatewayState()") == 1
    assert success.count("openOrResumeDesktopApp()") == 1
    assert success.index("clearReusableGatewayState()") < success.index(
        "openOrResumeDesktopApp()"
    )
    assert success.index("openOrResumeDesktopApp()") < success.index("return { ok: true }")


def test_desktop_boot_resume_waits_for_the_existing_owned_gateway() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")
    resume_flow = _section(
        main_ts,
        "async function resumeBootStartup()",
        "ipcMain.handle('desktop:boot:resume'",
    )
    resume_handler = _section(
        main_ts,
        "ipcMain.handle('desktop:boot:resume'",
        "ipcMain.handle('desktop:boot:retry'",
    )
    start = _section(
        main_ts,
        "async function startGateway(): Promise<GatewayState>",
        "async function startGatewayWithPortRecovery()",
    )
    resume_owned = _section(
        main_ts,
        "async function resumeOwnedGatewayStartup",
        "const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS",
    )

    assert "resumeStartup: () => ipcRenderer.invoke('desktop:boot:resume')" in preload
    assert "const pendingStart = gatewayStartPromise" in resume_flow
    assert "await resumeOwnedGatewayStartup(" in resume_flow
    assert "if (!gateway)" in resume_flow
    assert "void openOrResumeDesktopApp()" in resume_flow
    assert "await loadControlUiIntoCurrentWindow(" in resume_flow
    assert "bootResumeAuthorityIsCurrent(authority)" in resume_flow
    assert "if (pendingStart || !initialAuthority" in resume_flow
    assert "stopAndJoinAllLifecycleOwnedGateways" not in resume_flow
    assert "clearReusableGatewayState" not in resume_flow
    assert "stopGateway()" not in resume_flow
    assert "if (bootResumePromise) return await bootResumePromise" in resume_handler
    assert "bootResumePromise = promise" in resume_handler
    assert "bootResumePromise = null" in resume_handler

    assert "await resumeOwnedGatewayStartup(isCurrent)" in start
    assert start.index("await resumeOwnedGatewayStartup(isCurrent)") < start.index(
        "if (gatewayProcess && gatewayState.owned)"
    )
    assert "gatewayProcess === child" in resume_owned
    assert "gatewayProfileKey !== desktopProfileKey()" in resume_owned
    assert "await waitForGateway(url, childExitMessage)" in resume_owned
    assert "await waitForControlUi(url, childExitMessage)" in resume_owned
    assert "await verifyOwnedGatewayLaunch(child)" in resume_owned
    assert "|| !isCurrent()" in resume_owned
    assert "gatewayState.status = 'ready'" in resume_owned


def test_desktop_gateway_ready_requires_exact_launch_identity() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway(): Promise<GatewayState>",
        "async function startGatewayWithPortRecovery()",
    )
    verifier = _section(
        main_ts,
        "async function verifyOwnedGatewayLaunch",
        "async function resumeOwnedGatewayStartup",
    )

    assert "gatewayProcessOwnershipContexts.get(child)" in verifier
    assert "verifyDesktopGatewayLaunchOwnership" in verifier
    assert "await verifyOwnedGatewayLaunch(child)" in start
    assert "requestGatewayShutdown" not in start
    assert "unverified listener" in start

    discard = _section(
        main_ts,
        "async function discardUnverifiedOwnedGatewayChild",
        "async function resumeOwnedGatewayStartup",
    )
    assert "gatewayProcess = null" in discard
    assert "hardTerminateGatewayProcess(child)" in discard
    assert "requestGatewayShutdown" not in discard

    shutdown = _section(
        main_ts,
        "async function requestOwnedGatewayShutdown",
        "async function downloadDiagnostics",
    )
    assert "if (context) return false" in shutdown
    assert shutdown.index("if (context) return false") < shutdown.index(
        "return await requestGatewayShutdown(url)"
    )


def test_desktop_boot_resume_fences_stale_async_state_and_ready_events() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    authority = _section(
        main_ts,
        "function bootResumeAuthorityIsCurrent",
        "async function resumeBootStartup",
    )
    resume_flow = _section(
        main_ts,
        "async function resumeBootStartup",
        "ipcMain.handle('desktop:boot:resume'",
    )
    control_load = _section(
        main_ts,
        "async function loadControlUiIntoCurrentWindow",
        "async function restoreMainWindowToBootPage",
    )

    for fence in [
        "!isQuitting",
        "gatewayProcess === authority.child",
        "gatewayProfileKey === authority.profileKey",
        "desktopOpenFlowRevision === authority.openFlowRevision",
    ]:
        assert fence in authority
    assert "if (initialAuthority && !bootResumeAuthorityIsCurrent" in resume_flow
    assert "if (!authority || !bootResumeAuthorityIsCurrent" in resume_flow
    assert "!bootResumeAuthorityIsCurrent(initialAuthority)" in resume_flow

    first_fence = control_load.index("if (!isCurrent()) return false")
    load = control_load.index("await loadControlUi(window, gatewayUrl)")
    final_fence = control_load.index("if (!isCurrent()) return false", load)
    ready = control_load.rindex("sendBootStatus('ready')")
    assert first_fence < load < final_fence < ready

    resume_owned = _section(
        main_ts,
        "async function resumeOwnedGatewayStartup",
        "const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS",
    )
    assert resume_owned.rindex("|| !isCurrent()") < resume_owned.index(
        "gatewayState.status = 'ready'"
    )

    open_flow = _section(
        main_ts,
        "async function openOrResumeDesktopApp",
        "const GATEWAY_SHUTDOWN_KILL_AFTER_MS",
    )
    load_index = open_flow.index("await loadControlUiIntoCurrentWindow(")
    load_call = open_flow[load_index : load_index + 300]
    assert "operationIsCurrent" in load_call

    authority_capture = open_flow[
        open_flow.index("const gatewayUrl = gateway.url") : load_index
    ]
    for exact_gateway_fence in [
        "const expectedOwned = gateway.owned",
        "const expectedChild = expectedOwned ? gatewayProcess : null",
        "gatewayState.url === gatewayUrl",
        "gatewayState.owned === expectedOwned",
        "gatewayProcess === expectedChild",
        "!hasGatewayProcessExited(expectedChild)",
    ]:
        assert exact_gateway_fence in authority_capture
    assert "const authoritative = operationIsCurrent()" in open_flow


def test_desktop_open_flow_rejects_stale_start_errors() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    open_flow = _section(
        main_ts,
        "async function openOrResumeDesktopApp",
        "const GATEWAY_SHUTDOWN_KILL_AFTER_MS",
    )
    catch = open_flow.index("} catch (error) {")
    authority = open_flow.index("const authoritative = operationIsCurrent()", catch)
    status = open_flow.index("gatewayState.status = 'error'", authority)
    state_error = open_flow.index("gatewayState.error =", status)
    send_error = open_flow.index("sendBootError(error)", state_error)
    loop_fence = open_flow.index("desktopOpenAuthorityIsCurrent", send_error)

    assert catch < authority < status < state_error < send_error < loop_fence
    assert "operationIsCurrent()" in open_flow[authority:status]


def test_desktop_shared_spawn_gate_blocks_still_stopping_gateways() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway()",
        "async function startGatewayWithPortRecovery()",
    )

    # activate and second-instance both reach this shared boundary. A child that
    # Retry already moved into the stopping set must therefore block every entry
    # point, not only the original retry handler.
    stopping_check = "const alreadyStoppingGateways = liveLifecycleOwnedGatewayProcesses()"
    assert stopping_check in start
    assert "if (alreadyStoppingGateways.length > 0)" in start
    assert "throw new Error(desktopGatewayStillRunningMessage())" in start

    final_gate = _section(
        start,
        "const liveOwnedGatewayCount = liveLifecycleOwnedGatewayProcesses().length",
        "const url = `http://127.0.0.1:${port}`",
    )
    assert "lifecycleAllowsProcessSpawn(" in final_gate
    assert "liveOwnedGatewayCount," in final_gate
    assert "if (liveOwnedGatewayCount > 0)" in final_gate
    assert "throw new Error(desktopGatewayStillRunningMessage())" in final_gate


def test_boot_retry_surfaces_failed_restart_and_prevents_repeat_clicks() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    apply_error = _section(
        boot_html,
        "function applyError(payload)",
        "function renderRecoveryState",
    )
    retry_flow = _section(
        boot_html,
        "async function retryStartup()",
        "function updateTimer()",
    )

    assert "retryButton.disabled = true" in retry_flow
    assert "recoveryRetryButton.disabled = true" in retry_flow
    assert "typeof api?.resumeStartup === 'function'" in retry_flow
    assert "api.resumeStartup" in retry_flow
    assert "api?.retryStartup" in retry_flow
    assert "const result = await resumeStartup()" in retry_flow
    assert "result && result.ok === false" in retry_flow
    assert "result.error || msg.errorDefault" in retry_flow
    assert "applyError({ message: result.error || msg.errorDefault })" in retry_flow
    assert "errorPanel.classList.add('visible')" in apply_error
    assert "retryButton.disabled = false" in retry_flow
    assert "recoveryRetryButton.disabled = false" in retry_flow
    assert retry_flow.index("retryButton.disabled = true") < retry_flow.index(
        "await resumeStartup()"
    )
    assert retry_flow.index("await resumeStartup()") < retry_flow.index(
        "retryButton.disabled = false"
    )
    assert (
        "document.getElementById('retry').addEventListener('click', () => retryStartup())"
        in boot_html
    )
    assert (
        "document.getElementById('recoveryRetry').addEventListener"
        "('click', () => retryStartup())"
        in boot_html
    )
    assert "rawMessage.includes('OPENSQUILLA_PROFILE_IN_USE')" in apply_error
    assert boot_html.count("profileInUse:") == 6


def test_boot_error_and_recovery_states_freeze_determinate_progress() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    paused_styles = _section(
        boot_html,
        "body.errored .status-line::before",
        ".status-copy",
    )
    apply_error = _section(
        boot_html,
        "function applyError(payload)",
        "function renderRecoveryState",
    )
    render_recovery = _section(
        boot_html,
        "function renderRecoveryState(state, moveFocus = true)",
        "async function runRecoveryAction",
    )

    assert "opacity: 0.45" in paused_styles
    assert "@keyframes progress" not in boot_html
    assert "animation: progress" not in boot_html
    assert "body.errored .loader::before" in paused_styles
    assert "body.errored .loader span" in paused_styles
    assert "animation-play-state: paused" in paused_styles
    assert "document.body.classList.add('errored')" in apply_error
    assert "document.body.classList.add('recovering', 'errored')" in render_recovery
    assert "resetBootProgressOnNextStatus = true" in apply_error
    assert "resetBootProgressOnNextStatus = true" in render_recovery


def test_boot_timer_tracks_each_status_identity_without_spamming_live_regions() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    apply_status = _section(
        boot_html,
        "function applyStatus(payload)",
        "function applyError(payload)",
    )
    timer = _section(
        boot_html,
        "function updateTimer()",
        "const api = window.opensquillaDesktop",
    )

    # The phase is announced once, while the visual 100 ms timer stays out of
    # the live region so assistive technology is not flooded with elapsed time.
    assert '<section class="status">' in boot_html
    assert (
        'id="phase" data-i18n="phaseDefault" role="status" '
        'aria-live="polite" aria-atomic="true"'
        in boot_html
    )
    assert 'id="timer" aria-hidden="true"' in boot_html

    # Main-process BootStatus.at is the phase anchor. Replaying the same
    # (phaseId, at) snapshot must not reset it, while a new status identity does.
    assert "let phaseStartedAt = performance.now()" in boot_html
    assert "let phaseStatusIdentity = ''" in boot_html
    assert "const statusAt = payload && payload.at ? String(payload.at) : ''" in apply_status
    assert "const statusIdentity = phaseId + '\\u0000' + statusAt" in apply_status
    assert "if (statusIdentity !== phaseStatusIdentity)" in apply_status
    assert "phaseStatusIdentity = statusIdentity" in apply_status
    assert "const statusAtMs = Date.parse(statusAt)" in apply_status
    assert (
        "const wallAgeMs = Number.isFinite(statusAtMs) ? Date.now() - statusAtMs : 0"
        in apply_status
    )
    assert "phaseStartedAt = performance.now() - Math.max(0, wallAgeMs)" in apply_status
    assert "updateTimer()" in apply_status
    assert "performance.now() - phaseStartedAt" in timer


def test_boot_progress_reports_only_completed_startup_milestones() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    main_ts = _read("desktop/electron/src/main.ts")
    apply_status = _section(
        boot_html,
        "function applyStatus(payload)",
        "function applyError(payload)",
    )
    render_progress = _section(
        boot_html,
        "function renderBootProgress()",
        "function applyStatus(payload)",
    )
    start_gateway = _section(
        main_ts,
        "async function startGateway(): Promise<GatewayState>",
        "async function startGatewayWithPortRecovery",
    )

    assert 'id="startupProgress"' in boot_html
    assert 'role="progressbar"' in boot_html
    assert 'aria-labelledby="phase"' in boot_html
    assert 'aria-valuemin="0"' in boot_html
    assert 'aria-valuemax="4"' in boot_html
    assert 'aria-valuenow="0"' in boot_html
    assert 'id="progressCount">0/4<' in boot_html

    for phase, value in {
        "profile": 0,
        "'gateway-start'": 1,
        "'gateway-health'": 2,
        "control": 3,
        "ready": 4,
    }.items():
        assert f"{phase}: {value}" in boot_html

    assert "Math.max(completedBootMilestones, reportedProgress)" in apply_status
    assert "if (Number.isInteger(reportedProgress))" in apply_status
    assert "if (resetBootProgressOnNextStatus)" in apply_status
    assert "resetBootProgressOnNextStatus = false" in apply_status
    assert "const activeStep = stepMap[phaseId]" in apply_status
    assert "if (activeStep) setStepState" in apply_status
    assert "stepMap[phaseId] || 'profile'" not in apply_status
    assert "--boot-progress" in render_progress
    assert "aria-valuenow" in render_progress
    assert "progressCount.textContent" in render_progress

    assert (
        "sendBootStatus('profile')\n"
        "  await recoverVerifiedOrphanGatewayBeforeSpawn()"
        in start_gateway
    )
    assert (
        "sendBootStatus('gateway-health')\n"
        "  await recoverVerifiedOrphanGatewayBeforeSpawn()"
        not in start_gateway
    )


def test_boot_and_native_window_backgrounds_match_control_ui_theme_tokens() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    main_ts = _read("desktop/electron/src/main.ts")
    light_tokens = _read("opensquilla-webui/src/themes/light/tokens.css")
    dark_tokens = _read("opensquilla-webui/src/themes/dark/tokens.css")

    assert "--bg: #F7F7F8;" in light_tokens
    assert "--bg: #18181A;" in dark_tokens
    assert "--bg: #F7F7F8;" in boot_html
    assert "--bg: #18181A;" in boot_html
    assert "const DESKTOP_LIGHT_BACKGROUND_COLOR = '#F7F7F8'" in main_ts
    assert "const DESKTOP_DARK_BACKGROUND_COLOR = '#18181A'" in main_ts
    assert main_ts.count("backgroundColor: desktopWindowBackgroundColor()") == 1
    assert "const backgroundColor = desktopWindowBackgroundColor()" in main_ts
    assert "#08080A" not in main_ts
    assert "#F7F6F3" not in main_ts


def test_boot_error_panel_exposes_reset_setup_recovery() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    reset_flow = _section(
        boot_html,
        "async function resetSetup()",
        "setInterval",
    )

    assert 'id="resetSetup"' in boot_html
    assert "Reset setup" in boot_html
    assert 'data-i18n="resetSetup"' in boot_html
    assert "function resetSetup()" in boot_html
    assert "api.resetDesktopSettings" in boot_html
    assert "window.confirm(" in boot_html
    assert "msg.resetConfirm" in boot_html
    assert "msg.resetPhase" in boot_html
    assert "msg.resetProgress" in boot_html
    assert "msg.resetFailed" in boot_html
    assert "workspace path, identity, memory, and chat history are kept" in boot_html
    assert "await api.resetDesktopSettings()" in reset_flow
    assert "await retryStartup()" in reset_flow
    assert reset_flow.index("await api.resetDesktopSettings()") < reset_flow.index(
        "await retryStartup()"
    )
    assert "errorPanel.classList.add('visible')" in reset_flow


def test_primary_repair_ui_is_accessible_without_profile_choices() -> None:
    boot_html = _read("desktop/electron/src/boot.html")

    assert '<section class="recovery" id="recoveryPanel" role="region"' in boot_html
    assert 'aria-labelledby="recoveryTitle"' in boot_html
    assert 'id="recoveryTitle" tabindex="-1"' in boot_html
    assert 'id="recoveryStatus" role="status" aria-live="polite"' in boot_html
    assert 'id="recoveryRetry" class="primary"' in boot_html
    assert 'id="recoveryRetry" class="primary" type="button" data-i18n="retry"' in boot_html
    assert '<label for="workspaceCandidates"' in boot_html
    for button_id in (
        "chooseWorkspace",
        "browseWorkspace",
        "recoverTransaction",
        "revealProfile",
        "revealBackups",
        "copyDiagnostics",
        "recoveryQuit",
    ):
        assert f'id="{button_id}"' in boot_html
        assert 'type="button"' in _section(boot_html, f'id="{button_id}"', ">")
        assert f"getElementById('{button_id}').addEventListener" in boot_html

    assert "function renderRecoveryState(state, moveFocus = true)" in boot_html
    assert "function runRecoveryAction" in boot_html
    for bridge_name in (
        "onRecoveryState",
        "chooseRecoveryWorkspace",
        "recoverProfileTransaction",
        "revealRecoveryPath",
        "copyRecoveryDiagnostics",
        "openLatestDownloadPage",
    ):
        assert bridge_name in boot_html
    assert "abandonPartialCleanup" not in boot_html
    # Interrupted cleanups are abandoned automatically during startup (the
    # journal is archived, nothing further is deleted), so the boot page no
    # longer carries a manual cleanup surface.
    assert "abandonCleanup" not in boot_html
    for removed_name in (
        "recoveryProfiles",
        "copyCredential",
        "continueRecovery",
        "createRecovery",
        "retryPrimary",
        "returnPrimary",
        "launchSafeProfile",
        "retryPrimaryProfile",
        "returnPrimaryProfile",
    ):
        assert removed_name not in boot_html


def test_primary_repair_ui_scaffold_has_all_six_locales() -> None:
    boot_html = _read("desktop/electron/src/boot.html")
    locale_keys = (
        "recoveryTitle",
        "recoveryTitleLockBusy",
        "recoveryTitleUpdate",
        "recoveryIntro",
        "recoveryIntroUpdate",
        "openDownloadPage",
        "workspaceLabel",
        "chooseWorkspace",
        "browseWorkspace",
        "recoverTransaction",
        "revealProfile",
        "revealBackups",
        "copyDiagnostics",
        "diagnosticsCopied",
        "recoveryWorking",
        "noWorkspaceCandidates",
    )
    for key in locale_keys:
        assert boot_html.count(f"{key}:") == 6, key
    for removed_key in (
        "recoveryConfirmationTitle",
        "recoveryConfirmationIntro",
        "cleanupRecoveryTitle",
        "cleanupRecoveryIntro",
        "abandonCleanup",
        "abandonCleanupHelp",
        "recoveryProfileUnsafeTitle",
        "recoveryProfileUnsafeIntro",
        "existingRecoveryLabel",
        "continueRecovery",
        "noRecoveryProfiles",
        "newRecoveryLabel",
        "copyCredential",
        "createRecovery",
        "retryPrimary",
        "returnPrimary",
    ):
        assert f"{removed_key}:" not in boot_html


def test_primary_repair_ui_gives_actionable_copy_for_user_resolvable_blockers() -> None:
    """The two blockers a user can act on directly drop the generic framing.

    A profile held by another OpenSquilla process resolves by letting that
    process finish (or quitting it); a config authored by a newer build
    resolves by updating the app, so that state alone surfaces a download
    entry pointing at the canonical releases page.
    """

    boot_html = _read("desktop/electron/src/boot.html")
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")
    render_recovery = _section(
        boot_html,
        "function renderRecoveryState(state, moveFocus = true)",
        "async function runRecoveryAction",
    )

    assert "const needsAppUpdate = stableCode === 'config_schema_too_new'" in render_recovery
    assert "recoveryTitle.textContent = msg.recoveryTitleUpdate" in render_recovery
    assert "recoveryIntro.textContent = msg.recoveryIntroUpdate" in render_recovery
    assert "stableCode === 'profile_lock_busy'" in render_recovery
    assert "recoveryTitle.textContent = msg.recoveryTitleLockBusy" in render_recovery
    assert "recoveryIntro.textContent = msg.profileInUse" in render_recovery
    assert "document.getElementById('updateGroup').hidden = !needsAppUpdate" in render_recovery

    assert 'id="updateGroup"' in boot_html
    assert 'id="recoveryUpdate"' in boot_html
    assert "api.openLatestDownloadPage()" in boot_html

    assert "ipcRenderer.invoke('desktop:recovery:open-download')" in preload
    assert "ipcMain.handle('desktop:recovery:open-download'" in main_ts
    open_download = _section(
        main_ts,
        "ipcMain.handle('desktop:recovery:open-download'",
        "ipcMain.handle('desktop:boot:state'",
    )
    assert "trustedRecoveryIpc(event)" in open_download
    assert (
        "`https://github.com/${GITHUB_UPDATE_OWNER}/${GITHUB_UPDATE_REPO}/releases/latest`"
        in open_download
    )


def test_mutating_recovery_commands_wait_briefly_for_a_busy_profile_writer() -> None:
    """Startup passes a bounded --lock-timeout so a transient writer (an
    exiting gateway, a finishing cron tick) resolves on its own instead of
    stranding the user on the manual recovery page."""

    main_ts = _read("desktop/electron/src/main.ts")

    assert "const RECOVERY_LOCK_TIMEOUT_SECONDS = 5" in main_ts
    assert main_ts.count("'--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS)") == 5


def test_desktop_runtime_is_primary_only_with_safe_legacy_enumeration() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")
    context = _read("desktop/electron/src/desktop-profile-context.ts")

    assert "export function primaryProfilePaths" in context
    assert "export function allProfileContexts" in context
    assert "Safely enumerate legacy recovery profiles for one-time consolidation" in context
    assert "lstatSync(recoveryRoot)" in context
    assert "rootInfo.isSymbolicLink()" in context
    assert "realpathSync(profile.home)" in context
    for removed_context_api in (
        "contextForProfile",
        "desktopProfileContextPath",
        "loadDesktopProfileContext",
        "persistDesktopProfileContextFile",
        "serializeDesktopProfileContext",
        "updateDesktopProfileContextFile",
        "profileKindEnvironment",
    ):
        assert removed_context_api not in context

    assert "./desktop-profile-context.js" in main_ts
    assert "return primaryProfilePaths(app.getPath('userData'))" in main_ts
    assert "selectDesktopProfile" not in main_ts
    assert "activeRecoveryProfileConfirmedThisProcess" not in main_ts
    assert "createRecoveryProfile" not in main_ts
    assert "launchRecoveryProfile" not in main_ts
    assert "retryOrReturnPrimaryProfile" not in main_ts
    assert "desktop:recovery" in main_ts
    assert "desktop:recovery" in preload
    assert "onRecoveryState" in preload
    # Interrupted cleanups are auto-abandoned in the startup chain; there is
    # no renderer-reachable manual abandon surface anymore.
    assert "desktop:recovery:abandon-cleanup" not in main_ts
    assert "abandonCleanupTransaction" not in preload
    assert "abandonPartialCleanup" not in preload
    assert "launchSafeProfile" not in preload
    assert "retryPrimaryProfile" not in preload
    assert "returnPrimaryProfile" not in preload
    assert "getDesktopProfileKind" not in preload


def test_legacy_profiles_are_consolidated_before_primary_inspection_and_gateway_start() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    parser = _section(
        main_ts,
        "function parseDesktopProfileConsolidationProtocol",
        "async function runDesktopProfileConsolidationCli",
    )
    runner = _section(
        main_ts,
        "async function runDesktopProfileConsolidationCli",
        "function recoveryFailureResult",
    )
    credential_acknowledgement = _section(
        main_ts,
        "async function acknowledgeConsolidatedDesktopCredential",
        "function recoveryFailureResult",
    )
    credential_adoption = _section(
        main_ts,
        "async function adoptConsolidatedDesktopCredential",
        "let desktopProfilesConsolidatedThisProcess",
    )
    credential_reader = _section(
        main_ts,
        "async function readVerifiedConsolidatedCredential",
        "async function adoptConsolidatedDesktopCredential",
    )
    consolidation = _section(
        main_ts,
        "async function consolidateLegacyRecoveryProfilesBeforeStartup",
        "function recoveryStateSnapshot",
    )
    startup_inspection = _section(
        main_ts,
        "async function inspectActiveProfileBeforeStartup",
        "async function openOrResumeDesktopApp",
    )

    # A recovery may contribute config.toml without containing a Desktop
    # credential. Its source id remains receipt metadata, while a non-null
    # credential path still requires that source id and full path validation.
    assert "(sourceCredentialPath !== null && sourceRecoveryId === null)" in parser
    assert "(sourceRecoveryId === null) !== (sourceCredentialPath === null)" not in parser
    assert "'pending'" in main_ts
    assert "'complete'" in main_ts
    assert "'not_required'" in main_ts
    assert "credentialAdoptionStatus === 'pending'" in parser
    assert "credentialAdoptionStatus === 'not_required'" in parser
    assert "if (consolidation.credential_adoption_status !== 'pending') return" in (
        credential_adoption
    )
    assert "sourceRecoveryId === null" in credential_adoption
    assert "sourceCredentialPath === null" in credential_adoption
    assert "consolidation.backup_path === null" in credential_adoption
    assert "requirePlainConsolidationDirectory" in credential_adoption
    assert "requirePlainConsolidationFile" in credential_adoption
    assert "let credentialPhase: 'parse' | 'decrypt' = 'parse'" in credential_adoption
    assert "desktop_profile_consolidation_credential_skipped" in credential_adoption
    for stable_code in (
        "archived_credential_invalid",
        "archived_credential_decryption_failed",
    ):
        assert stable_code in credential_adoption
    assert "configuration_source_credential_sha256" in parser
    assert "configuration_source_credential_size" in parser
    assert "await open(path, 'r')" in credential_reader
    assert "await handle.stat()" in credential_reader
    assert "await handle.readFile()" in credential_reader
    assert "createHash('sha256').update(raw).digest('hex')" in credential_reader
    assert "digest !== expectedSha256" in credential_reader
    assert "raw.length !== expectedSize" in credential_reader
    assert "sourceIntegrityMatches" in credential_acknowledgement
    assert "disposition = 'source_unusable'" in credential_adoption
    assert "desktopCredentialHasUserConfiguration(currentCredential)" in (
        credential_adoption
    )
    assert "expected_credential: currentCredential" in credential_adoption
    assert "if (expectedConfig === null)" in credential_adoption
    assert "configAuthority: 'generated'" in credential_adoption
    assert "importTransactionId: ''" in credential_adoption
    assert "await applyDesktopSettingsPair(" in credential_adoption
    assert "expected_config: expectedConfig" in credential_adoption
    assert "config: expectedConfig" in credential_adoption
    assert credential_adoption.index("if (expectedConfig === null)") < (
        credential_adoption.index("await applyDesktopSettingsPair(")
    )
    assert "await acknowledgeConsolidatedDesktopCredential(consolidation)" in (
        credential_adoption
    )
    assert "'acknowledge-profile-credential'" in credential_acknowledgement
    assert "acknowledged.outcome !== 'noop'" in credential_acknowledgement
    assert "acknowledged.credential_adoption_status !== 'complete'" in (
        credential_acknowledgement
    )
    # Protocol/path/content trust failures remain fatal. Only parsing and
    # decrypting the receipt-bound archived bytes are recoverable.
    assert credential_adoption.index("requirePlainConsolidationDirectory") < (
        credential_adoption.index("let credentialPhase:")
    )
    assert credential_adoption.index("requirePlainConsolidationFile") < (
        credential_adoption.index("let credentialPhase:")
    )
    assert credential_adoption.index("readVerifiedConsolidatedCredential(") < (
        credential_adoption.index("let credentialPhase:")
    )
    assert "'consolidate-profiles'" in runner
    assert "'--user-data', app.getPath('userData')" in runner
    assert "'--primary-home', profile.home" in runner
    assert "OPENSQUILLA_RECOVERY_OFFLINE: '1'" in runner
    assert "const recoveryProfiles = legacyRecoveryProfiles()" in consolidation
    assert "for (const profile of [...recoveryProfiles, primary])" in consolidation
    assert "await recoverVerifiedOrphanGatewayBeforeSpawn(profile)" in consolidation
    assert "await runDesktopProfileConsolidationCli(primary)" in consolidation
    assert "result.outcome === 'blocked'" in consolidation
    assert "result.credential_adoption_status === 'pending'" in (
        consolidation
    )
    assert "pendingDesktopCredentialConsolidation = (" in consolidation
    assert "await adoptConsolidatedDesktopCredential(result)" not in consolidation
    assert (
        "await consolidateLegacyRecoveryProfilesBeforeStartup()"
        in startup_inspection
    )
    assert "await adoptConsolidatedDesktopCredential(pending)" in startup_inspection
    assert startup_inspection.index("inspection = await inspectDesktopProfile(active)") < (
        startup_inspection.index("await adoptConsolidatedDesktopCredential(pending)")
    )
    assert startup_inspection.index(
        "await consolidateLegacyRecoveryProfilesBeforeStartup()"
    ) < startup_inspection.index("await inspectDesktopProfile(active)")


def test_blocked_profile_consolidation_is_maintenance_until_primary_inspection() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    boot_html = _read("desktop/electron/src/boot.html")
    consolidation = _section(
        main_ts,
        "async function consolidateLegacyRecoveryProfilesBeforeStartup",
        "function deferProfileConsolidationMaintenance",
    )
    startup_inspection = _section(
        main_ts,
        "async function inspectActiveProfileBeforeStartup",
        "async function openOrResumeDesktopApp",
    )

    assert "Promise<DesktopProfileConsolidationResult | null>" in consolidation
    assert "result.outcome === 'blocked'" in consolidation
    assert "return result" in consolidation
    assert "recoveryFailureResult" not in consolidation
    assert "primary_home_intact" not in main_ts
    assert "isPlainDesktopDirectory" not in main_ts
    assert consolidation.index("result.outcome === 'blocked'") < consolidation.index(
        "desktopProfilesConsolidatedThisProcess = true"
    )

    assert (
        "const consolidationFailure = await consolidateLegacyRecoveryProfilesBeforeStartup()"
        in startup_inspection
    )
    assert "let inspection = await inspectDesktopProfile(active)" in startup_inspection
    assert startup_inspection.index("let inspection = await inspectDesktopProfile(active)") < (
        startup_inspection.index("if (consolidationFailure)")
    )
    assert "if (inspection.outcome === 'recovery_required')" in startup_inspection
    assert "deferProfileConsolidationMaintenance(consolidationFailure)" in startup_inspection
    assert "desktop_profile_consolidation_primary_blocked" in startup_inspection

    # Consolidation diagnostics stay out of the blocking splash. That UI is now
    # reserved for the primary inspector's genuine recovery-required verdict.
    assert "unsafePathHelp" not in boot_html
    assert "failure_detail" not in boot_html


def test_consolidated_safe_storage_failure_cannot_publish_or_ack_as_adopted() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    decrypt_secret = _section(
        main_ts,
        "function decryptSecret",
        "function decryptApiKey",
    )
    credential_adoption = _section(
        main_ts,
        "async function adoptConsolidatedDesktopCredential",
        "let desktopProfilesConsolidatedThisProcess",
    )

    # safeStorage can throw while decrypting ciphertext from another OS
    # keychain. Keep the candidate local to the guarded try until both secrets
    # have been validated, so the catch path leaves the publish guard null.
    assert "() => safeStorage.decryptString(payload)" in decrypt_secret
    assert "decryptedSecretCache.resolve(" in decrypt_secret

    candidate_index = credential_adoption.index(
        "const candidateCredential = normalizeDesktopCredential("
    )
    provider_validation_index = credential_adoption.index(
        "resolvedCredentialApiKey = decryptApiKey(candidateCredential)",
        candidate_index,
    )
    search_validation_index = credential_adoption.index(
        "resolvedCredentialSearchApiKey = decryptSearchApiKey(candidateCredential)",
        provider_validation_index,
    )
    publish_eligibility_index = credential_adoption.index(
        "credential = candidateCredential",
        search_validation_index,
    )
    catch_index = credential_adoption.index("} catch {", publish_eligibility_index)
    publish_guard_index = credential_adoption.index(
        "if (credential !== null)",
        catch_index,
    )
    acknowledge_index = credential_adoption.index(
        "await acknowledgeConsolidatedDesktopCredential(consolidation)",
        publish_guard_index,
    )

    assert "credential = normalizeDesktopCredential(" not in credential_adoption[
        candidate_index:publish_eligibility_index
    ]
    assert (
        candidate_index
        < provider_validation_index
        < search_validation_index
        < publish_eligibility_index
        < catch_index
        < publish_guard_index
        < acknowledge_index
    )
    assert "disposition = 'source_unusable'" in credential_adoption[
        catch_index:publish_guard_index
    ]
    assert "disposition = 'adopted'" in credential_adoption[
        publish_guard_index:acknowledge_index
    ]


def test_reset_desktop_settings_forces_onboarding_before_gateway_reuse() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )
    resume = _section(
        main_ts,
        "async function openOrResumeDesktopApp",
        "function stopGateway",
    )
    reset = _section(
        main_ts,
        "ipcMain.handle('desktop:settings:reset'",
        "ipcMain.handle('desktop:artifact:open'",
    )
    cleanup_apply = _section(
        main_ts,
        "async function applyApprovedDesktopCleanup",
        "async function resetDesktopSettingsThroughCleanup",
    )
    cleanup_reset = _section(
        main_ts,
        "async function resetDesktopSettingsThroughCleanup",
        "ipcMain.handle('desktop:cleanup:apply'",
    )

    assert "let forceOnboardingOnNextStartup = false" in main_ts
    assert "function clearReusableGatewayState(): void" in main_ts
    assert "const reusableGateway = forceOnboardingOnNextStartup" in start
    assert "await reuseHealthyGatewayState(isCurrent)" in start
    assert "forceOnboardingOnNextStartup = false" in start
    assert "forceOnboardingOnNextStartup" in resume
    assert "await reuseHealthyGatewayState(" in resume
    assert "resetDesktopSettingsThroughCleanup()" in reset
    assert "inspectDesktopCleanup('reset-current-settings')" in cleanup_reset
    assert "desktopCleanupPreviews.consume(" in cleanup_reset
    assert "applyApprovedDesktopCleanup(preview" in cleanup_reset
    assert "await waitForDesktopWriterOperations(1)" in cleanup_apply
    assert "await stopOwnedGatewayAndWait()" in cleanup_apply
    assert "runDesktopCleanupCli(active, 'cleanup-inspect'" in cleanup_apply
    assert "runDesktopCleanupCli(active, 'cleanup-apply'" in cleanup_apply
    assert "report.mode === 'reset-current-settings'" in cleanup_apply
    assert "forceOnboardingOnNextStartup = true" in cleanup_apply
    assert "clearReusableGatewayState()" in cleanup_apply
    post_delete_exit = _section(cleanup_apply, "if (shouldQuit) {", "} else {")
    assert "appExitPhase = 'committed'" in post_delete_exit
    assert "destroyWindowsTray()" in post_delete_exit
    assert "app.exit(0)" in post_delete_exit
    assert "setAppExitPhase(" not in post_delete_exit
    assert "desktopLog(" not in post_delete_exit


def test_desktop_gateway_port_selection_is_bind_aware_and_bounded() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    port_selection = _section(
        main_ts,
        "const GATEWAY_PORT_FIRST = 18791",
        "async function healthCheck",
    )
    recovery = _section(
        main_ts,
        "async function startGatewayWithPortRecovery",
        "async function loadControlUi",
    )
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )

    assert "const GATEWAY_PORT_LAST = 18830" in port_selection
    assert "function isPortBindable(port: number): Promise<boolean>" in port_selection
    assert "net.createServer()" in port_selection
    assert "server.listen({ host: '127.0.0.1', port, exclusive: true })" in port_selection
    assert "await isPortBindable(port)" in port_selection
    assert "gatewayPortCursor = nextGatewayPortAfter(port)" in port_selection
    assert "OPENSQUILLA_DESKTOP_GATEWAY_PORT" in port_selection
    assert "function gatewayExitLooksLikePortInUse(output: string): boolean" in main_ts
    assert "OPENSQUILLA_GATEWAY_PORT_IN_USE" in main_ts
    assert "gateway port is already in use" in main_ts
    assert "function gatewayExitLooksLikeProfileInUse(output: string): boolean" in main_ts
    assert "OPENSQUILLA_PROFILE_IN_USE" in main_ts
    assert "Another OpenSquilla runtime is still using this profile." in main_ts
    assert "Do not delete profile lock files." in main_ts
    port_classifier = _section(
        main_ts,
        "function gatewayExitLooksLikePortInUse",
        "function gatewayExitLooksLikeProfileInUse",
    )
    assert "OPENSQUILLA_PROFILE_IN_USE" not in port_classifier
    assert (
        "const maxAttempts = hasExplicitGatewayPort() ? 1 : "
        "GATEWAY_PORT_LAST - GATEWAY_PORT_FIRST + 1"
    ) in recovery
    assert "gatewayExitLooksLikePortInUse(message)" in recovery
    assert "desktopLog('gateway_port_retry'" in recovery
    assert "if (portConflictExit && !hasExplicitGatewayPort())" in start
    assert "sendBootError(gatewayState.error)" in start


def test_windows_gateway_hard_terminate_clears_pid_without_unlinking_lock() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    cleanup = _section(
        main_ts,
        "async function clearKnownOwnedGatewayPidFile",
        "function stopGateway",
    )

    assert "gateway.pid.lock" in cleanup
    assert "join(desktopStateDir(), 'gateway.pid')" in cleanup
    assert "join(desktopStateDir(), 'gateway.pid.lock')" not in cleanup
    assert "void clearKnownOwnedGatewayPidFile()" in cleanup


def test_quit_rejected_shutdown_preserves_posix_grace_budget() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    drain = _section(
        main_ts,
        "async function drainOwnedGatewayForQuit",
        "app.on('before-quit'",
    )

    rejected = _section(
        drain,
        "if (!accepted)",
        "} else {",
    )

    assert "hardTerminateGatewayProcess(child, signalBackstop)" in rejected
    assert "process.platform === 'win32'" in rejected
    assert "GATEWAY_HARD_KILL_BACKSTOP_MS" in rejected
    assert "GATEWAY_SHUTDOWN_KILL_AFTER_MS" in rejected
    assert "await clearKnownOwnedGatewayPidFile()" in rejected


def test_windows_uninstall_preserves_app_data() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))

    assert package_json["build"]["nsis"]["deleteAppDataOnUninstall"] is False


def test_desktop_local_web_build_installs_locked_dependencies_first() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))

    assert package_json["scripts"]["build:web"] == (
        "cd ../../opensquilla-webui && npm ci && npm run build"
    )


def test_desktop_local_packaging_builds_slim_package_without_runtime_fetch() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))
    scripts = package_json["scripts"]

    for local_script in ("dist:local", "pack:local"):
        commands = scripts[local_script].split(" && ")
        assert "npm run fetch:runtimes" not in commands
        assert commands.index("npm run build:web") < commands.index("npm run build:gateway")

    runtime_resources = {
        (entry["from"], entry["to"])
        for entry in package_json["build"]["extraResources"]
        if entry["to"].startswith("runtime")
    }
    assert runtime_resources == {
        ("runtime/gateway", "runtime/gateway"),
        ("runtime/runtime-manifest.json", "runtime/runtime-manifest.json"),
        ("runtime/runtime-pack-catalog.json", "runtime/runtime-pack-catalog.json"),
    }

    assert scripts["dist"].endswith(" && npm run verify:package")
    assert scripts["pack"].endswith(" && npm run verify:package")


def test_desktop_onboarding_is_owned_modal_child_of_main_window() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    verifier = _read("desktop/electron/scripts/verify-package.mjs")
    onboarding = _section(
        main_ts,
        "async function runOnboarding",
        "async function pathExists",
    )

    assert "const parentWindow = currentMainWindow()" in onboarding
    assert "const window = new BrowserWindow" in onboarding
    assert "parent: parentWindow ?? undefined" in onboarding
    assert "modal: Boolean(parentWindow)" in onboarding
    assert "onboardingWindow = window" in onboarding
    assert "focusOnboardingWindow()" in onboarding
    assert r"const\s+window\s*=\s*new\s+" in verifier
    assert r"onboardingWindow\s*=\s*window\b" in verifier
    assert "onboardingWindowAssignmentIndex < modalOptionIndex" in verifier


def test_desktop_onboarding_defaults_to_tokenrhythm_with_trusted_registration_cta() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    html = _section(main_ts, "function onboardingHtml", "async function runOnboarding")

    assert "const TOKENRHYTHM_REGISTER_URL = 'https://tokenrhythm.studio/register'" in main_ts
    assert '<input id="provider" type="hidden" value="tokenrhythm" />' in html
    assert 'id="tokenrhythmRegister"' in html
    assert 'href="${TOKENRHYTHM_REGISTER_URL}"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
    assert 'data-i18n-aria="onboarding.step2.tokenrhythmCtaExternalLabel"' in html
    assert ".provider-promo-cta:focus-visible" in html
    assert ".provider-combobox-toggle:focus-visible" in html
    assert ".provider-option:focus-visible" in html
    assert html.rindex("syncProviderDefaults(true);") < html.rindex(
        "applyMigrationPrefill(initialProviderPrefill);"
    )
    for key in (
        "onboarding.step2.tokenrhythmTitle",
        "onboarding.step2.tokenrhythmRegistration",
        "onboarding.step2.tokenrhythmCta",
        "onboarding.step2.tokenrhythmCtaExternalLabel",
    ):
        assert main_ts.count(f"'{key}':") == 6, key

    localized_ctas = re.findall(
        r"'onboarding\.step2\.tokenrhythmCta': '([^']+)',\n"
        r"\s*'onboarding\.step2\.tokenrhythmCtaExternalLabel': '([^']+)',",
        main_ts,
    )
    assert len(localized_ctas) == 6
    for visible_cta, accessible_label in localized_ctas:
        assert visible_cta in accessible_label


def test_desktop_onboarding_exposes_immediate_and_slow_submit_feedback() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    html = _section(main_ts, "function onboardingHtml", "async function runOnboarding")
    clear_slow_timer = _section(
        html,
        "function clearSubmitSlowTimer()",
        "function setSubmitting(next)",
    )
    submitting = _section(
        html,
        "function setSubmitting(next)",
        "onboardingLocale.addEventListener('change'",
    )
    submit = _section(
        html,
        "let succeeded = false",
        "function applyMigrationPrefill",
    )

    assert (
        '<span class="submit-status" id="submitStatus" role="status" '
        'aria-live="polite" aria-atomic="true"></span>'
        in html
    )
    assert "const SUBMIT_SLOW_FEEDBACK_MS = 8_000" in html
    assert main_ts.count("savingSetup:") == 6
    assert main_ts.count("setupTakingLonger:") == 6
    assert "t.savingSetup" in submitting
    assert "desktopMessage(activeLocale, 'boot.profile')" in submitting
    assert "t.setupTakingLonger" in submitting
    assert "setTimeout" in submitting
    assert "clearTimeout" in clear_slow_timer
    assert "clearSubmitSlowTimer()" in submitting
    assert "SUBMIT_SLOW_FEEDBACK_MS" in submitting
    assert "pagehide" in html
    assert submit.index("succeeded = true") < submit.index("clearSubmitSlowTimer()")
    assert "if (!succeeded)" in submit
    assert "setSubmitting(false)" in submit


def test_desktop_tokenrhythm_single_page_onboarding_defaults_to_router() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    tokenrhythm_catalog = _section(main_ts, "id: 'tokenrhythm'", "id: 'openrouter'")
    tokenrhythm_profile = _section(main_ts, "  tokenrhythm: {", "  openrouter: {")
    onboarding_html = _section(main_ts, "function onboardingHtml", "async function runOnboarding")

    assert "routerSupported: true" in tokenrhythm_catalog
    assert "ensembleSelectionMode: 'static_tokenrhythm_b5'" in tokenrhythm_catalog
    assert "model: 'deepseek-v4-pro-0813'" in tokenrhythm_catalog
    assert "const INLINE_ROUTER_PROFILE_IDS = new Set(['tokenrhythm'])" in main_ts
    assert "!INLINE_ROUTER_PROFILE_IDS.has(credential.provider)" in main_ts
    assert "return selected.routerSupported ? 'squilla_router' : 'direct';" in onboarding_html
    assert (
        "routerMode.value = modelRoutingMode.value === 'direct' ? 'disabled' : 'recommended';"
        in onboarding_html
    )
    assert "routerTiers = clone(routerProfiles[profileKeyForMode()]);" in onboarding_html
    assert "return provider.value;" in onboarding_html
    assert "routerDefaultTier: 'c1'," in onboarding_html
    assert "routerTiers: clone(routerTiers)," in onboarding_html
    assert "[data-model-routing-mode]" not in onboarding_html
    assert "'selection_mode = \"custom_b5\"'" in main_ts
    assert "'[[llm_ensemble.candidates]]'" in main_ts
    assert "DESKTOP_ENSEMBLE_PROFILES[selectionMode]" in main_ts

    expected_models = (
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro-0813",
        "kimi-k2.7-code",
        "glm-5.2",
        "kimi-k2.6",
    )
    for model in expected_models:
        assert model in tokenrhythm_profile
    assert "ensembleEnabled: true" in tokenrhythm_profile
    assert "thinkingLevel" not in tokenrhythm_profile
    assert "ensemble_enabled = ${tier.ensembleEnabled ? 'true' : 'false'}" in main_ts


def test_desktop_legacy_inline_router_does_not_inherit_new_c3_ensemble() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    normalizer = _read("desktop/electron/src/router-tier-normalization.ts")
    package_json = json.loads(_read("desktop/electron/package.json"))

    assert "normalizeRouterTiers" in main_ts
    assert "for (const tier of Object.values(out)) delete tier.ensembleEnabled" in normalizer
    assert "hasOwnProperty.call(tier, 'ensembleEnabled')" in normalizer
    assert "hasOwnProperty.call(tier, 'ensemble_enabled')" in normalizer
    assert "normalizeBooleanSetting(ensembleEnabled, false)" in normalizer
    assert package_json["scripts"]["test:router-tier-normalization"] == (
        "npm run build && node scripts/test-router-tier-normalization.mjs"
    )


def test_desktop_onboarding_opens_only_trusted_registration_url_outside_renderer() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")
    onboarding = _section(
        main_ts,
        "async function runOnboarding",
        "async function pathExists",
    )
    window_open = _section(
        onboarding,
        "window.webContents.setWindowOpenHandler",
        "const guardOnboardingNavigation",
    )

    assert "if (url === TOKENRHYTHM_REGISTER_URL)" in window_open
    assert "void shell.openExternal(TOKENRHYTHM_REGISTER_URL)" in window_open
    assert "return { action: 'deny' }" in window_open
    assert "shell.openExternal(url)" not in window_open
    assert "openExternal" not in preload
    assert "desktop:external:open" not in main_ts
    assert "desktop:external:open" not in preload


def test_desktop_focus_prefers_open_onboarding_window() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    focus = _section(
        main_ts,
        "function focusMainWindow",
        "function installEditingContextMenu",
    )

    assert "function currentOnboardingWindow(): BrowserWindow | null" in main_ts
    assert "function focusOnboardingWindow(): boolean" in main_ts
    assert "if (focusOnboardingWindow()) return true" in focus
    onboarding_index = focus.index("if (focusOnboardingWindow()) return true")
    main_index = focus.index("if (!mainWindow || mainWindow.isDestroyed()) return false")
    assert onboarding_index < main_index


def test_start_gateway_reuses_healthy_gateway_before_spawn() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    reuse = _section(
        main_ts,
        "async function reuseHealthyGatewayState",
        "async function startGateway",
    )
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )

    assert "await healthCheck(gatewayState.url)" in reuse
    assert "gatewayState.status = 'ready'" in reuse
    reuse_guard = "const reusableGateway = forceOnboardingOnNextStartup"
    assert reuse_guard in start
    assert "await reuseHealthyGatewayState(isCurrent)" in start
    assert start.index(reuse_guard) < start.index("const overrideUrl")
    assert "if (reusableGateway) return reusableGateway" in start
    assert "hasGatewayProcessExited(gatewayProcess)" in start
    assert "stopGateway()" in start


def test_start_gateway_does_not_attach_to_unrequested_default_dev_gateway() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )

    assert "const activeProfile = activeDesktopProfile()" in start
    assert "activeProfile.kind === 'primary'" not in start
    assert "return primaryProfilePaths(app.getPath('userData'))" in main_ts
    assert "process.env.OPENSQUILLA_DESKTOP_GATEWAY_URL" in start
    assert "await healthCheck('http://127.0.0.1:18791')" not in start
    assert "gatewayState.url = 'http://127.0.0.1:18791'" not in start


def test_desktop_recovers_only_cryptographically_verified_orphan_gateway() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    recovery = _section(
        main_ts,
        "async function recoverVerifiedOrphanGatewayBeforeSpawn",
        "async function startGateway",
    )
    start = _section(main_ts, "async function startGateway", "async function loadControlUi")

    assert "loadDesktopGatewayOwnershipRecord(ownershipDir)" in recovery
    assert "runRecovery(ownershipDir, record" in recovery
    assert "current.profile_fingerprint !== desktopProfileFingerprint(profile.home)" in recovery
    assert "await verifyDesktopGatewayOwnershipWhenReady(ownershipDir, current)" in recovery
    assert "await requestVerifiedDesktopGatewayShutdown(current)" in recovery
    assert "await waitForDesktopGatewayOwnershipRelease(ownershipDir, current" in recovery
    assert "process.kill(" not in recovery
    assert "hardTerminateGatewayProcess(" not in recovery
    assert "unlink(" not in recovery
    assert "Do not delete profile lock files" in main_ts
    recovery_call = "await recoverVerifiedOrphanGatewayBeforeSpawn()"
    assert recovery_call in start
    assert start.index(recovery_call) < start.index("const port = await findGatewayPort()")
    inspect = _section(
        main_ts,
        "async function inspectActiveProfileBeforeStartup",
        "async function openOrResumeDesktopApp",
    )
    preflight_call = "await recoverVerifiedOrphanGatewayBeforeSpawn(active)"
    assert preflight_call in inspect
    assert inspect.index(preflight_call) < inspect.index("inspectDesktopProfile(active)")
    assert "liveLifecycleOwnedGatewayProcesses().length === 0" in inspect
    assert "OPENSQUILLA_DESKTOP_GATEWAY_URL" in inspect
    assert "OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE" in start
    assert "createDesktopGatewayInstanceNonce()" in start


def test_unverified_or_legacy_gateway_record_never_grants_stop_authority() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    recovery = _section(
        main_ts,
        "async function recoverVerifiedOrphanGatewayBeforeSpawn",
        "async function startGateway",
    )

    verification = recovery.index(
        "if (!await verifyDesktopGatewayOwnershipWhenReady(ownershipDir, current))"
    )
    shutdown = recovery.index("requestVerifiedDesktopGatewayShutdown(current)")
    assert verification < shutdown
    assert "gateway_ownership_record_untrusted" in recovery
    assert "gateway_ownership_not_verified" in recovery
    assert "return" in recovery[verification:shutdown]
    # The old gateway.pid schema has no port/profile/nonce proof and remains
    # deliberately absent from this recovery authority path.
    assert "gateway.pid" not in recovery


def test_desktop_blocks_macos_app_translocation_without_forcing_applications() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )

    assert "const MAC_APP_TRANSLOCATION_SEGMENT = '/AppTranslocation/'" in main_ts
    assert "function macDesktopInstallContext(): MacInstallContext" in main_ts
    assert "function assertSupportedMacInstallLocation(): void" in main_ts
    assert "process.platform !== 'darwin' || !app.isPackaged" in main_ts
    assert "blocked: translocated" in main_ts
    assert "translocated || !inApplications" not in main_ts
    assert "drag OpenSquilla.app from the DMG into Applications" in main_ts
    assert "then open OpenSquilla again" in main_ts
    assert "assertSupportedMacInstallLocation()" in start
    assert start.index("if (reusableGateway) return reusableGateway") < start.index(
        "assertSupportedMacInstallLocation()"
    )
    assert start.index("assertSupportedMacInstallLocation()") < start.index("const overrideUrl")


def test_desktop_gateway_exit_classifies_newer_config_validation_errors() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )
    wait = _section(
        main_ts,
        "async function waitForGateway",
        "async function waitForControlUi",
    )

    assert "const GATEWAY_OUTPUT_TAIL_MAX_CHARS = 12_000" in main_ts
    assert "const NEWER_CONFIG_DIAGNOSTIC_FIELDS = [" in main_ts
    for field in ["'llm_ensemble'", "'privacy'", "'sandbox.auto_setup'", "'llm_profiles'"]:
        assert field in main_ts
    assert (
        "function classifyGatewayExitMessage(message: string, outputTail: string): string"
        in main_ts
    )
    assert "settings written by a newer OpenSquilla version" in main_ts
    assert "let gatewayOutputTail = ''" in start
    assert "let childExitMessage: string | null = null" in start
    assert "appendGatewayOutputTail(gatewayOutputTail, chunk)" in start
    assert "classifyGatewayExitMessage(exitMessage, gatewayOutputTail)" in start
    assert "await waitForGateway(url, () => childExitMessage)" in start
    assert "waitForGatewayReadiness({" in wait
    assert "primaryTimeoutMs: 45_000" in wait
    assert (
        "lateGraceMs: DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS - 45_000" in wait
    )
    assert "exitMessage: earlyExitMessage" in wait
    assert "if (result.status === 'exited') throw new Error(result.message)" in wait


def test_start_gateway_preserves_host_path_without_static_runtime_injection() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )

    assert "function desktopChildPath" in main_ts
    assert "function desktopNodeBinCandidates" not in main_ts
    assert "packagedRuntimeRoot(), 'node', 'bin'" not in main_ts
    assert "OPENSQUILLA_NODE_BIN_DIR" not in start
    assert "optional Runtime Packs are resolved inside Gateway" in main_ts
    assert "PATH: childPath" in start


def test_desktop_python_children_force_utf8_stdio() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )
    cleanup = _section(
        main_ts,
        "async function runDesktopCleanupCli",
        "async function inspectDesktopCleanup",
    )

    for section in (start, cleanup):
        assert "PYTHONUNBUFFERED: '1'" in section
        assert "PYTHONUTF8: '1'" in section
        assert "PYTHONIOENCODING: 'utf-8:replace'" in section


def test_stop_gateway_sigkill_fallback_uses_real_child_exit_state() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    stop = _section(
        main_ts,
        "function stopGateway(): void",
        "// ── Desktop updates",
    )
    hard_terminate = _section(
        main_ts,
        "function hardTerminateGatewayProcess",
        "function stopGateway",
    )

    assert "child.killed" not in stop
    assert "hasGatewayProcessExited(child)" in hard_terminate
    assert "if (hasGatewayProcessExited(child)) return" in hard_terminate
    assert "if (!hasGatewayProcessExited(child))" in hard_terminate
    assert "terminateGatewayProcess(child, 'SIGKILL')" in hard_terminate
    assert "child.kill(signal)" in hard_terminate
    assert "let exited = false" in stop
    assert "child.once('exit', () => {\n      exited = true\n    })" in stop


def test_dev_gateway_runtime_is_process_tree_aware_on_termination() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function startGatewayWithPortRecovery",
    )
    terminate = _section(
        main_ts,
        "function terminateGatewayProcess",
        "function stopGateway",
    )

    assert "mode: 'dev'" in main_ts
    assert "const gatewayProcessTreeChildren = new WeakSet" in main_ts
    assert "detached: runtime.mode === 'dev' && process.platform !== 'win32'" in start
    assert "if (runtime.mode === 'dev') gatewayProcessTreeChildren.add(child)" in start
    assert "gatewayProcessTreeChildren.has(child)" in terminate
    assert "spawnSync('taskkill', ['/pid', String(pid), '/t', '/f']" in terminate
    assert "process.kill(-pid, signal)" in terminate
    assert "child.kill(signal)" in terminate


def test_desktop_update_menu_exposes_pending_downloaded_update_relaunch() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    menu = _section(
        main_ts,
        "function createApplicationMenu(): void",
        "function focusMainWindow",
    )

    assert "let downloadedUpdateVersion: string | null = null" in main_ts
    assert "downloadedUpdateVersion" in menu
    assert "desktopT('menu.relaunchToUpdate')" in menu
    assert "void applyDownloadedUpdate()" in menu
    assert "desktopT('menu.checkForUpdates')" in menu
    assert "void checkForUpdates(true)" in menu


def test_desktop_update_state_bridge_exposes_nonblocking_renderer_api() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")

    assert "type DesktopUpdateStatus =" in main_ts
    assert "interface DesktopUpdateState" in main_ts
    assert "function desktopUpdateSnapshot()" in main_ts
    assert "function publishDesktopUpdateState()" in main_ts
    assert "ipcMain.handle('desktop:update:state'" in main_ts
    assert "ipcMain.handle('desktop:update:check'" in main_ts
    assert "ipcMain.handle('desktop:update:download'" in main_ts
    assert "ipcMain.handle('desktop:update:relaunch'" in main_ts
    assert "ipcMain.handle('desktop:update:dismiss'" in main_ts
    assert "getUpdateState" in preload
    assert "checkForUpdates" in preload
    assert "downloadUpdate" in preload
    assert "relaunchToUpdate" in preload
    assert "dismissUpdate" in preload
    assert "onUpdateState" in preload
    assert "desktop:update:state-changed" in preload


def test_desktop_update_dismiss_and_persistence_cover_errors_and_source_memory() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    persist = _section(
        main_ts,
        "function persistDesktopUpdateState",
        "function activeDesktopUpdateSnoozeFor",
    )
    dismiss = _section(
        main_ts,
        "async function dismissDesktopUpdate",
        "// macOS Squirrel",
    )

    assert "desktopUpdatePersistenceWrite.then" in persist
    assert "atomicWriteFile" in persist
    assert "lastSuccessfulSource" in persist
    assert "!latestVersion && desktopUpdateStatus === 'error'" in dismiss
    assert "status: 'idle'" in dismiss
    assert "errorCode: null" in dismiss


def test_native_update_provider_events_do_not_publish_unvalidated_availability() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    update_available = _section(
        main_ts,
        "autoUpdater.on('update-available'",
        "autoUpdater.on('update-not-available'",
    )
    update_downloaded = _section(
        main_ts,
        "autoUpdater.on('update-downloaded'",
        "autoUpdater.on('error'",
    )

    assert "setDesktopUpdateState" not in update_available
    assert "provider reports update available" in update_available
    assert "showUpdateDialog" not in update_available
    assert "downloadUpdate" not in update_available

    assert "setDesktopUpdateState" in update_downloaded
    assert "status: 'downloaded'" in update_downloaded
    assert "downloadedUpdateVersion = version" in update_downloaded
    assert "createApplicationMenu()" in update_downloaded
    assert "showUpdateDialog" not in update_downloaded


def test_desktop_mock_update_is_dev_only_and_uses_native_update_surface() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    mock_version = _section(
        main_ts,
        "function mockUpdateVersion",
        "function desktopUpdateMenuEnabled",
    )
    native_gate = _section(
        main_ts,
        "function nativeAutoUpdateEnabled",
        "// macOS Squirrel",
    )
    startup = _section(main_ts, "void app.whenReady().then", "})\n}")

    assert "const MOCK_UPDATE_VERSION_ENV = 'OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION'" in main_ts
    assert "if (app.isPackaged) return null" in mock_version
    assert "process.env[MOCK_UPDATE_VERSION_ENV]" in mock_version
    assert "mockUpdateVersion() !== null" in native_gate
    assert "autoUpdateSupported() && macUpdateLocationOk()" in native_gate
    assert "desktopUpdateMenuEnabled()" in main_ts
    assert "mockUpdateVersion() !== null" in startup
    assert "desktopUpdateCheckScheduler.start(MOCK_UPDATE_CHECK_INITIAL_DELAY_MS)" in startup


def test_desktop_mock_update_flow_is_nonblocking_until_renderer_downloads() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    mock_flow = _section(
        main_ts,
        "async function runMockUpdateFlow",
        "async function downloadDesktopUpdate",
    )
    mock_download = _section(
        main_ts,
        "async function downloadDesktopUpdate",
        "function initAutoUpdater",
    )
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "setDesktopUpdateState" in mock_flow
    assert "status: 'available'" in mock_flow
    assert "showUpdateDialog" not in mock_flow
    assert "downloadedUpdateVersion = version" not in mock_flow
    assert "mockDownloadedUpdate = true" not in mock_flow

    assert "setDesktopUpdateState" in mock_download
    assert "status: 'downloading'" in mock_download
    assert "status: 'downloaded'" in mock_download
    assert "downloadedUpdateVersion = version" in mock_download
    assert "mockDownloadedUpdate = true" in mock_download
    assert "createApplicationMenu()" in mock_download
    assert "autoUpdater" not in mock_flow
    assert "quitAndInstall" not in mock_flow

    assert "if (mockDownloadedUpdate)" in apply_update
    mock_apply = _section(
        apply_update,
        "if (mockDownloadedUpdate)",
        "const pendingVersion = downloadedUpdateVersion",
    )
    assert "showUpdateDialog" in mock_apply
    assert "desktopT('update.mockInstallTitle')" in mock_apply
    assert "autoUpdater.quitAndInstall" not in mock_apply


def test_desktop_update_actions_are_guarded_against_reentry() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    download_update = _section(
        main_ts,
        "async function downloadDesktopUpdate",
        "function initAutoUpdater",
    )
    check_update = _section(
        main_ts,
        "async function runDesktopUpdateCheck",
        "async function waitForGatewayProcessExit",
    )
    check_allowed = _section(
        main_ts,
        "function desktopUpdateCheckAllowed",
        "async function runDesktopUpdateCheck",
    )
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "updateDownloadInProgress" in download_update
    assert "manualInstallerActionInProgress" in download_update
    assert "updateApplying" in download_update
    assert "desktopUpdateStatus === 'downloaded'" in download_update
    assert download_update.index("updateDownloadInProgress") < (
        download_update.index("const mockVersion = mockUpdateVersion()")
    )
    assert "if (!desktopUpdateCheckAllowed()) return" in check_update
    assert "downloading: updateDownloadInProgress ||" in check_allowed
    assert "applying: updateApplying" in check_allowed
    assert "downloaded: downloadedUpdateVersion !== null" in check_allowed
    assert "if (!mockDownloadedUpdate && !downloadedUpdateVersion) return" in apply_update
    assert apply_update.index("if (updateApplying) return") < apply_update.index(
        "if (!mockDownloadedUpdate && !downloadedUpdateVersion) return"
    )
    assert "if (isQuitting || desktopWriters.closed) return" in apply_update
    assert apply_update.index(
        "if (!mockDownloadedUpdate && !downloadedUpdateVersion) return"
    ) < apply_update.index("if (mockDownloadedUpdate)")


def test_desktop_mock_update_dialog_auto_responder_is_mock_only() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    responder = _section(
        main_ts,
        "function nextMockUpdateDialogResponse",
        "async function runMockUpdateFlow",
    )
    show_dialog = _section(
        main_ts,
        "function showUpdateDialog",
        "function showUpdateError",
    )

    assert (
        "const MOCK_UPDATE_DIALOG_RESPONSES_ENV = "
        "'OPENSQUILLA_DESKTOP_MOCK_UPDATE_DIALOG_RESPONSES'"
    ) in main_ts
    assert "if (mockUpdateVersion() === null) return null" in responder
    assert "process.env[MOCK_UPDATE_DIALOG_RESPONSES_ENV]" in responder
    assert "Number.isInteger(response)" in responder
    assert "const mockResponse = nextMockUpdateDialogResponse()" in show_dialog
    assert "response: mockResponse" in show_dialog
    assert "dialog.showMessageBox" in show_dialog


def test_desktop_mock_update_flow_has_automated_e2e_script() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))
    script = _read("desktop/electron/scripts/test-mock-update-flow.mjs")

    assert package_json["scripts"]["test:mock-update-flow"] == (
        "npm run build && node scripts/test-mock-update-flow.mjs"
    )
    assert "_electron" in script
    assert "OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION" in script
    assert "OPENSQUILLA_DESKTOP_MOCK_UPDATE_DIALOG_RESPONSES" in script
    assert "window.opensquillaDesktop.isAutoUpdateEnabled()" in script
    assert "window.opensquillaDesktop.getUpdateState" in script
    assert 'data-testid="desktop-update-download"' in script
    assert 'data-testid="update-banner"' in script
    assert "Menu.getApplicationMenu()" in script
    assert "Relaunch to Update" in script


def test_update_downloaded_records_pending_version_and_rebuilds_menu() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    update_downloaded = _section(
        main_ts,
        "autoUpdater.on('update-downloaded'",
        "autoUpdater.on('error'",
    )
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "downloadedUpdateVersion = version" in update_downloaded
    assert update_downloaded.index("downloadedUpdateVersion = version") < update_downloaded.index(
        "createApplicationMenu()"
    )
    assert "setDesktopUpdateState" in update_downloaded
    assert "status: 'downloaded'" in update_downloaded
    assert "showUpdateDialog" not in update_downloaded
    assert "if (response === 0) void applyDownloadedUpdate()" not in update_downloaded
    assert "downloadedUpdateVersion = null" in apply_update
    assert apply_update.index("downloadedUpdateVersion = null") < apply_update.index(
        "autoUpdater.quitAndInstall(false, true)"
    )


def test_generic_update_error_preserves_pending_downloaded_update_menu() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    show_error = _section(
        main_ts,
        "function showUpdateError",
        "async function runMockUpdateFlow",
    )

    assert "downloadedUpdateVersion = null" not in show_error
    assert "createApplicationMenu()" not in show_error
    assert "setDesktopUpdateState" in show_error
    assert "status: 'error'" in show_error
    assert "hadDownloadedUpdate" not in show_error


def test_silent_startup_update_error_is_not_published_as_visible_error() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    show_error = _section(
        main_ts,
        "function showUpdateError",
        "async function runMockUpdateFlow",
    )

    assert (
        "const shouldNotify = desktopUpdateCheckScheduler.consumeManualRequest() || "
        "updateDownloadInProgress"
    ) in show_error
    assert "if (!shouldNotify)" in show_error
    assert "desktopUpdateCandidate = silentFallback.candidate" in show_error
    assert "status: silentFallback.state.status" in show_error
    assert "status: downloadedUpdateVersion ? 'downloaded' : 'idle'" in show_error
    assert "error: null" in show_error
    assert show_error.index("if (!shouldNotify)") < show_error.index("status: 'error'")


def test_apply_downloaded_update_waits_for_actual_gateway_exit_before_install() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    wait_helper = _section(
        main_ts,
        "async function waitForGatewayProcessExit",
        "async function applyDownloadedUpdate",
    )
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "hasGatewayProcessExited(child)" in wait_helper
    assert "child.once('exit', () => finish(true))" in wait_helper
    assert "setTimeout(resolve" not in apply_update
    assert "await stopAndJoinAllLifecycleOwnedGateways(" in apply_update
    assert apply_update.index("await stopAndJoinAllLifecycleOwnedGateways(") < apply_update.index(
        "autoUpdater.quitAndInstall(false, true)"
    )


def test_apply_downloaded_update_timeout_restores_retry_state_before_returning() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "const pendingVersion = downloadedUpdateVersion" in apply_update
    assert "const exited = await stopAndJoinAllLifecycleOwnedGateways(" in apply_update
    assert "if (!exited || liveLifecycleOwnedGatewayProcesses().length > 0)" in apply_update
    timeout_branch = _section(
        apply_update,
        "if (!exited || liveLifecycleOwnedGatewayProcesses().length > 0)",
        "autoUpdater.quitAndInstall(false, true)",
    )
    assert "restoreDownloadedUpdateRetryState(" in timeout_branch
    assert "pendingVersion," in timeout_branch
    assert "updateWriterAdmission," in timeout_branch
    assert "return" in timeout_branch
    assert timeout_branch.index("return") < apply_update.index(
        "autoUpdater.quitAndInstall(false, true)"
    )


def test_apply_downloaded_update_handoff_error_restores_retry_state() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    restore = _section(
        main_ts,
        "function restoreDownloadedUpdateRetryState",
        "// Stop the owned gateway child",
    )
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "downloadedUpdateVersion = pendingVersion" in restore
    assert "updateApplying = false" in restore
    assert "isQuitting = false" in restore
    assert "desktopWriters.reopen(writerAdmissionToken)" in restore
    assert "setAppExitPhase('running', 'update handoff did not commit')" in restore
    assert "createWindowsTray()" in restore
    assert "createApplicationMenu()" in restore
    handoff_ready = apply_update.index("updateInstallHandoffReady = true")
    handoff_committed = apply_update.index(
        "setAppExitPhase('committed', 'handing off to desktop updater')"
    )
    handoff = apply_update.index("autoUpdater.quitAndInstall(false, true)")
    assert handoff_ready < handoff_committed < handoff
    handoff_error = _section(
        apply_update,
        "} catch (err)",
        "}\n}",
    )
    assert "restoreDownloadedUpdateRetryState(" in handoff_error
    assert "pendingVersion," in handoff_error
    assert "updateWriterAdmission," in handoff_error
    assert "showUpdateDialog" in handoff_error


def test_desktop_persists_network_observability_privacy_setting() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    types_ts = _read("opensquilla-webui/src/platform/types.ts")
    vite_env = _read("opensquilla-webui/src/vite-env.d.ts")
    connection = _section(
        main_ts,
        "interface DesktopConnection",
        "interface OnboardingPayload",
    )
    onboarding_payload = _section(
        main_ts,
        "interface OnboardingPayload",
        "interface DesktopSettingsPayload",
    )
    settings_payload = _section(
        main_ts,
        "interface DesktopSettingsPayload",
        "interface DesktopSettingsSnapshot",
    )
    snapshot = _section(main_ts, "interface DesktopSettingsSnapshot", "interface RuntimeLaunch")
    save = _section(
        main_ts,
        "async function saveDesktopCredential",
        "async function writeDesktopConfig",
    )
    config_writer = _section(
        main_ts,
        "async function writeDesktopConfig",
        "function settingsSnapshot",
    )
    config_renderer = _section(
        main_ts,
        "function renderDesktopConfigAfterPreflight",
        "async function applyDesktopSettingsPair",
    )
    web_settings = _section(
        types_ts,
        "export interface DesktopSettings",
        "export interface ProviderOption",
    )
    web_payload = _section(
        types_ts,
        "export interface DesktopSettingsPayload",
        "export interface PlatformCapabilities",
    )
    desktop_api = _section(vite_env, "interface OpenSquillaDesktopApi", "interface Window")

    assert "disableNetworkObservability: boolean" in connection
    assert "disableNetworkObservability?: unknown" in onboarding_payload
    assert "disableNetworkObservability?: unknown" not in settings_payload
    assert "interface DesktopSettingsPayload extends OnboardingPayload {}" in settings_payload
    assert "disableNetworkObservability: boolean" in snapshot
    assert "disableNetworkObservability: boolean" in web_settings
    assert "disableNetworkObservability?: boolean" in web_payload
    assert (
        "saveDesktopSettings: (payload: DesktopSettingsPayload) => Promise<DesktopSettings>"
        in desktop_api
    )

    assert "normalizeBooleanSetting(" in main_ts
    assert "payload.disableNetworkObservability" in save
    assert "existing?.disableNetworkObservability" in save
    assert "disableNetworkObservability," in save
    assert "applyDesktopSettingsPair" in config_writer
    assert "privacyConfigTomlLines(credential)" in config_renderer
    assert "function privacyConfigTomlLines" in main_ts
    assert "function desktopConfigShouldWritePrivacySection" in main_ts
    assert (
        "credential.disableNetworkObservability || "
        "readDesktopConfigNetworkObservabilitySetting() !== null"
    ) in main_ts
    assert (
        "`disable_network_observability = "
        "${credential.disableNetworkObservability ? 'true' : 'false'}`" in main_ts
    )


def test_desktop_credential_save_preserves_config_privacy_without_payload_setting() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    save = _section(
        main_ts,
        "async function saveDesktopCredential",
        "async function writeDesktopConfig",
    )
    read_config = _section(
        main_ts,
        "function readDesktopConfigNetworkObservabilitySetting",
        "function desktopConfigNetworkObservabilityDisabled",
    )

    assert (
        "const configDisableNetworkObservability = readDesktopConfigNetworkObservabilitySetting()"
    ) in save
    assert (
        ": configDisableNetworkObservability ?? existing?.disableNetworkObservability ?? false"
        in save
    )
    assert "if (!existsSync(path)) return null" in read_config
    assert "parseDesktopNetworkObservabilityPrivacyConfig(raw)" in read_config
    assert "return true" in read_config


def test_desktop_config_writer_does_not_emit_new_privacy_section_by_default() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    config_writer = _section(
        main_ts,
        "async function writeDesktopConfig",
        "function settingsSnapshot",
    )
    privacy_lines = _section(
        main_ts,
        "function privacyConfigTomlLines",
        "function plainSecret",
    )

    assert "'[privacy]'" not in config_writer
    assert "'[llm_ensemble]'" not in config_writer
    assert "if (!desktopConfigShouldWritePrivacySection(credential)) return []" in privacy_lines
    assert (
        "credential.disableNetworkObservability || "
        "readDesktopConfigNetworkObservabilitySetting() !== null" in main_ts
    )


def test_desktop_config_regeneration_preserves_control_ui_locale_and_seeds_new_config() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    credential_save = _section(
        main_ts,
        "async function saveDesktopCredential",
        "function buildImportedDesktopCredential",
    )
    locale_reader = _section(
        main_ts,
        "function persistedControlUiDefaultLocale",
        "async function readOptionalDesktopText",
    )
    config_renderer = _section(
        main_ts,
        "function renderDesktopConfigAfterPreflight",
        "async function applyDesktopSettingsPair",
    )

    # A regenerated config must retain the effective Gateway locale from its
    # own [control_ui] section, including legacy BCP-47 spellings. An absent
    # field falls back to the current Desktop locale when a new config is
    # seeded. TOML permits a comment after the table header, which must not
    # cause the locale reader to miss the section during regeneration.
    assert "if (raw === null) return null" in locale_reader
    assert r"match(/^\[\s*([^\]]+?)\s*\](?:\s*#.*)?$/)" in locale_reader
    assert "inControlUi = header[1] === 'control_ui'" in locale_reader
    assert 'default_locale\\s*=\\s*["\']([^"\']*)["\']' in locale_reader
    assert "return normalizeGatewayLocale(match[1])" in locale_reader
    assert locale_reader.rstrip().endswith("return null\n}")

    preserved_locale = (
        "const preservedControlUiLocale = persistedControlUiDefaultLocale(existingRaw)"
    )
    rendered_locale = "`default_locale = ${tomlString(preservedControlUiLocale ?? defaultLocale)}`"
    assert preserved_locale in config_renderer
    assert rendered_locale in config_renderer
    assert config_renderer.index(preserved_locale) < config_renderer.index(rendered_locale)
    assert config_renderer.count("default_locale") == 1
    assert "defaultLocale: DesktopLocale" in config_renderer
    assert (
        "const configLocale = desktopLocaleChoice(payload.locale) ?? desktopLocale"
        in credential_save
    )
    assert "writerReserved,\n      configLocale," in credential_save


def test_desktop_network_observability_disable_gates_native_update_and_gateway_env() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    update_managed = _section(
        main_ts,
        "function desktopUpdateManaged(): boolean",
        "function autoUpdateSupported",
    )
    startup = _section(main_ts, "void app.whenReady().then", "})\n}")
    start = _section(main_ts, "async function startGateway", "async function loadControlUi")
    persisted_gate = _section(
        main_ts,
        "function desktopPersistedNetworkObservabilityDisabled(): boolean",
        "function parseDesktopNetworkObservabilityPrivacyConfig",
    )
    config_gate = _section(
        main_ts,
        "function desktopConfigNetworkObservabilityDisabled(): boolean",
        "function desktopNetworkObservabilityDisabled(): boolean",
    )
    read_config = _section(
        main_ts,
        "function readDesktopConfigNetworkObservabilitySetting",
        "function desktopConfigNetworkObservabilityDisabled",
    )
    network_gate = _section(
        main_ts,
        "function desktopNetworkObservabilityDisabled(): boolean",
        "function autoUpdateSupported",
    )

    assert "function desktopPersistedNetworkObservabilityDisabled(): boolean" in main_ts
    assert "function desktopConfigNetworkObservabilityDisabled(): boolean" in main_ts
    assert "function desktopNetworkObservabilityDisabled(): boolean" in main_ts
    assert "const path = credentialPath()" in persisted_gate
    assert "if (!existsSync(path)) return false" in persisted_gate
    assert "readFileSync(path, 'utf8')" in persisted_gate
    assert "return true" in persisted_gate
    assert "const path = desktopConfigPath()" in read_config
    assert "readDesktopConfigNetworkObservabilitySetting() ?? false" in config_gate
    assert "return true" in read_config
    assert "desktopPersistedNetworkObservabilityDisabled()" in main_ts
    assert "desktopConfigNetworkObservabilityDisabled()" in main_ts
    assert (
        "return desktopPersistedNetworkObservabilityDisabled() || "
        "desktopConfigNetworkObservabilityDisabled()" in network_gate
    )
    assert "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY" in main_ts
    assert "OPENSQUILLA_TELEMETRY_DISABLED" in main_ts
    assert "OPENSQUILLA_UPDATE_CHECK_DISABLED" in main_ts
    assert "if (desktopNetworkObservabilityDisabled()) return false" in update_managed
    assert update_managed.index("desktopNetworkObservabilityDisabled()") < update_managed.index(
        "process.env.OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE"
    )
    assert "else if (desktopUpdateManaged())" in startup
    assert "desktopUpdateCheckScheduler.start(UPDATE_CHECK_INITIAL_DELAY_MS)" in startup
    assert "connection.disableNetworkObservability" in start
    assert "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: '1'" in start


def test_desktop_native_update_rechecks_daily_without_overlapping() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    scheduler_ts = _read("desktop/electron/src/update-check-scheduler.ts")
    scheduler_test = _read("desktop/electron/scripts/test-update-check-scheduler.mjs")
    startup = _section(main_ts, "void app.whenReady().then", "})\n}")
    before_quit = _section(main_ts, "app.on('before-quit'", "function shutdownFromSignal")

    assert "const UPDATE_CHECK_INITIAL_DELAY_MS = 12_000" in main_ts
    assert "const UPDATE_CHECK_REPEAT_DELAY_MS = 24 * 60 * 60 * 1000" in main_ts
    assert "desktopUpdateCheckScheduler.start(UPDATE_CHECK_INITIAL_DELAY_MS)" in startup
    assert "desktopUpdateCheckScheduler.stop()" in before_quit
    assert "if (this.inFlight)" in scheduler_ts
    assert "return this.inFlight" in scheduler_ts
    assert "if (manual) this.promoteToManual()" in scheduler_ts
    assert "this.schedule(this.repeatDelayMs)" in scheduler_ts
    assert "repeat delay starts at completion" in scheduler_test
    assert "manual caller must join the active promise" in scheduler_test


def test_package_verifier_hard_fails_stale_runtime_and_boot_contract() -> None:
    verifier = _read("desktop/electron/scripts/verify-package.mjs")
    package_json = json.loads(_read("desktop/electron/package.json"))

    assert package_json["scripts"]["verify:icons"] == "node scripts/verify-icon-config.mjs"
    assert (
        package_json["scripts"]["verify:package"]
        == "npm run verify:icons && node scripts/verify-package.mjs"
    )
    for expected in [
        "runtime is empty",
        "_AsyncConnection.create_function",
        "app.asar",
        "gatewayStartPromise",
        "openOrResumeDesktopApp",
        "create the desktop window before gateway startup",
        "first-run onboarding an owned modal child window",
        "does not prefer the onboarding window when focusing",
        "app.asar package.json version is not npm semver",
        "prereleases must use 0.5.0-rc2 style, not 0.5.0rc2",
        "must not contain bundled developer runtimes",
        "runtime-pack catalog must declare a boolean finalized flag",
        "process.exit(1)",
    ]:
        assert expected in verifier


def test_packaged_session_recovery_gate_uses_installed_electron_and_real_gateway() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))
    recovery = _read("desktop/electron/scripts/test-packaged-session-recovery.mjs")
    helpers = _read("desktop/electron/scripts/packaged-smoke-helpers.mjs")

    assert (
        package_json["scripts"]["test:packaged-session-recovery"]
        == "node scripts/test-packaged-session-recovery.mjs"
    )
    assert "_electron as electron" in helpers
    assert "executablePath" in helpers
    assert "--user-data-dir=" in helpers
    assert "connectToServer()" in recovery
    assert "app.context().routeWebSocket" in recovery
    assert recovery.index("app.context().routeWebSocket") < recovery.index("app.firstWindow")
    assert recovery.index("page.reload") < recovery.index("page.goto(sessionUrl")
    assert "chat.history" in recovery
    assert "sessions.messages.subscribe" in recovery
    assert "frame.params?.sessionKey === sessionKey" in recovery
    assert "frame.params?.key === sessionKey" in recovery
    assert "let targetSocketCounted = false" in recovery
    assert recovery.count("countTargetSocket()") == 2
    assert "client.onMessage" in recovery
    assert "server.onMessage" in recovery
    assert "server.send(message)" in recovery
    assert "client.send(message)" in recovery
    assert "page.clock" not in recovery
    assert "socketCount > 1" in recovery
    assert "expectedLastMessage" in recovery
    assert "preservedDraft" in recovery


def test_offline_document_workbench_gate_composes_owned_gateway_and_real_electron() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))
    gate = _read("desktop/electron/scripts/test-offline-document-workbench-e2e.mjs")
    native = _read("desktop/electron/scripts/test-native-workbench-v2-electron.mjs")
    ci = _read(".github/workflows/ci.yml")

    assert (
        package_json["scripts"]["test:offline-document-workbench-e2e"]
        == "npm run build && node scripts/test-offline-document-workbench-e2e.mjs"
    )
    assert "test_owned_gateway_html_workbench_lifecycle_is_offline_and_immutable" in gate
    assert "test-native-workbench-v2-electron.mjs" in gate
    assert "OPENSQUILLA_REQUIRE_ELECTRON_FOREGROUND: '1'" in gate
    assert "owned-Gateway WebSocket lifecycle" in gate
    assert "real Electron process" in gate
    assert "OPENSQUILLA_REQUIRE_ELECTRON_FOREGROUND === '1'" in native
    assert "requires an unlocked foreground GUI session" in native
    assert "OPENSQUILLA_WORKBENCH_E2E_MODE || 'stress'" in native
    assert "const annotationGeometryDeltas = fixture.stressMode" in native
    assert "? [96, -24, 24, -32, 32]" in native
    assert "const annotationRearmCycleCount = fixture.stressMode ? 3 : 1" in native
    assert "restoreTrustedAnnotationInputFocus" in native
    assert "state.ownerFocused" in native
    assert "&& state.nativeFocused" in native
    assert "&& state.documentFocused" in native
    assert "&& state.editorFocused" in native
    assert "(!ownerFocused || nativeFocused)" not in native
    assert "(!cycle.ownerFocused || cycle.nativeFocused)" not in native
    assert native.count("type: 'char'") == 1
    assert native.count(".insertText(") == 1
    assert "window.__opensquillaNativeInputProbe" in native
    assert "observed.input.length > 0" in native
    assert "ELECTRON_FOREGROUND_PREREQUISITE_MISSING" in native
    assert "TRUSTED_OVERLAY_FOCUS_CONTRACT_FAILED" in native
    assert "TRUSTED_OVERLAY_INPUT_CONTRACT_FAILED" in native
    assert ci.count(
        "offline-document-workbench-e2e:scripts/test-offline-document-workbench-e2e.mjs"
    ) == 3


def test_desktop_gateway_build_and_verifier_cover_runtime_capabilities() -> None:
    build_gateway = _read("desktop/electron/scripts/build-gateway.mjs")
    verifier = _read("desktop/electron/scripts/verify-package.mjs")

    for extra in ["recommended", "mcp", "msg", "matrix", "document-extras"]:
        assert f"'{extra}'" in build_gateway
    for module in ["joblib", "sklearn", "lightgbm", "tokenizers", "tiktoken", "onnxruntime", "mcp"]:
        assert f"'{module}'" in build_gateway
    assert "'--collect-all',\n  'sklearn'" not in build_gateway
    assert "'--collect-all',\n  'lightgbm'" not in build_gateway
    assert "'--collect-binaries',\n  'sklearn'" in build_gateway
    assert "join('bin', 'lib_lightgbm.dll')" in build_gateway
    assert "platformLightgbmBundleDir()" in build_gateway
    assert "'lightgbm/bin'" in build_gateway
    assert "lib_lightgbm.dylib" in build_gateway
    assert "libomp.dylib" in build_gateway
    assert "Git LFS pointer file, not the real router artifact" in build_gateway
    assert "git lfs pull --include=" in build_gateway
    assert "findFilesByName(runtimeGatewayDir, 'libomp.dylib')" in build_gateway
    assert "install_name_tool" in build_gateway
    assert "codesign" in build_gateway
    assert "'--force', '--sign', '-'" in build_gateway
    assert "@loader_path/libomp.dylib" in build_gateway
    assert "assertRuntimeSetReady" not in build_gateway
    assert "fetch-bundled-runtimes.mjs" not in build_gateway
    assert "verifyMacLightgbmRuntime" in verifier
    assert "lightgbm/lib/lib_lightgbm.dylib" in verifier
    assert "bundled libomp.dylib" in verifier
    assert "otool" in verifier
    assert "@loader_path/libomp.dylib" in verifier
    assert "code-task', 'stage-task-file'" in verifier
    assert "code-task', 'smoke-imports'" in verifier
    assert "code-task', 'smoke-router'" in verifier
    assert "timeout: 120000" in verifier
    gateway_smoke = _read("desktop/electron/scripts/smoke-gateway.mjs")
    assert "OPENSQUILLA_GATEWAY_SMOKE_TIMEOUT_MS" in gateway_smoke
    assert "'90000'" in gateway_smoke
    assert "function smokeEnv(tempHome, config)" in gateway_smoke
    assert "OPENSQUILLA_STATE_DIR: tempHome" in gateway_smoke
    assert "OPENSQUILLA_STATE_DIR: stateDir" not in gateway_smoke
    assert "const env = smokeEnv(tempHome, config)" in gateway_smoke
    assert "verifyGatewayCaStore(gatewayBinary, env)" in gateway_smoke
    assert re.search(r"spawn\(gatewayBinary,.*?\{.*?\benv,", gateway_smoke, re.DOTALL)
    assert "const workspaceDir = join(tempHome, 'workspace')" in gateway_smoke
    assert "await mkdir(workspaceDir, { recursive: true })" in gateway_smoke
    assert "writeFile(join(workspaceDir, 'SOUL.md')" in gateway_smoke


def test_packaged_gateway_smoke_profile_satisfies_recovery_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from opensquilla.recovery import guard_desktop_profile

    home = tmp_path / "opensquilla-gateway-smoke"
    (home / "state").mkdir(parents=True)
    workspace = home / "workspace"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text(
        "synthetic packaged gateway smoke\n",
        encoding="utf-8",
    )
    (home / "config.toml").write_text('[auth]\nmode = "none"\n', encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_INSTALL_METHOD", "desktop")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(home / "config.toml"))

    report = guard_desktop_profile(home)

    assert report is not None
    assert report.outcome == "ready"
    assert report.stable_code == "canonical_workspace"
    assert report.effective_workspace == workspace


def test_desktop_gateway_bundle_collects_usage_ledger_and_verifies_query_ui() -> None:
    """PyInstaller's package contract covers both sides of the upgrade.

    Source checkouts do not carry a generated Vite bundle. The release path
    verifies that artifact before PyInstaller runs, while this test checks the
    canonical Usage client source that feeds the bundle.
    """

    build_script = _read("desktop/electron/scripts/build-gateway.mjs")
    migration = ROOT / "migrations" / "V021__usage_ledger.py"
    usage_query = _read("opensquilla-webui/src/composables/usage/useUsageQuery.ts")

    assert "'--collect-all',\n  'opensquilla'," in build_script
    assert migration.is_file()
    assert "const USAGE_QUERY_METHOD = 'usage.query'" in usage_query
    assert "controlUiVerifier" in build_script
    assert "spawnSync(process.execPath, [controlUiVerifier, controlUiDistDir]" in build_script
    assert build_script.index("\nassertControlUiArtifactReady()\n") < build_script.index(
        "'--collect-all',\n  'opensquilla',"
    )


def test_windows_release_workflow_fails_fast_after_gateway_build_failure() -> None:
    workflow = _read(".github/workflows/wheelhouse-release.yml")
    windows_build = _section(
        workflow,
        "      - name: Build unsigned Windows installer",
        "      - name: Verify Electron package",
    )

    assert "shell: bash" in windows_build
    assert "set -euo pipefail" in windows_build
    assert windows_build.index("npm run build:gateway") < windows_build.index(
        "          npm run build\n"
    )


def test_desktop_native_artifact_open_allows_active_documents_with_file_extensions() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    artifact_list_vue = _read("opensquilla-webui/src/components/chat/ChatArtifactList.vue")
    mime_extensions = _section(main_ts, "const MIME_EXTENSIONS", "}\n\n")
    native_open = _section(
        main_ts,
        "async function openArtifactWithDefaultApp",
        "function createApplicationMenu",
    )

    assert "'text/html': '.html'" in mime_extensions
    assert "'application/xhtml+xml': '.xhtml'" in mime_extensions
    assert "function isActiveDocumentArtifactRequest" not in main_ts
    assert "shell.openPath(filePath)" in native_open
    assert "isActiveDocumentArtifact(artifact, fetched.blob)" not in artifact_list_vue


def test_desktop_cleanup_does_not_claim_os_app_uninstall() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    runtime_panel_vue = _read(
        "opensquilla-webui/src/components/settings/DesktopRuntimePanel.vue"
    )
    maintenance_panel_vue = _read(
        "opensquilla-webui/src/components/settings/DataMigrationPanel.vue"
    )
    en_locale = json.loads(_read("opensquilla-webui/src/locales/en.json"))
    zh_locale = json.loads(_read("opensquilla-webui/src/locales/zh-Hans.json"))

    cleanup = _section(
        main_ts,
        "// ── Desktop data cleanup",
        "ipcMain.handle('desktop:boot:state'",
    )

    child_environment = _section(
        main_ts,
        "function desktopChildEnvironment",
        "// ── Legacy home import detection",
    )
    assert "desktopChildEnvironment(profile" in cleanup
    assert "desktop:uninstall:summary" not in main_ts
    assert "desktop:uninstall:run" not in main_ts
    assert "OPENSQUILLA_INSTALL_METHOD: 'desktop'" in child_environment
    assert "OPENSQUILLA_STATE_DIR: profile.home" in child_environment
    assert "installed app itself will remain" in main_ts
    assert "setup.runtime.cleanup.label" not in runtime_panel_vue
    assert "setup.runtime.cleanup.label" in maintenance_panel_vue

    en_runtime = en_locale["setup"]["runtime"]
    zh_runtime = zh_locale["setup"]["runtime"]
    assert "desktop data cleanup" in en_runtime["uninstallLabel"]
    assert "remove the installed app itself" in en_runtime["uninstallDesc"]
    assert "uninstalled" not in en_runtime["uninstallDone"].lower()
    assert "remove OpenSquilla through your OS" in en_runtime["uninstallDone"]
    assert "清理桌面本地数据" in zh_runtime["uninstallLabel"]
    assert "移除已安装的应用本体" in zh_runtime["uninstallDesc"]
    assert "已卸载" not in zh_runtime["uninstallDone"]


def test_desktop_second_launch_retries_lock_and_logs_instead_of_silent_quit() -> None:
    # Issue #446: a relaunch right after closing must not silently no-op. The
    # single-instance lock is retried for a bounded window, and both success and
    # failure are recorded to a main-process launch log.
    main_ts = _read("desktop/electron/src/main.ts")

    assert "function acquireSingleInstanceLockWithRetry(): boolean" in main_ts
    assert "function desktopLog(" in main_ts
    assert "desktop.log" in main_ts
    # Bounded retry, not a single attempt.
    retry = _section(
        main_ts,
        "function acquireSingleInstanceLockWithRetry(): boolean",
        "desktopLog('launch',",
    )
    assert "Date.now() + 5_000" in retry
    assert "app.requestSingleInstanceLock()" in retry
    # On give-up: explicit dialog + quit, not a bare silent app.quit().
    giveup = _section(main_ts, "if (!gotSingleInstanceLock) {", "app.on('second-instance'")
    assert "launch_aborted_lock_held" in giveup
    assert "showErrorBox" in giveup


def test_desktop_renderer_logging_is_trusted_bounded_and_lifecycle_aware() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    renderer_log = _read("desktop/electron/src/desktop-renderer-log.ts")
    log_file = _read("desktop/electron/src/desktop-log-file.ts")
    create_window = _section(
        main_ts,
        "async function createMainWindow(): Promise<BrowserWindow>",
        "async function loadControlUi",
    )

    assert "details.frame !== window.webContents.mainFrame" in create_window
    assert "new RendererConsoleLogLimiter()" in create_window
    assert "app.getPath('home')" in create_window
    assert "webContents.on('render-process-gone'" in create_window
    assert "webContents.on('unresponsive'" in create_window
    assert "webContents.on('responsive'" in create_window
    assert "'error'" in renderer_log
    assert "'warning'," not in renderer_log
    assert "renderer_console_suppressed" in renderer_log
    assert "redactRendererLogText" in renderer_log
    assert "DESKTOP_LOG_MAX_BYTES" in log_file
    assert "DESKTOP_LOG_BACKUP_COUNT" in log_file
    assert "appendDesktopLogRecord" in main_ts


def test_desktop_renderer_loss_revokes_artifact_preview_leases() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    create_window = _section(
        main_ts,
        "async function createMainWindow(): Promise<BrowserWindow>",
        "async function loadControlUi",
    )
    cleanup = _section(
        create_window,
        "const releaseRendererOwnedArtifactPreviews = (): void => {",
        "// Forward renderer console errors",
    )
    renderer_gone = _section(
        create_window,
        "window.webContents.on('render-process-gone'",
        "window.webContents.on('unresponsive'",
    )
    full_navigation = _section(
        create_window,
        "window.webContents.on('did-start-navigation'",
        "window.on('close'",
    )

    assert "void nativeWorkbenchSurfaces.destroyAll()" in cleanup
    assert "void artifactPreviewLeaseBroker.revokeAll()" in cleanup
    assert "releaseRendererOwnedArtifactPreviews()" in renderer_gone
    assert (
        "if (isMainFrame && !isInPlace) releaseRendererOwnedArtifactPreviews()"
        in full_navigation
    )


def test_desktop_quit_drains_gateway_before_exit_on_every_platform() -> None:
    # The daily close path on every platform must wait for the owned gateway's
    # graceful drain. Otherwise Electron can exit first and leave the gateway
    # holding the profile writer lock, which blocks the next Desktop launch.
    main_ts = _read("desktop/electron/src/main.ts")

    before_quit = _section(main_ts, "app.on('before-quit'", "function shutdownFromSignal")
    drain = _section(main_ts, "async function drainOwnedGatewayForQuit", "app.on('before-quit'")
    assert "process.platform === 'win32'" not in before_quit
    assert "event.preventDefault()" in before_quit
    assert "requestOwnedGatewayShutdown(" in drain
    assert "waitForGatewayProcessExit(child)" in drain
    assert "app.exit(0)" in before_quit
    # Repeated quit events join one in-flight drain and cannot launch competing
    # shutdown/kill sequences against the same child.
    assert "let quitGatewayDrainPromise: Promise<boolean> | null = null" in main_ts
    assert "if (quitGatewayDrainPromise)" in before_quit
    assert "const children = liveLifecycleOwnedGatewayProcesses()" in before_quit
    assert "Promise.all(children.map((child) => drainOwnedGatewayForQuit(" in before_quit
    assert "if (exited)" in before_quit
    assert before_quit.index("if (exited)") < before_quit.index("app.exit(0)")


def test_desktop_signal_quit_keeps_gateway_handle_for_before_quit_drain() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    shutdown = _section(
        main_ts,
        "function shutdownFromSignal",
        "process.on('SIGINT'",
    )

    assert "stopGateway()" not in shutdown
    assert "app.quit()" in shutdown
    assert "process.on('SIGINT', shutdownFromSignal)" in main_ts
    assert "process.on('SIGTERM', shutdownFromSignal)" in main_ts


def test_desktop_quit_joins_children_already_stopping_for_other_lifecycles() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    stop = _section(main_ts, "function stopGateway", "// ── Desktop updates")
    before_quit = _section(main_ts, "app.on('before-quit'", "function shutdownFromSignal")

    assert "const gatewayStoppingProcesses = new Set" in main_ts
    assert stop.index("trackStoppingGatewayProcess(child)") < stop.index(
        "gatewayProcess = null"
    )
    assert "requestOwnedGatewayShutdown(child, url)" in stop
    assert "requestGatewayShutdown(url)" not in stop
    assert "const children = new Set(gatewayStoppingProcesses)" in main_ts
    assert "const children = liveLifecycleOwnedGatewayProcesses()" in before_quit
    assert "currentChild === child" in before_quit


def test_desktop_update_and_recovery_join_every_lifecycle_owned_gateway() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    stop_wait = _section(
        main_ts,
        "async function stopOwnedGatewayAndWait",
        "async function inspectActiveProfileBeforeStartup",
    )
    coordinator = _section(
        main_ts,
        "async function stopAndJoinAllLifecycleOwnedGateways",
        "function restoreDownloadedUpdateRetryState",
    )
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )

    assert "await stopAndJoinAllLifecycleOwnedGateways()" in stop_wait
    assert "liveProcesses: liveLifecycleOwnedGatewayProcesses" in coordinator
    assert "await stopAndJoinAllLifecycleOwnedGateways(" in apply_update
    assert "liveLifecycleOwnedGatewayProcesses().length > 0" in apply_update
    assert apply_update.index("liveLifecycleOwnedGatewayProcesses().length > 0") < (
        apply_update.index("autoUpdater.quitAndInstall(false, true)")
    )

    start = _section(main_ts, "async function startGateway", "async function loadControlUi")
    admission = "if (!lifecycleAllowsProcessSpawn("
    assert admission in start
    assert "isQuitting," in start
    assert "desktopWriters.closed," in start
    assert "liveOwnedGatewayCount," in start
    assert start.index("const port = await findGatewayPort()") < start.index(admission)
    assert start.index(admission) < start.index("const child = spawn(")


def test_desktop_update_drain_defers_user_quit_until_safe_handoff_or_retry() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    before_quit = _section(main_ts, "app.on('before-quit'", "function shutdownFromSignal")
    apply_update = _section(
        main_ts,
        "async function applyDownloadedUpdate(): Promise<void>",
        "// Lets the gateway-served Control UI",
    )
    restore = _section(
        main_ts,
        "function restoreDownloadedUpdateRetryState",
        "// Stop the owned gateway child",
    )

    assert "if (updateApplying)" in before_quit
    updater_handoff = _section(
        before_quit,
        "if (updateInstallHandoffReady) {",
        "event.preventDefault()",
    )
    assert "setAppExitPhase('committed', 'desktop updater owns exit')" in updater_handoff
    assert "destroyWindowsTray()" in updater_handoff
    assert "return" in updater_handoff
    assert "quitRequestedDuringUpdateDrain = true" in before_quit
    assert apply_update.index("updateInstallHandoffReady = true") < apply_update.index(
        "autoUpdater.quitAndInstall(false, true)"
    )
    assert "updateInstallHandoffReady = false" in restore
    assert "setImmediate(() => app.quit())" in restore


def test_desktop_quit_failure_is_fail_closed_and_retryable() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    before_quit = _section(main_ts, "app.on('before-quit'", "function shutdownFromSignal")

    assert "return exited || hasGatewayProcessExited(child)" in main_ts
    assert "if (exited)" in before_quit
    assert before_quit.index("if (exited)") < before_quit.index("app.exit(0)")
    failed = _section(before_quit, "// Fail closed:", "return\n  }")
    assert "quitGatewayDrainPromise = null" in failed
    assert "isQuitting = false" in failed
    assert "desktopWriters.reopen(quitWriterAdmission)" in failed
    assert "dialog.showErrorBox" in failed


def test_desktop_gateway_exit_classification_waits_for_stdio_close() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function startGatewayWithPortRecovery",
    )
    classifier = _section(start, "// Classify startup failures", "// A failed spawn")

    assert "child.once('close', (code, signal) =>" in classifier
    assert "classifyGatewayExitMessage(exitMessage, gatewayOutputTail)" in classifier
    assert "child.once('exit', (code, signal) =>" not in classifier


def test_desktop_gateway_ownership_control_dir_is_outside_profile_data_state() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    helper = _section(
        main_ts,
        "function desktopGatewayOwnershipDir",
        "function credentialPath",
    )
    start = _section(
        main_ts,
        "async function startGateway",
        "async function startGatewayWithPortRecovery",
    )

    assert "app.getPath('userData')" in helper
    assert "'gateway-ownership'" in helper
    assert "desktopProfileFingerprint(profile.home)" in helper
    assert "desktopStateDir()" not in helper
    assert "OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR: gatewayOwnershipDir" in start

    ownership = _read("desktop/electron/src/desktop-gateway-ownership.ts")
    launch_match = _section(
        ownership,
        "export function desktopGatewayOwnershipMatchesLaunch",
        "export interface DesktopGatewayIdentityPayload",
    )
    assert "record.instance_nonce === authority.instanceNonce" in launch_match
    assert "record.profile_fingerprint === authority.profileFingerprint" in launch_match
    assert "record.port === authority.port" in launch_match
    assert "record.pid" not in launch_match


def test_windows_process_start_identity_avoids_powershell_module_autoload() -> None:
    ownership = _read("desktop/electron/src/desktop-gateway-ownership.ts")
    windows_probe = _section(
        ownership,
        "function windowsProcessStartIdentity",
        "function posixProcessStartIdentity",
    )

    assert "Get-Process" not in windows_probe
    assert "[System.Diagnostics.Process]::GetProcessById" in windows_probe


def test_desktop_orphan_recovery_has_a_real_electron_process_flow() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))
    script = _read(
        "desktop/electron/scripts/test-desktop-gateway-orphan-recovery-flow.mjs"
    )
    main = _read("desktop/electron/src/main.ts")
    lifecycle = _read("desktop/electron/src/gateway-lifecycle.ts")

    assert package_json["scripts"]["test:gateway-orphan-recovery-flow"] == (
        "npm run build && node scripts/test-desktop-gateway-orphan-recovery-flow.mjs"
    )
    assert "firstMain.kill('SIGKILL')" in script
    assert "verifyDesktopGatewayOwnership(firstRecord)" in script
    assert "await launchDesktop(" in script
    assert "loaded.record.pid !== firstRecord.pid" in script
    assert "waitForDesktopGatewayOwnershipRelease" in script
    assert "export const DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS = 120_000" in lifecycle
    assert "const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS = 80_000" in main
    assert "const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS = 80_000" in script
    assert "DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS + CONTROL_UI_ROUTE_TIMEOUT_MS" in script
    assert (
        "VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS + INITIAL_DESKTOP_STARTUP_BUDGET_MS"
        in script
    )
    assert "createPhaseBudget('hard-crash-exit', CRASH_EXIT_BUDGET_MS)" in script
    assert "phase.remainingMs('first-window')" in script
    assert "phase.remainingMs('control-ui-route')" in script
    assert "DESKTOP_E2E_PHASE_TIMEOUT:" in script
    assert "DESKTOP_E2E_PHASE_FAILED:" in script
    assert "DESKTOP_E2E_PROCESS_EXITED:" in script
    assert "if (flowSucceeded && stillLive.length === 0)" in script
    assert "async function waitFor(check, label, timeoutMs = 60_000)" not in script


def test_desktop_dual_source_update_resolver_wires_static_channels() -> None:
    # Stable and same-base preview discovery uses a rate-limit-free static OSS
    # manifest. Versioned assets then use a strict OSS/GitHub generic feed with
    # runtime fallback; unsigned Windows verifies an exact versioned installer
    # against the release SHA256SUMS (OSS mirror first, canonical GitHub
    # Release as fail-over) before revealing it.
    main_ts = _read("desktop/electron/src/main.ts")
    resolver = _read("desktop/electron/src/update-channel.ts")
    verification = _read("desktop/electron/src/update-verification.ts")
    package_json = json.loads(_read("desktop/electron/package.json"))
    check = _section(
        main_ts,
        "async function runDesktopUpdateCheck",
        "async function waitForGatewayProcessExit",
    )
    native_check = _section(
        main_ts,
        "async function checkNativeDesktopUpdate",
        "async function downloadNativeDesktopUpdateWithFallback",
    )
    native_download = _section(
        main_ts,
        "async function downloadNativeDesktopUpdateWithFallback",
        "function desktopUpdateCheckAllowed",
    )
    verified_windows_download = _section(
        main_ts,
        "async function downloadVerifiedWindowsInstallerWithFallback",
        "function alternateDesktopUpdateSource",
    )
    manual_download = _section(
        main_ts,
        "if (desktopUpdateInstallMode() === 'manual')",
        "if (!autoUpdateSupported())",
    )

    assert "export function updateChannelPathForVersion" in resolver
    assert "'stable.json'" in resolver
    assert "`preview/${parsed.base}.json`" in resolver
    assert "latest-mac.yml" in resolver
    assert "candidate.base !== current.base" in resolver
    assert "platform assets do not match the release version" in resolver
    assert "UPDATE_OSS_RELEASE_ROOT" in resolver
    assert "UPDATE_GITHUB_RELEASE_ROOT" in resolver

    assert "function configureDesktopUpdateFeed(resolved: ResolvedDesktopUpdate)" in main_ts
    assert "provider: 'generic'" in main_ts
    assert "url: updateFeedBaseUrl(resolved.candidate, resolved.source)" in main_ts
    # Numeric rc order can disagree with electron-updater's string-based semver
    # gate (0.5.0-rc10 sorts below rc9), so the resolved-candidate path allows the
    # "downgrade"; the default path forbids it so stable users never regress.
    resolver_feed = _section(
        main_ts,
        "function configureDesktopUpdateFeed(resolved: ResolvedDesktopUpdate)",
        "async function checkNativeDesktopUpdate",
    )
    assert "autoUpdater.allowDowngrade = false" in resolver_feed
    assert "current?.rc !== null" in resolver_feed
    assert "const resolved = await resolveDesktopUpdate()" in check
    assert "await checkNativeDesktopUpdate(resolved)" in check
    assert "result?.isUpdateAvailable !== true" in native_check
    assert "result?.isUpdateAvailable !== true" in native_download
    assert "nativeUpdateReady = null" in native_check
    assert "nativeUpdateReadyFor(readyCandidate)" in native_download
    assert "nativeUpdateReadyFor(candidate)" in main_ts
    assert "manualInstallerActionInProgress = true" in manual_download
    assert "manualInstallerActionInProgress = false" in manual_download
    assert "desktopUpdateStatus === 'checking'" in manual_download
    assert "await checkForUpdates(true)" in manual_download
    assert "desktopUpdateStatus !== 'available'" in manual_download
    assert "desktopUpdateErrorMessage('source_unreachable')" in manual_download
    assert "'install_failed'" in manual_download
    assert "manualInstall" in check
    assert "updateAssetUrl(resolved.candidate, resolved.source)" in check
    assert "updateAssetUrl(candidate, source, 'SHA256SUMS')" in main_ts
    assert (
        "DESKTOP_UPDATE_CHECKSUM_SOURCES: readonly DesktopUpdateSource[] = ['oss', 'github']"
        in main_ts
    )
    assert "const UPDATE_CHECKSUM_FETCH_ATTEMPTS = 3" in main_ts
    assert "err.code === 'integrity_failed') throw err" in main_ts
    assert "desktopLog('update_checksum_fetch_retry', {" in main_ts
    assert "desktopLog('update_checksum_fetch_failed', {" in main_ts
    assert "await fetchCanonicalWindowsInstallerDigest(candidate)" in manual_download
    assert "await downloadVerifiedWindowsInstallerWithFallback(" in manual_download
    assert "alternateDesktopUpdateSource(chosen.source)" in verified_windows_download
    assert (
        "err.code === 'download_failed' || err.code === 'integrity_failed'"
        in verified_windows_download
    )
    assert "source: verified.source" in manual_download
    assert "fallbackUsed: verified.fallbackUsed" in manual_download
    assert "rememberSuccessfulUpdateSource(verified.source)" in manual_download
    assert "shell.showItemInFolder(verified.path)" in manual_download
    assert "shell.openExternal(installerUrl)" not in manual_download
    manual_discovery = _section(
        main_ts,
        "if (manualInstall) {",
        "await checkNativeDesktopUpdate(resolved)",
    )
    assert "rememberSuccessfulUpdateSource" not in manual_discovery
    assert "parseSha256SumsForAsset" in verification
    assert "streamResponseToVerifiedFile" in verification
    assert "actual !== expected" in verification
    assert "await rm(temporaryPath, { force: true })" in verification
    assert "received !== totalBytes" in verification
    assert "ipcMain.handle('desktop:update:managed'" in main_ts
    assert "'x-user-staging-id': '00000000-0000-4000-8000-000000000000'" in main_ts

    assert package_json["scripts"]["test:update-resolver"] == (
        "npm run build && node scripts/test-update-resolver.mjs"
    )


def test_gateway_spawn_state_dir_is_the_desktop_home_root() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )
    child_environment = _section(
        main_ts,
        "function desktopChildEnvironment",
        "// ── Legacy home import detection",
    )

    # OPENSQUILLA_STATE_DIR names the OpenSquilla HOME ROOT on the Python side
    # (paths.default_opensquilla_home); runtime state lives in its state/
    # subdir. The gateway child must receive desktopHome(), not the state
    # subdir, or home-derived data (managed skills, workspace/MEMORY.md,
    # session-archive, .env) nests one level too deep — the pre-0.5.x layout
    # bug now handled by the Python recovery engine before gateway startup.
    assert "desktopChildEnvironment(activeProfile" in start
    assert "OPENSQUILLA_STATE_DIR: profile.home" in child_environment
    assert "OPENSQUILLA_PROFILE_KIND: 'desktop-primary'" in child_environment
    assert "profileKindEnvironment" not in main_ts
    assert "OPENSQUILLA_STATE_DIR: desktopStateDir()" not in main_ts
    # The generated TOML keeps pinning the runtime state dir to <home>/state so
    # database paths (sessions.db, scheduler.db, agents/) never move.
    assert "state_dir = ${tomlString(join(profile.home, 'state'))}" in main_ts


def test_copyable_desktop_cli_targets_the_desktop_home_root() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    cli_invocation = _section(
        main_ts,
        "ipcMain.handle('gateway:cli-invocation'",
        "ipcMain.handle('gateway:reveal-log'",
    )

    # The copyable CLI prefix must resolve the same home-derived files as the
    # gateway child. Passing <home>/state would nest workspace, skills, and
    # other home data one level too deep for pasted commands.
    assert "stateDir: desktopHome()," in cli_invocation
    assert "stateDir: desktopStateDir()," not in cli_invocation


def test_python_recovery_engine_replaces_typescript_layout_relocation() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function loadControlUi",
    )
    inspect = _section(
        main_ts,
        "async function inspectActiveProfileBeforeStartup",
        "async function openOrResumeDesktopApp",
    )
    resume = _section(main_ts, "async function openOrResumeDesktopApp", "function stopGateway")

    assert "relocateLegacyDesktopStateLayout" not in main_ts
    assert "recoverInterruptedDesktopImport()" not in start
    assert "recoverPendingMigrationReconciliation()" not in start
    assert resume.index("inspectActiveProfileBeforeStartup()") < resume.index(
        "ensureGatewayStarted()"
    )
    assert "inspection.allowed_actions.includes('reconcile')" in inspect
    assert "'reconcile', '--home', active.home," in inspect
    assert "'--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS), '--json'," in inspect
    assert "inspection.outcome !== 'recovery_required'" in inspect


def test_attention_is_settings_only_without_native_prompt_or_acknowledgement_consumption() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    context_ts = _read("desktop/electron/src/desktop-profile-context.ts")
    inspect = _section(
        main_ts,
        "async function inspectActiveProfileBeforeStartup",
        "async function openOrResumeDesktopApp",
    )
    resume = _section(main_ts, "async function openOrResumeDesktopApp", "function stopGateway")

    assert "showAttentionPrompt" not in main_ts
    assert "attentionPromptInFlight" not in main_ts
    assert "attentionAcknowledgementFor" not in main_ts
    assert "persistAttentionAcknowledgement" not in main_ts
    assert "provenLegacyAttention" not in inspect
    assert "inspection.outcome !== 'recovery_required'" in inspect
    assert "recoveryInspection?.outcome === 'attention'" not in resume
    assert "parseAttentionAcknowledgement" not in context_ts
    assert "attention_acknowledgement" not in context_ts
    assert "current.persisted.attention_acknowledgement" not in main_ts


def test_profile_import_is_settings_only_and_windows_portable_remains_discoverable() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    onboarding = _section(main_ts, "async function runOnboarding", "async function pathExists")
    onboarding_html = _section(main_ts, "function onboardingHtml", "async function runOnboarding")
    settings_summary = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:summary'",
        "ipcMain.handle('desktop:migration:run'",
    )
    portable_detection = _section(
        main_ts,
        "function detectWindowsPortableImportCandidates",
        "function detectLegacyImportCandidates",
    )
    candidate_identity = _section(
        main_ts,
        "function legacyCandidateIdentity",
        "// Compare via realpath",
    )
    assert "Number.isSafeInteger(device)" in candidate_identity
    assert "Number.isSafeInteger(inode)" in candidate_identity
    assert "device !== 0 || inode !== 0" in candidate_identity
    assert "realpathSync(path)" in candidate_identity
    assert "canonical.toLowerCase()" in candidate_identity
    assert main_ts.count("legacyCandidateIdentity(") == 3
    assert "process.platform !== 'win32'" in portable_detection
    assert "windowsPortableHomeRoots()" in portable_detection
    assert "'windows-portable'" in portable_detection
    assert "homedir()" not in portable_detection
    assert "manuallyApprovedMigrationCandidates" not in portable_detection
    assert "detectLegacyImportCandidates()" in settings_summary
    assert "detectWindowsPortableImportCandidates()" not in onboarding
    assert "desktop:onboarding:migrate:" not in main_ts
    assert "migrationStepEnabled" not in onboarding_html
    assert 'data-screen="5"' not in onboarding_html
    assert "portable-transfer" not in main_ts


def test_run_migrate_cli_targets_desktop_home_via_bundled_cli() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    migrate = _section(
        main_ts,
        "async function runMigrateCli",
        "async function migrateSummaryJson",
    )

    assert "[...prefix, 'migrate', subcommand, ...extraArgs]" in migrate
    assert "runtime.args.slice(0, -2)" in migrate
    # OPENSQUILLA_STATE_DIR names the OpenSquilla HOME ROOT (the migrator's
    # import target) and must match the gateway spawn: desktopHome(), never the
    # state subdir.
    assert "const primary = primaryDesktopProfile()" in migrate
    assert "desktopChildEnvironment(primary" in migrate
    child_environment = _section(
        main_ts,
        "function desktopChildEnvironment",
        "// ── Legacy home import detection",
    )
    assert "OPENSQUILLA_STATE_DIR: profile.home" in child_environment
    assert "OPENSQUILLA_GATEWAY_CONFIG_PATH: join(profile.home, 'config.toml')" in child_environment
    assert "OPENSQUILLA_INSTALL_METHOD: 'desktop'" in child_environment
    for env in ("PYTHONUNBUFFERED: '1'", "PYTHONUTF8: '1'", "PYTHONIOENCODING: 'utf-8:replace'"):
        assert env in migrate
    assert "subcommand === 'verify-opensquilla-import'" in migrate
    assert "OPENSQUILLA_RECOVERY_OFFLINE: '1'" in migrate

    summary_json = _section(
        main_ts,
        "async function migrateSummaryJson",
        "type DesktopMigrationPhase",
    )
    assert "[...extraArgs, '--json']" in summary_json
    assert "writerReserved" in summary_json


def test_desktop_profile_import_always_targets_the_single_primary_profile() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    summary = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:summary'",
        "ipcMain.handle('desktop:migration:run'",
    )
    run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:migration:last-result'",
    )

    for handler in (summary, run):
        assert "activeDesktopProfile().kind !== 'primary'" not in handler
        assert "Return to the primary profile before transferring data." not in handler
    assert "target: primaryDesktopHome()" in summary
    assert "'--confirm-replace-target', primaryDesktopHome()" in run


def test_desktop_migration_run_quiesces_then_restarts_without_forcing_onboarding() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    summary = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:summary'",
        "ipcMain.handle('desktop:migration:run'",
    )
    run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:migration:last-result'",
    )

    # The dry-run summary is read-only and must not touch the running gateway.
    assert "detectLegacyImportCandidates()" in summary
    assert "requiresSelection: true" in summary
    assert "candidates.find" in summary
    assert "stopGateway" not in summary

    # The apply path quiesces the owned gateway BEFORE the CLI runs, refuses an
    # unmanaged gateway that still serves the profile, then restarts via the
    # boot splash — without forcing onboarding on the next startup.
    assert "stopGateway()" in run
    assert "await waitForGatewayProcessExit(child)" in run
    assert "const exited = await waitForGatewayProcessExit(child)" in run
    assert "if (!exited)" in run
    assert run.index("stopGateway()") < run.index("await runMigrateCli(")
    assert "A gateway is still serving this profile" in run
    assert run.index("(!gatewayProcess || !gatewayState.owned)") < run.index("isQuitting = true")
    assert run.index("A gateway is still serving this profile") < run.index("await runMigrateCli(")
    assert "'--apply'" in run
    assert "'--replace-target'" in run
    assert "'--confirm-replace-target', primaryDesktopHome()" in run
    assert "'--overwrite'" not in run
    assert "'--json'" in run
    assert "forceOnboardingOnNextStartup" not in run
    assert "bootError = null" in run
    assert "loadFile(bootPagePath())" in run
    assert "await openOrResumeDesktopApp()" in run
    # The restart happens after the CLI finished, regardless of the outcome.
    assert run.index("await runMigrateCli(") < run.index("loadFile(bootPagePath())")


def test_desktop_migration_receipt_authority_is_bounded_python_verification() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    detection = _section(
        main_ts,
        "function detectLegacyImportCandidates",
        "function bootPagePath",
    )

    assert "sourceWasImportedToTarget" not in main_ts
    assert "'.opensquilla-imported.json'" not in main_ts
    assert "join(receiptDir, 'report.json')" not in main_ts
    assert "layout-receipt.json" not in main_ts
    assert "trustedMigrationReceiptRoot" not in main_ts
    assert "MIGRATION_LAYOUT_RECEIPT_MAX_ENTRIES" not in main_ts
    assert "sourceHasCommittedLayoutReceipt" not in main_ts
    assert "verifyCommittedProfileImport" in main_ts
    assert "verify-opensquilla-import" in main_ts
    assert "matching_transaction_ids.length > 128" in main_ts
    assert "parseImportReceiptVerification" in main_ts
    assert "IMPORTED_PROVIDER_API_KEY_ENV_RE" in main_ts
    assert "!IMPORTED_PROVIDER_API_KEY_ENV_RE.test" in main_ts
    assert "previously_imported" in main_ts
    assert "addCandidate(legacyImportCandidate('cli-home', cliHome))" in detection
    assert "return candidates.sort" in detection


def test_desktop_boot_does_not_run_legacy_typescript_import_recovery() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    start = _section(
        main_ts,
        "async function startGateway",
        "async function startGatewayWithPortRecovery",
    )

    assert "function recoverInterruptedDesktopImport" not in main_ts
    assert "recoverInterruptedDesktopImport()" not in start
    assert "recoverPendingMigrationReconciliation()" not in start
    assert "relocateLegacyDesktopStateLayout" not in main_ts
    assert "await runOnboarding()" in start


def test_desktop_migration_run_requires_valid_report_and_reopens_before_restart() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:migration:last-result'",
    )

    assert "migrationReportValidationError(report" in run
    assert "migrationReportErrors(report)" in run
    assert "findAppliedReceiptForIntent(" in run
    assert "migrationTransactionIdFromReport(report)" in run
    receipt_branch = run.split("if (receipt)", 1)[1]
    assert "report = receipt.report" in receipt_branch
    assert "migrationVerified = true" in receipt_branch
    assert "isQuitting = false" in run
    assert run.rindex("desktopWriters.reopen(exclusive.admissionToken)") < run.index(
        "await openOrResumeDesktopApp()"
    )
    assert "desktopWriters.hasOtherOwner(exclusive.admissionToken)" in run
    assert "restartOk" in run


def test_desktop_migration_apply_is_bound_to_one_trusted_preview_and_native_overwrite() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    summary = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:summary'",
        "ipcMain.handle('desktop:migration:run'",
    )
    run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:migration:last-result'",
    )

    assert "trustedDesktopMigrationPreview = preview" in summary
    assert "payload?.previewId !== preview.id" in run
    assert "DESKTOP_MIGRATION_PREVIEW_TTL_MS" in run
    assert "migrationPreviewAllowsApply(preview.report, overwrite)" in run
    assert "dialog.showMessageBox" in run
    assert "trustedDesktopMigrationPreview = null" in run


def test_complete_profile_import_holds_exclusive_writer_admission_through_reconciliation() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    settings_run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:migration:last-result'",
    )
    assert "desktopWriters.tryBeginExclusive" in settings_run
    assert "await waitForDesktopWriterOperations(1)" in settings_run
    assert "exclusive.finish()" in settings_run
    assert "desktopWriters.reopen(exclusive.admissionToken)" in settings_run
    assert settings_run.index("tryBeginExclusive") < settings_run.index("'--apply'")
    assert settings_run.index("'--apply'") < settings_run.index("exclusive.finish()")

    assert "reconcileImportedDesktopCredential(intent, true)" in settings_run
    save_credential = _section(
        main_ts,
        "async function saveDesktopCredential",
        "// Sections the desktop config template owns",
    )
    assert "writerReserved = false" in save_credential
    assert "writerReserved\n    ? () => {}" in save_credential


def test_desktop_migration_writes_reconciliation_intent_before_apply() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:migration:last-result'",
    )
    assert "beginMigrationReconciliationIntent(candidate)" in run
    assert run.index("beginMigrationReconciliationIntent(candidate)") < run.index(
        "await runMigrateCli(["
    )
    assert "findAppliedReceiptForIntent(" in run


def test_settings_import_reconciles_or_prompts_for_imported_provider() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    run = _section(
        main_ts,
        "ipcMain.handle('desktop:migration:run'",
        "ipcMain.handle('desktop:boot:state'",
    )
    onboarding = _section(main_ts, "async function runOnboarding", "async function pathExists")
    save = _section(
        main_ts,
        "ipcMain.handle('desktop:onboarding:save'",
        "ipcMain.handle('desktop:onboarding:cancel'",
    )
    perform_save = _section(
        main_ts,
        "async function performOnboardingSave",
        "async function withRecoveryOperation",
    )

    assert "reconcileImportedDesktopCredential" in run
    assert "loadPendingMigrationProviderSetup" in onboarding
    assert "pendingProviderSetup" in onboarding
    assert "onboardingFlows.requestSave" in save
    assert "clearPendingMigrationProviderSetup" in perform_save
    assert "scrubImportedProviderEnvEntry" not in main_ts
    assert "readImportedProviderKey" not in main_ts
    assert "apiKey: ''" in main_ts
    assert "onboardingHtml(" in onboarding
    assert "pendingProviderSetup," in onboarding
    assert "onboardingMigrationCandidates" not in onboarding
    assert "desktopSecretStoragePolicyBackend() === 'safeStorage'" in onboarding

    reconcile = _section(
        main_ts,
        "async function reconcileImportedDesktopCredential",
        "async function recoverPendingMigrationReconciliation",
    )
    save_index = reconcile.index("await saveImportedDesktopCredential(")
    assert save_index < reconcile.index("await clearPendingMigrationProviderSetup()", save_index)

    pending_reconciliation = _section(
        main_ts,
        "async function recoverPendingMigrationReconciliation",
        "function desktopStartupLog",
    )
    assert pending_reconciliation.index(
        "beginDesktopWriterOperation('recover imported provider settings')"
    ) < pending_reconciliation.index("readPendingMigrationProviderSetup()")

    encryption = _section(main_ts, "function encryptSecret", "function decryptSecret")
    assert "desktopSecretStoragePolicyBackend()" in encryption
    assert "if (availableBackend !== 'safeStorage')" in encryption
    assert "The OS keychain is unavailable" in encryption
    assert "catch {\n      return plainSecret(secret)" not in encryption


def test_imported_credentials_are_transaction_bound_and_backed_up_only_by_python() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    normalize = _section(
        main_ts,
        "function normalizeDesktopCredential",
        "async function loadDesktopCredential",
    )
    imported_save = _section(
        main_ts,
        "function buildImportedDesktopCredential",
        "function settingsSnapshot",
    )
    backup = _section(
        main_ts,
        "function importedCredentialBackupPath",
        "async function writePendingMigrationProviderSetup",
    )
    assert "configAuthority === 'profile' && !importTransactionId" in normalize
    assert "configAuthority === 'generated' && importTransactionId" in normalize
    assert "configAuthority: 'profile'" in imported_save
    assert "importTransactionId" in imported_save
    assert "readback.importTransactionId !== importTransactionId" in imported_save
    assert "desktop-credential.import-backup.${transactionId}.json" in backup
    assert "Python's settings transaction parks the existing credential" in backup
    assert "writeFile" not in backup
    assert "copyPrimaryCredentialToRecovery" not in main_ts
    assert "createRecoveryProfile" not in main_ts


def test_invalid_desktop_credential_fails_closed_instead_of_reonboarding() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    ready = _section(main_ts, "function isConnectionReady", "function normalizeDesktopCredential")
    load = _section(
        main_ts,
        "async function loadDesktopCredential",
        "async function saveDesktopCredential",
    )

    assert "try" not in ready
    assert "catch" not in ready
    assert "code === 'ENOENT'" in load
    assert "Saved Desktop credential is invalid or unreadable." in load
    assert "catch {\n    return null" not in load


def test_settings_migration_confirmation_keys_exist_without_onboarding_or_attention_copy() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    desktop_catalog = _section(
        main_ts,
        "const DESKTOP_MESSAGES: Record<DesktopLocale, Record<string, string>> = {",
        "// Runtime string bag",
    )
    desktop_keys = [
        "migration.overwriteTitle",
        "migration.overwriteMessage",
        "migration.overwriteDetail",
        "migration.overwriteNoMerge",
        "migration.overwriteSourceUntouched",
        "migration.overwriteNoSync",
        "migration.overwriteCancel",
        "migration.overwriteConfirm",
    ]
    for key in desktop_keys:
        assert desktop_catalog.count(f"'{key}':") == 6, key
    assert "'attention." not in desktop_catalog
    assert "'migration.nav." not in desktop_catalog
    assert "'migration.step." not in desktop_catalog
    assert "migrationPreviewRunning:" not in main_ts


def test_single_page_onboarding_never_contains_profile_migration() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    html = _section(main_ts, "function onboardingHtml", "async function runOnboarding")

    assert "const initialProviderPrefill = ${inlineScriptJson(pendingProviderSetup)};" in html
    assert html.count('class="setup-card active" data-screen="1"') == 1
    assert 'data-screen="0"' not in html
    assert 'data-screen="2"' not in html
    assert 'data-screen="3"' not in html
    assert 'data-screen="4"' not in html
    assert "function routeSteps()" not in html
    assert "let step = 0;" not in html
    assert "migrationStepEnabled" not in html
    assert "migrationCandidates" not in html
    assert "OnboardingMigration" not in html
    assert 'data-screen="5"' not in html
    assert 'data-step-label="5"' not in html


def test_onboarding_inline_json_escapes_script_terminators_and_line_separators() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    helper = _section(main_ts, "function inlineScriptJson", "function routerTierTomlLines")
    html = _section(main_ts, "function onboardingHtml", "async function runOnboarding")

    assert ".replace(/</g, '\\\\u003c')" in helper
    assert ".replace(/\\u2028/g, '\\\\u2028')" in helper
    assert ".replace(/\\u2029/g, '\\\\u2029')" in helper
    assert "${JSON.stringify" not in html
    for value in (
        "DESKTOP_MESSAGES",
        "ONBOARDING_SCRIPT_MESSAGES",
        "SEARCH_PROVIDER_NOTE_MESSAGES",
        "desktopLocale",
        "PROVIDER_CATALOG",
        "SEARCH_PROVIDER_CATALOG",
        "ROUTER_PROFILES",
        "pendingProviderSetup",
    ):
        assert f"${{inlineScriptJson({value})}}" in html
    assert "${inlineScriptJson(PROVIDER_NOTE_MESSAGES)}" not in html
    assert "${inlineScriptJson(TEXT_ROUTER_TIERS)}" not in html


def test_migration_preload_bridge_and_progress_channel() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")

    assert "ipcRenderer.invoke('desktop:recovery:state')" in preload
    assert "getDesktopProfileKind" not in preload
    assert "kind === 'primary' || kind === 'recovery'" not in preload
    assert "launchSafeProfile" not in preload
    assert "retryPrimaryProfile" not in preload
    assert "returnPrimaryProfile" not in preload
    assert "'desktop:migration:summary'" in preload
    assert "'desktop:migration:run'" in preload
    assert "'desktop:migration:last-result'" in preload
    assert "'desktop:migration:peek-last-result'" in preload
    assert "'desktop:migration:dismiss-last-result'" in preload
    assert "'desktop:migration:browse-source'" in preload
    assert "'desktop:onboarding:migrate:" not in preload
    assert "chooseLegacyAgentDataLocation" in preload
    assert "'desktop:recovery:choose-legacy-agent-data'" in preload
    assert "onMigrationProgress" in preload
    assert "'desktop:migration:progress'" in preload

    assert "function publishDesktopMigrationProgress" in main_ts
    assert "webContents.send('desktop:migration:progress', payload)" in main_ts
    assert "persistDesktopMigrationResult" in main_ts
    assert "failureCode?: string" in main_ts
    assert "failureStage?: DesktopMigrationFailureStage" in main_ts
    assert "function migrationFailureFromReport" in main_ts
    assert "source_snapshot_locked" in main_ts
    assert "source_snapshot_changed" in main_ts
    assert "source_snapshot_unreadable" in main_ts
    assert "gateway_restart_failed" in main_ts
    assert "result.stderr || result.stdout" not in main_ts

    legacy_choice = _section(
        main_ts,
        "ipcMain.handle('desktop:recovery:choose-legacy-agent-data'",
        "ipcMain.handle('desktop:recovery:recover-transaction'",
    )
    assert "trustedControlUiIpc(event)" in legacy_choice
    assert "activeDesktopProfile().kind !== 'primary'" not in legacy_choice
    assert "inspection?.outcome !== 'attention'" in legacy_choice
    assert "workspace_conflict" in legacy_choice
    assert "choosePrimaryWorkspace(" in legacy_choice
    assert "'legacy-agent-data'" in legacy_choice


def test_compiled_electron_flows_preserve_xvfb_display_authority() -> None:
    package_json = json.loads(_read("desktop/electron/package.json"))
    assert package_json["scripts"]["test:profile-consolidation-flow"] == (
        "npm run build && node scripts/test-profile-consolidation-flow.mjs"
    )
    assert package_json["scripts"]["test:primary-repair-accessibility"] == (
        "npm run build && node scripts/test-primary-repair-accessibility.mjs"
    )
    assert package_json["scripts"]["test:unsafe-legacy-recovery-no-write"] == (
        "npm run build && node scripts/test-unsafe-legacy-recovery-no-write.mjs"
    )
    assert package_json["scripts"]["test:profile-import-flow"] == (
        "npm run build && node scripts/test-profile-import-flow.mjs"
    )
    for retired_script in (
        "test:profile-recovery-flow",
        "test:profile-recovery-accessibility",
        "test:unsafe-profile-no-write",
        "test:profile-recovery",
    ):
        assert retired_script not in package_json["scripts"]
    for script in (
        "desktop/electron/scripts/test-profile-consolidation-flow.mjs",
        "desktop/electron/scripts/test-primary-repair-accessibility.mjs",
        "desktop/electron/scripts/test-profile-import-flow.mjs",
    ):
        source = _read(script)
        assert "name === 'DISPLAY' || name === 'XAUTHORITY'" in source
        assert source.index("name === 'DISPLAY' || name === 'XAUTHORITY'") < source.index(
            "/(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)/i"
        )


def test_consolidation_e2e_waits_for_primary_route_and_emits_renderer_diagnostics() -> None:
    source = _read("desktop/electron/scripts/test-profile-consolidation-flow.mjs")
    control = _section(source, "async function controlPage", "async function createLegacyRecovery")

    assert "pathname !== '/control/chat' && pathname !== '/control/chat/new'" in control
    assert "candidate.locator('.chat-textarea').count()" in control
    assert "page.on('console'" in source
    assert "page.on('pageerror'" in source
    assert "windows=${JSON.stringify(windows)}" in control
    assert "gatewayLogTail: gatewayLog.slice(-8_000)" in source


def test_consolidation_e2e_covers_receipt_replay_and_inactive_state_archival() -> None:
    source = _read("desktop/electron/scripts/test-profile-consolidation-flow.mjs")

    assert "runProfileConsolidationCli(" in source
    assert "await writeFile(primaryCredential, '{}\\n', 'utf8')" in source
    assert "assert.equal(await readFile(primaryCredential, 'utf8'), '{}\\n')" in source
    assert "prelaunchConsolidation.credential_adoption_status, 'pending'" in source
    assert "pendingReceiptRecoveredAfterCrash: true" in source
    assert "completedReceiptDidNotResurrectCredential: true" in source
    assert "invalidCredentialStableCode" in source
    assert "'archived_credential_invalid'" in source
    assert "'not_required'" in source
    assert "'complete'" in source
    assert "credentialOnlySourceGeneratedPrimary: true" in source
    assert "generatedCredential.configAuthority, 'generated'" in source
    assert "generatedCredential.importTransactionId, ''" in source

    # Supported historical data becomes active, while unknown/runtime state is
    # absent from active state and remains traceable in non-active recovered-data
    # plus the immutable consolidation backup.
    assert "'session-archive'" in source
    assert "'recovered-data'" in source
    assert "pathExists(join(primaryHome, 'state'" in source
    assert "archivedProfiles" in source


def test_obsolete_profile_consolidation_escape_hatch_is_removed() -> None:
    """The product path replaces the old environment-variable workaround."""

    main_ts = _read("desktop/electron/src/main.ts")
    preload = _read("desktop/electron/src/preload.cts")

    assert "OPENSQUILLA_DESKTOP_SKIP_PROFILE_CONSOLIDATION" not in main_ts
    assert "profileConsolidationOptOut" not in main_ts
    assert "desktop_profile_consolidation_skipped" not in main_ts
    assert "desktopProfileConsolidationMaintenance" in main_ts
    assert "retryDeferredProfileConsolidation" in main_ts
    assert "'desktop:recovery:retry-consolidation'" in main_ts
    assert "'desktop:recovery:retry-consolidation'" in preload


def test_blocked_consolidation_defers_only_after_primary_is_bootable() -> None:
    """Maintenance never overrides the primary inspector's startup verdict."""

    main_ts = _read("desktop/electron/src/main.ts")
    deferral = _section(
        main_ts,
        "function deferProfileConsolidationMaintenance",
        "function recoveryStateSnapshot",
    )
    startup = _section(
        main_ts,
        "async function inspectActiveProfileBeforeStartup",
        "async function openOrResumeDesktopApp",
    )

    assert "desktop_profile_consolidation_deferred" in deferral
    assert "desktopProfileConsolidationMaintenance = {" in deferral
    assert "desktopProfileConsolidationFailureDetail" in deferral
    assert "let desktopProfileConsolidationDeferredThisProcess = false" in main_ts

    decision = startup.split("if (consolidationFailure)")[1].split(
        "recoveryInspection = inspection",
    )[0]
    assert "inspection.outcome === 'recovery_required'" in decision
    assert "deferProfileConsolidationMaintenance(consolidationFailure)" in decision
    assert decision.index("inspection.outcome === 'recovery_required'") < decision.index(
        "deferProfileConsolidationMaintenance(consolidationFailure)"
    )

    # Interrupted profile transactions are attempted automatically before the
    # severe blocking UI is considered.
    assert "inspection.allowed_actions.includes('recover-transaction')" in startup
    assert "'profile_transaction_auto_recovery_failed'" in startup

    # An interrupted cleanup is abandoned automatically: the journal record is
    # archived and every surviving file is preserved, so startup continues on
    # the remaining profile without a manual confirmation.
    assert "inspection.allowed_actions.includes('abandon-cleanup')" in startup
    assert "inspection.stable_code === 'cleanup_transaction_incomplete'" in startup
    assert "'cleanup_auto_abandon_failed'" in startup

    # A corrupt config is repaired automatically from its newest valid backup
    # (defaults otherwise) after the corrupt file is preserved beside itself.
    assert "inspection.allowed_actions.includes('recover-config')" in startup
    assert "'recover-config', '--home', active.home," in startup
    assert "'--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS), '--json'," in startup
    assert "'config_auto_recovery_failed'" in startup


def test_recovery_protocol_detail_reaches_the_blocking_panel() -> None:
    """The engine's sanitized diagnosis travels intact from CLI JSON to boot UI."""

    main_ts = _read("desktop/electron/src/main.ts")
    boot_html = _read("desktop/electron/src/boot.html")
    parse = _section(
        main_ts,
        "function parseRecoveryProtocol",
        "function parseDesktopProfileConsolidationProtocol",
    )
    render_recovery = _section(
        boot_html,
        "function renderRecoveryState(state, moveFocus = true)",
        "async function runRecoveryAction",
    )

    assert "detail: string | null" in main_ts
    # Older CLIs omit the key entirely; both absence and null render nothing.
    assert "typeof record.detail === 'string' ? record.detail : null" in parse
    assert "detail: null," in main_ts
    assert 'id="recoveryDetail"' in boot_html
    assert "typeof inspection.detail === 'string'" in render_recovery
    assert "recoveryDetail.textContent = detail" in render_recovery
    assert "recoveryDetail.hidden = !detail" in render_recovery
