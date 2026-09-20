import { strict as assert } from 'node:assert'
import { mkdir, mkdtemp, open, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'
import {
  canAcceptWindowsElectronShutdownFallback,
  closeElectronWithDeadline,
  closeHttpServerWithDeadline,
  desktopShutdownEvidenceSince,
  trackHttpServerConnections,
} from './e2e-shutdown-helpers.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const ELECTRON_SHUTDOWN_TIMEOUT_MS = 15_000
const flowControl = process.argv.includes('--flow-control')
const connectionFaults = process.argv.includes('--connection-faults')
const idleSend = process.argv.includes('--idle-send')
const wakeBlackhole = process.argv.includes('--wake-blackhole')
const backgroundOption = process.argv.find(value => value.startsWith('--background-ms='))
const backgroundMs = backgroundOption ? Number(backgroundOption.split('=')[1]) : 65_000
if (!Number.isInteger(backgroundMs) || backgroundMs < 5_000 || backgroundMs > 600_000) {
  throw new Error('background-ms must be an integer between 5000 and 600000')
}
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
let syntheticProvider
let acceptedIdleSends = 0
let sentIdleMessages = 0
let idleSendReceipt = null
let hiddenIdleMs = null
let idleRecoveryMs = null
const idleSendIds = new Set()
const routedClients = new Set()
const routedServers = new WeakMap()
const blackholedClients = new Set()
let wakeBlackholeEvidence = null
let wakeBlackholeFailure = null

async function startSyntheticProvider() {
  let chatRequests = 0
  const server = createServer((request, response) => {
    request.resume()
    request.once('end', () => {
      if (request.method !== 'POST' || request.url !== '/api/chat') {
        response.writeHead(404)
        response.end()
        return
      }
      chatRequests++
      response.writeHead(200, { 'content-type': 'application/x-ndjson' })
      response.end(JSON.stringify({
        model: 'opensquilla-window-close-test-model',
        created_at: '2026-01-01T00:00:00Z',
        message: { role: 'assistant', content: 'Synthetic background recovery complete.' },
        done: true,
        done_reason: 'stop',
        prompt_eval_count: 8,
        eval_count: 3,
      }) + '\n')
    })
  })
  const sockets = trackHttpServerConnections(server)
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert.ok(address && typeof address === 'object')
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    chatRequests: () => chatRequests,
    close: () => closeHttpServerWithDeadline(server, sockets, {
      label: 'background-flow synthetic provider shutdown',
    }),
  }
}

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

async function readRpcTransportObservation(page) {
  return await page?.evaluate(() => {
    const phases = new Set([
      'connect_start', 'hello', 'challenge', 'first_successful_rpc', 'close',
      'watchdog_timeout', 'handshake_invalid', 'wake_incident_timeout',
      'wake_incident_start', 'wake_incident_recovered', 'retire',
      'probe_socket_unavailable', 'probe_deferred', 'probe_timeout',
      'scheduler_lag', 'reconnect_scheduled',
    ])
    const numericFields = [
      'at', 'generation', 'suspectAt', 'lastRxAt', 'wakeIncidentId',
      'wakeIncidentStartedAt', 'wakeIncidentDeadlineAt', 'wakeSignalCount',
      'roundTripMs', 'reconnectAttempt', 'recoveryMs', 'loopLagMs', 'maxLoopLagMs',
      'closeCode', 'delayMs',
    ]
    const enums = {
      health: ['healthy', 'suspect'],
      topology: ['loopback', 'remote', 'proxy/vpn', 'unknown'],
      visibility: ['visible', 'hidden', 'unknown'],
      wakeIncidentStatus: ['probing', 'suspect', 'reconnecting', 'recovered'],
    }
    let entries
    try { entries = JSON.parse(localStorage.getItem('opensquilla.chat.sessionNavigationDiag') || '[]') }
    catch { return { unreadable: true, records: [] } }
    if (!Array.isArray(entries)) return { unreadable: true, records: [] }
    const transport = entries.filter(entry => entry?.source === 'rpc.transport' && phases.has(entry.phase))
    const records = transport.slice(0, 96).reverse().map(entry => {
      const record = { phase: entry.phase }
      for (const field of numericFields) {
        if (typeof entry[field] === 'number' && Number.isFinite(entry[field])) record[field] = entry[field]
      }
      for (const [field, values] of Object.entries(enums)) {
        if (values.includes(entry[field])) record[field] = entry[field]
      }
      return record
    })
    return { records, dropped: Math.max(0, transport.length - records.length) }
  }).catch(() => ({ unavailable: true, records: [] }))
}

