import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, open, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import {
  canAcceptWindowsElectronShutdownFallback,
  closeElectronWithDeadline,
  desktopShutdownEvidenceSince,
} from './e2e-shutdown-helpers.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const ELECTRON_SHUTDOWN_TIMEOUT_MS = 15_000
const flowControl = process.argv.includes('--flow-control')
const connectionFaults = process.argv.includes('--connection-faults')
const outageOption = process.argv.find(value => value.startsWith('--outage-ms='))
const outageMs = outageOption ? Number(outageOption.split('=')[1]) : 5_000
if (!Number.isInteger(outageMs) || outageMs < 5_000 || outageMs > 600_000) {
  throw new Error('outage-ms must be an integer between 5000 and 600000')
}

async function waitFor(check, label, timeoutMs = 60_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(200)
  }
  const suffix = lastError ? ` Last error: ${lastError.message || lastError}` : ''
  throw new Error(`Timed out waiting for ${label}.${suffix}`)
}

async function mainWindowSnapshot(app) {
  return await app.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows().find((candidate) => (
      candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
    ))
    if (!window) return null
    return {
      browserWindowId: window.id,
      webContentsId: window.webContents.id,
      url: window.webContents.getURL(),
      visible: window.isVisible(),
      minimized: window.isMinimized(),
      focused: window.isFocused(),
      destroyed: window.isDestroyed(),
    }
  })
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-electron-window-close-test-'))
const userDataDir = join(isolationRoot, 'chromium-user-data')
const isolatedHome = join(isolationRoot, 'home')
const isolatedRoaming = join(isolationRoot, 'AppData', 'Roaming')
const isolatedLocal = join(isolationRoot, 'AppData', 'Local')
let desktopApp
let flowSucceeded = false
let outage = false
let reconnectAttempts = 0
let acceptedSockets = 0
let negotiatedFlow = false
let warmRecoveryMs = null
let continuityPage
let continuityDiagnostics = null
const routedClients = new Set()
const routedServers = new WeakMap()

// Observe only lifecycle/element categories, never text, values, URLs or keys.
// The ring is bounded and records transitions, not every DOM mutation/poll.
async function installContinuityObservation(page, app) {
  await page.evaluate(() => {
    const startedAt = Date.now()
    const original = window.__stabilityComposer
    const records = []
    let dropped = 0
    let lastState = ''
    const category = element => {
      if (!element) return 'none'
      if (element === original) return 'original-composer'
      if (element === document.querySelector('.chat-textarea')) return 'replacement-composer'
      if (element === document.body) return 'body'
      if (element === document.documentElement) return 'document'
      if (element === window) return 'window'
      const tag = String(element.tagName || '').toLowerCase()
      return ['button', 'input', 'textarea', 'a', 'div', 'iframe'].includes(tag) ? tag : 'other'
    }
    const state = () => {
      const current = document.querySelector('.chat-textarea')
      return {
        composerPresent: Boolean(current),
        sameComposer: current === original,
        originalConnected: Boolean(original?.isConnected),
        focusedComposer: document.activeElement === original,
        activeElement: category(document.activeElement),
        documentFocused: document.hasFocus(),
        visibility: document.visibilityState,
        composerDisabled: current?.disabled ?? null,
        composerReadOnly: current?.readOnly ?? null,
      }
    }
    const record = (event, target, force = false) => {
      const current = state()
      const serialized = JSON.stringify(current)
      if (!force && serialized === lastState) return
      lastState = serialized
      if (records.length >= 96) { records.shift(); dropped++ }
      records.push({ atMs: Date.now() - startedAt, event, target: category(target), ...current })
    }
    const onLifecycle = event => record(event.type, event.target, true)
    const lifecycleEvents = ['focus', 'blur', 'focusin', 'focusout', 'visibilitychange', 'pagehide', 'pageshow']
    for (const event of lifecycleEvents) window.addEventListener(event, onLifecycle, true)
    const observer = new MutationObserver(() => record('dom-change'))
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'readonly'] })
    const timer = setInterval(() => record('sample'), 500)
    record('start', original, true)
    window.__stabilityContinuityObservation = {
      read: () => ({ state: state(), records: [...records], dropped }),
      stop: () => {
        clearInterval(timer)
        observer.disconnect()
        for (const event of lifecycleEvents) window.removeEventListener(event, onLifecycle, true)
      },
    }
  })
  await app.evaluate(({ BrowserWindow }) => {
    const main = BrowserWindow.getAllWindows().find(candidate => (
      candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
    ))
    if (!main) return
    const startedAt = Date.now()
    const records = []
    let dropped = 0
    const record = event => {
      if (records.length >= 64) { records.shift(); dropped++ }
      records.push({ atMs: Date.now() - startedAt, event, focused: main.isFocused(),
        visible: main.isVisible(), minimized: main.isMinimized() })
    }
    const listeners = []
    for (const event of ['focus', 'blur', 'show', 'hide', 'minimize', 'restore']) {
      const listener = () => record(event)
      main.on(event, listener)
      listeners.push([event, listener])
    }
    record('start')
    globalThis.__stabilityWindowObservation = {
      read: () => ({ records: [...records], dropped }),
      stop: () => { for (const [event, listener] of listeners) main.removeListener(event, listener) },
    }
  })
}