try {
  await mkdir(userDataDir, { recursive: true })
  await mkdir(isolatedHome, { recursive: true })
  await mkdir(isolatedRoaming, { recursive: true })
  await mkdir(isolatedLocal, { recursive: true })
  if (idleSend) syntheticProvider = await startSyntheticProvider()

  // Use a synthetic keyless profile so the lifecycle test reaches the Control
  // UI without reading developer credentials or requiring an external model.
  const now = new Date().toISOString()
  await writeFile(join(userDataDir, 'desktop-credential.json'), JSON.stringify({
    provider: 'ollama',
    model: 'opensquilla-window-close-test-model',
    baseUrl: syntheticProvider?.baseUrl || 'http://127.0.0.1:11434',
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
    disableNetworkObservability: idleSend,
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
      ...(idleSend ? {
        // Match the synthetic first-send gate's model budget so bundled tool
        // schemas do not exhaust Ollama's small fallback context before HTTP.
        OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS: '131072',
        NO_PROXY: '127.0.0.1,localhost,::1',
        no_proxy: '127.0.0.1,localhost,::1',
      } : {}),
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
      client.onMessage(message => {
        if (blackholedClients.has(client)) return
        try {
          const frame = JSON.parse(String(message))
          if (frame.type === 'req' && frame.method === 'chat.send') {
            sentIdleMessages++
            idleSendIds.add(frame.id)
          }
        } catch { /* Non-JSON frames remain transparent. */ }
        server.send(message)
      })
      server.onMessage(message => {
        if (blackholedClients.has(client)) return
        try {
          const frame = JSON.parse(String(message))
          if (frame?.policy?.transport_flow?.delivery_epoch) negotiatedFlow = true
          if (frame?.type === 'res' && idleSendIds.has(frame.id)) {
            idleSendIds.delete(frame.id)
            idleSendReceipt = { ok: frame.ok, accepted: frame.payload?.accepted }
            if (frame.ok === true && frame.payload?.accepted === true) acceptedIdleSends++
          }
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
  if (connectionFaults || idleSend || wakeBlackhole) {
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
  const draft = idleSend ? 'Synthetic background recovery request.' : 'isolated stability draft - never send'
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

  if (wakeBlackhole) {
    // Application-level frame fault against the source Gateway. This proves
    // native UI integration, not a kernel TCP blackhole or physical sleep.
    await waitFor(async () => routedClients.size === 1, 'one wake-fault connection')
    const acceptedBefore = acceptedSockets
    const precedingTransport = await readRpcTransportObservation(page)
    for (const client of routedClients) blackholedClients.add(client)
    const wakeStarted = Date.now()
    await page.evaluate(() => localStorage.removeItem('opensquilla.chat.sessionNavigationDiag'))
    await desktopApp.evaluate(({ powerMonitor }) => { powerMonitor.emit('resume') })
    const duplicateWake = setInterval(() => {
      void desktopApp.evaluate(({ powerMonitor }) => { powerMonitor.emit('resume') }).catch(() => {})
    }, 3_000)
    try {
      const readDiagnostics = () => page.evaluate(() => JSON.parse(
        localStorage.getItem('opensquilla.chat.sessionNavigationDiag') || '[]',
      ).filter(entry => entry.source === 'rpc.transport'))
      await waitFor(async () => (await readDiagnostics()).some(entry => entry.phase === 'probe_timeout'),
        'wake suspect diagnostic', 20_000)
      await waitFor(async () => await page.locator('[data-testid="connection-status"].connecting, [data-testid="chat-system-status-trigger"].connecting').count() > 0,
        'native suspect UI projection', 3_000)
      const suspectObservedMs = Date.now() - wakeStarted
      assert.equal(await composer.inputValue(), draft, 'suspect must preserve draft')
      await waitFor(async () => acceptedSockets > acceptedBefore
        && (await readDiagnostics()).some(entry => entry.phase === 'first_successful_rpc'),
      'wake replacement first successful RPC', 15_000)
      const timeline = await readDiagnostics()
      const incidentStart = timeline.find(entry => entry.phase === 'wake_incident_start')
      assert.ok(incidentStart, 'wake must start an incident')
      const incidentEntries = timeline.filter(entry => (
        entry.wakeIncidentId === incidentStart.wakeIncidentId
        && entry.generation === incidentStart.generation
      ))
      assert.equal(incidentEntries.filter(entry => entry.phase === 'wake_incident_start').length, 1,
        'duplicate native resume signals must share the first incident')
      const incidentEnd = incidentEntries.find(entry => entry.phase === 'wake_incident_timeout')
      assert.ok(incidentEnd, 'first wake incident must reach its timeout')
      assert.equal(incidentStart.topology, 'loopback')
      assert.equal(incidentEnd.wakeIncidentId, incidentStart.wakeIncidentId)
      assert.equal(incidentEnd.wakeIncidentDeadlineAt, incidentStart.wakeIncidentDeadlineAt)
      assert.ok(incidentEnd.wakeSignalCount > 1, 'duplicate resume signals must be counted')
      assert.equal(incidentEnd.reason, 'wake_incident_timeout')
      assert.equal(incidentEnd.health, 'suspect')
      assert.ok(Date.now() - wakeStarted < 25_000, 'wake recovery should fit the candidate budget plus handshake')
      assert.equal(page.url(), resumeUrl, 'wake recovery must not reload renderer')
      assert.equal(await composer.inputValue(), draft)
      assert.equal(await page.evaluate(() => window.__stabilityComposer === document.querySelector('.chat-textarea')), true)
      wakeBlackholeEvidence = {
        topology: 'loopback', faultLayer: 'application-frame-route', physicalSleep: false,
        suspectObservedMs, recoveredMs: Date.now() - wakeStarted,
        acceptedReplacements: acceptedSockets - acceptedBefore, timeline,
      }
    } catch (error) {
      // Capture before removing the fault so a late response cannot obscure
      // the failure. Only bounded timing/state fields leave the test profile.
      wakeBlackholeFailure = {
        elapsedMs: Date.now() - wakeStarted,
        acceptedBefore,
        acceptedSockets,
        routedConnectionCount: routedClients.size,
        precedingTransport,
        faultTransport: await readRpcTransportObservation(page),
      }
      throw error
    } finally {
      clearInterval(duplicateWake)
      blackholedClients.clear()
    }
  }

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
  assert.ok(!idleSend || backgroundSupported, 'idle-send requires native background-window support')
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

    if (idleSend) {
      // Exercise real native hide/activate and an interrupted transport. This
      // deliberately does not claim to suspend the physical host computer.
      outage = true
      for (const client of [...routedClients]) {
        routedClients.delete(client)
        await client.close({ code: 1013, reason: 'Synthetic hidden-window interruption' })
        await routedServers.get(client)?.close({ code: 1013, reason: 'Synthetic hidden-window interruption' })
      }
      const hiddenStarted = Date.now()
      console.log(JSON.stringify({ event: 'desktop_idle_send_phase', phase: 'hidden', backgroundMs }))
      await delay(backgroundMs)
      hiddenIdleMs = Date.now() - hiddenStarted
      assert.equal(await composer.inputValue(), draft, 'hidden idle must retain the draft')
      outage = false
    }

    const revealStarted = Date.now()
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

    if (idleSend) {
      const sendButton = page.locator('.chat-send-btn.btn--primary')
      await waitFor(async () => !await sendButton.isDisabled(), 'send after native background return', 30_000)
      idleRecoveryMs = Date.now() - revealStarted
      assert.equal(await composer.inputValue(), draft, 'native return must retain the unsent draft')
      assert.equal(await page.evaluate(() => window.__stabilityComposer === document.querySelector('.chat-textarea')), true)
      await sendButton.click()
      await waitFor(() => idleSendReceipt, 'real Gateway background-return admission')
      assert.deepEqual(idleSendReceipt, { ok: true, accepted: true }, 'real Gateway must admit the send')
      await waitFor(async () => (
        await page.locator('.msg-ai-text').last().textContent({ timeout: 1_000 })
      )?.includes('Synthetic background recovery complete.'), 'synthetic provider response')
      assert.equal(sentIdleMessages, 1, 'background return must submit the draft exactly once')
      assert.equal(syntheticProvider.chatRequests(), 1, 'accepted send must reach the local provider once')
      assert.equal(await composer.inputValue(), '', 'accepted send must clear the draft')
    }

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
      wakeBlackholeEvidence,
      connectionFaults,
      outageMs: connectionFaults ? outageMs : null,
      flowControl,
      negotiatedFlow,
      reconnectAttempts,
      warmRecoveryMs,
      continuityDiagnostics,
      idleSend,
      backgroundMs: idleSend ? backgroundMs : null,
      hiddenIdleMs,
      idleRecoveryMs,
      acceptedIdleSends,
      syntheticProviderRequests: syntheticProvider?.chatRequests() ?? null,
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
    sentIdleMessages,
    idleSendReceipt,
    syntheticProviderRequests: syntheticProvider?.chatRequests() ?? null,
    continuityDiagnostics,
    wakeBlackholeFailure,
    rpcTransport: await readRpcTransportObservation(continuityPage),
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
          try {
            await handle?.close()
          } catch (error) {
            // A diagnostic file-close failure must be visible, but must not
            // replace the original Electron shutdown outcome or expose paths.
            shutdownDiagnostics = {
              ...shutdownDiagnostics,
              logCloseFailed: true,
              closeErrorCode: ['ENOENT', 'EACCES', 'EPERM', 'EBUSY', 'EIO'].includes(error?.code)
                ? error.code : 'OTHER',
            }
          }
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
  await syntheticProvider?.close()
  if (flowSucceeded && shutdownError) throw shutdownError
}