async function readContinuityObservation(page, app) {
  return {
    renderer: await page?.evaluate(() => window.__stabilityContinuityObservation?.read()).catch(() => null),
    window: await app?.evaluate(() => globalThis.__stabilityWindowObservation?.read()).catch(() => null),
  }
}

try {
  await mkdir(userDataDir, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })
  await mkdir(isolatedRoaming, { recursive: true })
  await mkdir(isolatedLocal, { recursive: true })

  // Use a synthetic keyless profile so the lifecycle test reaches the Control
  // UI without reading developer credentials or requiring an external model.
  const now = new Date().toISOString()
  await writeFile(join(userDataDir, 'desktop-credential.json'), JSON.stringify({
    provider: 'ollama',
    model: 'opensquilla-window-close-test-model',
    baseUrl: 'http://127.0.0.1:11434',
    apiKeyEnv: '',
    encryptedApiKey: '',
    modelRoutingMode: 'direct',
    routerMode: 'disabled',
    routerDefaultTier: 'c1',
    routerTiers: {},
    searchProvider: 'duckduckgo',
    searchApiKeyEnv: '',
    encryptedSearchApiKey: '',
    encryption: 'plain',
    disableNetworkObservability: false,
    createdAt: now,
    updatedAt: now,
  }, null, 2), { mode: 0o600 })

  desktopApp = await electron.launch({
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
      packageRoot,
    ],
    env: {
      ...Object.fromEntries(Object.entries(process.env).filter(([key]) => !(
        /(^OPENSQUILLA_|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY|PASSWORD|^ELECTRON_RUN_AS_NODE$)/i.test(key)
      ))),
      HOME: isolatedHome,
      USERPROFILE: isolatedHome,
      APPDATA: isolatedRoaming,
      LOCALAPPDATA: isolatedLocal,
      OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
      OPENSQUILLA_AUTH_MODE: 'token',
      OPENSQUILLA_AUTH_TOKEN: 'synthetic-window-flow-operator-token',
      OPENSQUILLA_GATEWAY_WS_TRANSPORT_FLOW_ENABLED: flowControl ? 'true' : 'false',
    },
  })

  const runtimeIsolation = await desktopApp.evaluate(({ app }) => ({
    userData: app.getPath('userData'),
    platform: process.platform,
  }))
  assert.equal(await realpath(runtimeIsolation.userData), await realpath(userDataDir))

  const installFaultRoute = async () => {
    await desktopApp.context().routeWebSocket(/\/ws$/, client => {
      if (outage) {
        reconnectAttempts++
        client.close({ code: 1013, reason: 'Isolated connectivity fault' })
        return
      }
      acceptedSockets++
      routedClients.add(client)
      const server = client.connectToServer()
      routedServers.set(client, server)
      client.onClose((code, reason) => {
        routedClients.delete(client)
        void server.close({ code, reason })
      })
      server.onClose((code, reason) => {
        routedClients.delete(client)
        void client.close({ code, reason })
      })
      server.onMessage(message => {
        try {
          const frame = JSON.parse(String(message))
          if (frame?.policy?.transport_flow?.delivery_epoch) negotiatedFlow = true
        } catch { /* Non-JSON frames remain transparent. */ }
        client.send(message)
      })
    })
  }

  const page = await desktopApp.firstWindow({ timeout: 60_000 })
  continuityPage = page
  await page.waitForLoadState('domcontentloaded', { timeout: 60_000 }).catch(() => {})
  await waitFor(
    async () => page.url().startsWith('opensquilla-app://desktop/chat'),
    'Desktop renderer to load on Chat',
  )
  await waitFor(
    async () => (await page.evaluate(
      () => window.opensquillaDesktop?.getGatewayConnection?.(),
    ))?.status === 'ready',
    'Desktop Gateway readiness',
    // This readiness window includes the profile inspection and settings
    // reconciliation that run before the Gateway process can be spawned.
    120_000,
  )
  // Fixture setup only: install interception before the measured connection.
  // No reload, refresh or navigation is permitted during fault recovery.
  if (connectionFaults) {
    await installFaultRoute()
    await page.reload({ waitUntil: 'domcontentloaded' })
  }
  const gatewayAccess = await page.evaluate(async () => {
    const connection = await window.opensquillaDesktop?.getGatewayConnection?.()
    const response = await fetch('/api/system/status', {
      headers: { Authorization: 'Bearer stale-renderer-token' },
    })
    return {
      authToken: connection?.authToken || '',
      status: response.status,
    }
  })
  assert.match(gatewayAccess.authToken, /^[0-9a-f]{64}$/)
  assert.equal(gatewayAccess.status, 200)

  // Exercise the real preload -> platform resume bridge without suspending
  // the developer's computer. The signal must preserve the live renderer.
  const composer = page.locator('.chat-textarea').first()
  await composer.waitFor({ state: 'visible', timeout: 60_000 })
  const draft = 'isolated stability draft - never send'
  await composer.fill(draft)
  await composer.focus()
  const resumeUrl = page.url()
  await page.evaluate(() => {
    window.__stabilityComposer = document.querySelector('.chat-textarea')
    window.__stabilityResumeSignals = 0
    window.__stabilityDetachResume = window.opensquillaDesktop.onSystemResume(() => {
      window.__stabilityResumeSignals++
    })
  })
  await installContinuityObservation(page, desktopApp)
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Network.enable')
  let socketsClosed = 0
  cdp.on('Network.webSocketClosed', () => { socketsClosed++ })
  await desktopApp.evaluate(({ powerMonitor }) => {
    powerMonitor.emit('resume')
    powerMonitor.emit('resume')
  })
  await waitFor(async () => (await page.evaluate(() => window.__stabilityResumeSignals)) === 2,
    'preload system-resume bridge')
  await delay(6_000)
  assert.equal(page.url(), resumeUrl, 'resume must not navigate or reload')
  assert.equal(await composer.inputValue(), draft, 'resume must preserve the unsent draft')
  continuityDiagnostics = await readContinuityObservation(page, desktopApp)
  assert.equal(continuityDiagnostics.renderer?.state.sameComposer, true, 'resume must preserve composer identity')
  assert.equal(continuityDiagnostics.renderer?.state.focusedComposer, true, 'resume must preserve composer focus')
  assert.equal(socketsClosed, 0, 'healthy resume must not close a shared WebSocket')
  await page.evaluate(() => window.__stabilityDetachResume())
  await cdp.detach()

  if (connectionFaults) {
    await waitFor(async () => routedClients.size === 1, 'one measured Gateway connection')
    if (flowControl) assert.equal(negotiatedFlow, true, 'candidate must negotiate flow control')
    const acceptedBefore = acceptedSockets
    outage = true
    for (const client of [...routedClients]) {
      routedClients.delete(client)
      await client.close({ code: 1013, reason: 'Isolated network interruption' })
      await routedServers.get(client)?.close({ code: 1013, reason: 'Isolated network interruption' })
    }
    await delay(outageMs)
    assert.equal(await composer.inputValue(), draft, 'offline editing must preserve the draft')
    outage = false
    const recoverStarted = Date.now()
    await page.evaluate(() => window.dispatchEvent(new Event('online')))
    await waitFor(async () => acceptedSockets > acceptedBefore && !await page.locator('.chat-send-btn.btn--primary').isDisabled(),
      'automatic warm recovery with no user action', 30_000)
    warmRecoveryMs = Date.now() - recoverStarted
    assert.equal(page.url(), resumeUrl)
    assert.equal(await composer.inputValue(), draft)
    continuityDiagnostics = await readContinuityObservation(page, desktopApp)
    assert.equal(continuityDiagnostics.renderer?.state.sameComposer, true, 'actual reconnect must preserve composer identity')
    assert.equal(continuityDiagnostics.renderer?.state.focusedComposer, true, 'actual reconnect must preserve composer focus')
    assert.ok(reconnectAttempts <= 8 + Math.ceil(outageMs / 5_000), 'interruption must not cause a reconnect storm')
  }

  await page.evaluate(() => window.__stabilityContinuityObservation?.stop())
  await desktopApp.evaluate(() => globalThis.__stabilityWindowObservation?.stop())

  const preferences = await page.evaluate(
    () => window.opensquillaDesktop.getDesktopPreferences?.(),
  )
  assert.ok(preferences, 'new desktop shell must expose window-close preferences')

  const backgroundSupported = runtimeIsolation.platform === 'darwin'
    || runtimeIsolation.platform === 'win32'
  assert.equal(preferences.canRunInBackground, backgroundSupported)
  assert.equal(
    preferences.mainWindowCloseBehavior,
    backgroundSupported ? 'background' : 'quit',
  )

  if (!backgroundSupported) {
    console.log(JSON.stringify({
      ok: true,
      platform: runtimeIsolation.platform,
      behavior: preferences.mainWindowCloseBehavior,
      backgroundSupported,
    }, null, 2))
  } else {
    const marker = `renderer-${Date.now()}`
    await page.evaluate((value) => {
      window.__opensquillaWindowLifecycleMarker = value
    }, marker)

    const before = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'visible main window',
    )

    await desktopApp.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window) throw new Error('Main Desktop window is unavailable.')
      window.close()
    })

    const hidden = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot && !snapshot.visible ? snapshot : null
      },
      'main window to hide without closing',
    )
    assert.equal(hidden.destroyed, false)
    assert.equal(hidden.browserWindowId, before.browserWindowId)
    assert.equal(hidden.webContentsId, before.webContentsId)
    // The live SPA may move between client-side routes while hidden, so only
    // require that the window was not navigated away from the Desktop renderer
    // or reloaded to the boot splash.
    // Renderer continuity itself is proven by the marker check below.
    assert.ok(
      hidden.url.startsWith('opensquilla-app://desktop/'),
      `hidden window must stay on the Desktop renderer, got ${hidden.url}`,
    )
    assert.equal(page.isClosed(), false)

    await desktopApp.evaluate(({ app }) => {
      app.emit('activate')
    })

    const revealed = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'same main window to be revealed',
    )
    assert.equal(revealed.browserWindowId, before.browserWindowId)
    assert.equal(revealed.webContentsId, before.webContentsId)
    assert.ok(
      revealed.url.startsWith('opensquilla-app://desktop/'),
      `revealed window must stay on the Desktop renderer, got ${revealed.url}`,
    )
    assert.equal(
      await page.evaluate(() => window.__opensquillaWindowLifecycleMarker),
      marker,
      'revealing a hidden desktop window must preserve renderer state',
    )

    await desktopApp.evaluate(({ app, BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window) throw new Error('Main Desktop window is unavailable.')
      // Reproduce the activation race deterministically: activateMainWindow()
      // focuses synchronously, then continues across an asynchronous startup
      // boundary. A user hide after that first focus must not be undone by a
      // delayed second focus.
      app.emit('activate')
      window.hide()
    })
    await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot && !snapshot.visible ? snapshot : null
      },
      'main window to hide before deep-link activation',
    )
    await delay(350)
    assert.equal(
      (await mainWindowSnapshot(desktopApp))?.visible,
      false,
      'an asynchronous activation tail must not reveal a window hidden afterward',
    )

    await desktopApp.evaluate(({ app }) => {
      app.emit(
        'second-instance',
        {},
        ['OpenSquilla', 'opensquilla://unknown'],
        process.cwd(),
        {},
      )
    })
    await delay(350)
    assert.equal(
      (await mainWindowSnapshot(desktopApp))?.visible,
      false,
      'an unknown deep-link action must not reveal the window',
    )

    await desktopApp.evaluate(({ app }) => {
      app.emit(
        'second-instance',
        {},
        ['OpenSquilla', 'opensquilla://open'],
        process.cwd(),
        {},
      )
    })
    const secondInstanceRevealed = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'second-instance deep link to reveal the same main window',
    )
    assert.equal(secondInstanceRevealed.browserWindowId, before.browserWindowId)
    assert.equal(secondInstanceRevealed.webContentsId, before.webContentsId)

    await desktopApp.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window) throw new Error('Main Desktop window is unavailable.')
      window.hide()
    })
    const openUrlPrevented = await desktopApp.evaluate(({ app }) => {
      let prevented = false
      app.emit('open-url', {
        preventDefault() {
          prevented = true
        },
      }, 'opensquilla://open')
      return prevented
    })
    assert.equal(openUrlPrevented, true)
    const openUrlRevealed = await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.visible ? snapshot : null
      },
      'open-url deep link to reveal the same main window',
    )
    assert.equal(openUrlRevealed.browserWindowId, before.browserWindowId)
    assert.equal(openUrlRevealed.webContentsId, before.webContentsId)

    const minimizable = await desktopApp.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows().find((candidate) => (
        candidate.webContents.getURL().startsWith('opensquilla-app://desktop/')
      ))
      if (!window || !window.isMinimizable()) return false
      window.minimize()
      return true
    })
    const minimized = minimizable && Boolean(await waitFor(
      async () => {
        const snapshot = await mainWindowSnapshot(desktopApp)
        return snapshot?.minimized ? snapshot : null
      },
      'main window to minimize before deep-link activation',
      5_000,
    ).catch(() => null))
    if (minimized) {
      await desktopApp.evaluate(({ app }) => {
        app.emit(
          'second-instance',
          {},
          ['OpenSquilla', 'opensquilla://open'],
          process.cwd(),
          {},
        )
      })
      const restored = await waitFor(
        async () => {
          const snapshot = await mainWindowSnapshot(desktopApp)
          return snapshot?.visible && !snapshot.minimized ? snapshot : null
        },
        'deep link to restore a minimized main window',
      )
      assert.equal(restored.browserWindowId, before.browserWindowId)
      assert.equal(restored.webContentsId, before.webContentsId)
    }

    assert.equal(
      await page.evaluate(() => window.__opensquillaWindowLifecycleMarker),
      marker,
      'deep-link activation must preserve renderer state',
    )

    console.log(JSON.stringify({
      ok: true,
      platform: runtimeIsolation.platform,
      behavior: preferences.mainWindowCloseBehavior,
      backgroundSupported,
      browserWindowId: revealed.browserWindowId,
      webContentsId: revealed.webContentsId,
      rendererPreserved: true,
      secondInstanceDeepLink: true,
      openUrlDeepLink: true,
      minimizedRestored: minimized,
      resumeBridgePreservedDraftAndSocket: true,
      connectionFaults,
      outageMs: connectionFaults ? outageMs : null,
      flowControl,
      negotiatedFlow,
      reconnectAttempts,
      warmRecoveryMs,
      continuityDiagnostics,
    }, null, 2))
  }
  flowSucceeded = true
} catch (error) {
  continuityDiagnostics = await readContinuityObservation(continuityPage, desktopApp)
  const windows = desktopApp
    ? await desktopApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(
        (window) => ({
          destroyed: window.isDestroyed(),
          title: window.getTitle(),
          url: window.webContents.getURL(),
          visible: window.isVisible(),
        }),
      )).catch(() => [])
    : []
  const desktopLog = await readFile(
    join(userDataDir, 'logs', 'desktop.log'),
    'utf8',
  ).catch(() => '')
  console.error(JSON.stringify({
    error: error instanceof Error ? error.message : String(error),
    routedConnectionCount: routedClients.size,
    acceptedSockets,
    negotiatedFlow,
    continuityDiagnostics,
    windows,
    desktopLog,
  }, null, 2))
  throw error
} finally {
  let shutdownError = null
  let preserveEvidence = !flowSucceeded
  if (desktopApp) {
    // Capture ownership while Playwright's dispatcher still exists. The public
    // process() accessor is no longer usable after a successful app.close().
    const ownedChild = desktopApp.process()
    const desktopLogPath = join(userDataDir, 'logs', 'desktop.log')
    const desktopLogCheckpoint = await readFile(desktopLogPath, 'utf8').catch(() => null)
    const shutdownStartedAt = Date.now()
    let shutdownDiagnostics = null
    const shutdown = await closeElectronWithDeadline({
      app: desktopApp,
      phase: 'window-background-final-shutdown',
      timeoutMs: ELECTRON_SHUTDOWN_TIMEOUT_MS,
      // The helper bounds this callback to 3 seconds. Do not use Electron IPC
      // here: the very process being diagnosed may no longer answer it.
      diagnostics: async () => {
        let handle
        try {
          handle = await open(desktopLogPath, 'r')
          const { size } = await handle.stat()
          const tailStart = Math.max(0, size - 256 * 1024)
          const buffer = Buffer.alloc(size - tailStart)
          const { bytesRead } = await handle.read(buffer, 0, buffer.length, tailStart)
          let tail = buffer.subarray(0, bytesRead).toString('utf8')
          if (tailStart > 0) tail = tail.slice(tail.indexOf('\n') + 1)
          const events = new Set([
            'before_quit', 'desktop_exit_phase', 'quit_gateway_shutdown_requested',
            'quit_gateway_exit', 'quit_gateway_drain_failed', 'quit_gateway_still_running',
            'quit_deferred_for_profile_writer', 'quit_deferred_for_update_drain',
          ])
          const phases = new Set(['running', 'deferred', 'draining', 'committed'])
          const reasons = new Set([
            'Windows session ending', 'desktop updater owns exit',
            'waiting for desktop update handoff', 'Gateway quit drain already in progress',
            'waiting for desktop writers', 'stopping lifecycle-owned Gateway',
            'all lifecycle-owned Gateways exited', 'Gateway quit drain failed safely',
            'no lifecycle-owned Gateway remains',
          ])
          const records = []
          let dropped = 0
          for (const line of tail.split('\n')) {
            let record
            try { record = JSON.parse(line) } catch { continue }
            if (!record || !events.has(record.event)) continue
            // Strict field/value allowlists: no log bodies, error messages,
            // credentials, ownership nonces, URLs, paths or arbitrary strings.
            const safe = { event: record.event }
            const at = typeof record.at === 'string' ? Date.parse(record.at) : NaN
            if (Number.isFinite(at)) safe.relativeToShutdownMs = at - shutdownStartedAt
            for (const key of ['exited', 'hardTerminated', 'accepted', 'alreadyStopping', 'gatewayDrainInFlight']) {
              if (typeof record[key] === 'boolean' || record[key] === null) safe[key] = record[key]
            }
            for (const key of ['from', 'to']) {
              if (phases.has(record[key])) safe[key] = record[key]
            }
            if (reasons.has(record.reason)) safe.reason = record.reason
            if (Number.isSafeInteger(record.activeWriters) && record.activeWriters >= 0) safe.activeWriters = record.activeWriters
            if (Array.isArray(record.pids)) safe.ownedProcessCount = record.pids.length
            if (records.length === 64) { records.shift(); dropped++ }
            records.push(safe)
          }
          shutdownDiagnostics = {
            logBytes: size,
            checkpointBytes: desktopLogCheckpoint === null ? null : Buffer.byteLength(desktopLogCheckpoint, 'utf8'),
            inspectedTailBytes: bytesRead,
            tailTruncated: tailStart > 0,
            checkpointPrefixMatches: desktopLogCheckpoint === null || tailStart > 0
              ? null : tail.startsWith(desktopLogCheckpoint),
            records,
            dropped,
          }
        } catch (error) {
          shutdownDiagnostics = {
            logReadFailed: true,
            errorCode: ['ENOENT', 'EACCES', 'EPERM', 'EBUSY', 'EIO'].includes(error?.code)
              ? error.code : 'OTHER',
          }
        } finally {
          await handle?.close().catch(() => {})
        }
        return shutdownDiagnostics
      },
    })
    shutdownError = shutdown.error
    preserveEvidence ||= Boolean(shutdown.error)
    let shutdownEvidence = null
    if (shutdown.error) {
      const desktopLog = await readFile(desktopLogPath, 'utf8').catch(() => null)
      shutdownEvidence = desktopShutdownEvidenceSince(desktopLogCheckpoint, desktopLog)
      if (canAcceptWindowsElectronShutdownFallback({
        shutdown,
        ...shutdownEvidence,
      })) {
        shutdownError = null
        console.warn(JSON.stringify({
          event: 'desktop_e2e_windows_shell_wrapper_reaped_after_commit',
          phase: 'window-background-final-shutdown',
        }))
      }
    }
    console.error(JSON.stringify({
      event: 'desktop_e2e_shutdown_outcome',
      phase: 'window-background-final-shutdown',
      flowSucceeded,
      elapsedMs: Date.now() - shutdownStartedAt,
      closed: shutdown.closed,
      forcedExitSucceeded: shutdown.forcedExitSucceeded,
      processTreeReaped: shutdown.processTreeReaped,
      strictFallbackAccepted: Boolean(shutdown.error) && shutdownError === null,
      childExited: ownedChild ? ownedChild.exitCode !== null || ownedChild.signalCode !== null : null,
      shutdownEvidence,
      shutdownDiagnostics,
    }))
  }
  if (preserveEvidence) {
    // Keep only this synthetic test profile for local diagnosis; never publish
    // its raw logs or profile contents. A failed teardown must not erase proof.
    console.error(JSON.stringify({
      event: 'desktop_e2e_evidence_preserved',
      phase: 'window-background-final-shutdown',
      isolationRoot,
      flowSucceeded,
      shutdownFailed: shutdownError !== null,
    }))
  } else {
    await rm(isolationRoot, { recursive: true, force: true }).catch(() => {})
  }
  if (flowSucceeded && shutdownError) throw shutdownError
}
