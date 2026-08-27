import assert from 'node:assert/strict'
import { basename, resolve } from 'node:path'

import {
  launchPackagedCandidate,
  requiredOption,
  waitFor,
} from './packaged-smoke-helpers.mjs'

const LONG_SESSION_MESSAGE_COUNT = 320
const TERMINAL_RECOVERY_TIMEOUT_MS = 35_000
const SESSION_RECOVERY_TIMEOUT_MS = 30_000

const executablePath = resolve(requiredOption('--executable'))
const userDataDir = resolve(requiredOption('--user-data-dir'))
const sessionKey = requiredOption('--session-key')
const switchSessionKey = requiredOption('--switch-session-key')
const label = requiredOption('--label')

if (!/^[A-Za-z0-9._-]{1,80}$/.test(label)) {
  throw new Error('Label must contain only ASCII letters, digits, dot, underscore, or dash')
}

const expectedLastMessage =
  `Synthetic retained history message ${String(LONG_SESSION_MESSAGE_COUNT).padStart(4, '0')} (${label})`
const preservedDraft = 'Synthetic draft preserved through packaged session recovery.'

let app
let injectHang = false
let socketCount = 0
let nextSocketIndex = 0
let healthyCloseCount = 0
const healthyNavigationSocketIds = new Set()
const healthySubscribeKeys = []
let heldHistoryRequests = 0
let heldSubscribeRequests = 0
let serverTickCount = 0

try {
  app = await launchPackagedCandidate({
    executablePath,
    userDataDir,
    model: 'opensquilla-release-session-recovery-smoke',
    env: {
      // A release preflight must exercise production deadlines, not the app's
      // ordinary testing shortcuts or mocked timer policy.
      GITHUB_ACTIONS: '0',
      OPENSQUILLA_TESTING: '0',
    },
  })
  await app.context().routeWebSocket(/\/ws$/, (client) => {
    const socketIndex = nextSocketIndex++
    let targetSocketCounted = false
    const countTargetSocket = () => {
      if (targetSocketCounted) return
      targetSocketCounted = true
      socketCount += 1
    }
    const server = client.connectToServer()

    client.onClose(() => {
      if (!injectHang) healthyCloseCount += 1
    })

    client.onMessage((message) => {
      try {
        const frame = JSON.parse(String(message))
        if (
          frame?.type === 'req'
          && frame.method === 'sessions.messages.subscribe'
          && !injectHang
          && [sessionKey, switchSessionKey].includes(frame.params?.key)
        ) {
          healthyNavigationSocketIds.add(socketIndex)
          healthySubscribeKeys.push(frame.params.key)
        }
        if (frame?.type === 'req' && injectHang) {
          if (
            frame.method === 'chat.history'
            && frame.params?.sessionKey === sessionKey
          ) {
            countTargetSocket()
            heldHistoryRequests += 1
            return
          }
          if (
            frame.method === 'sessions.messages.subscribe'
            && frame.params?.key === sessionKey
          ) {
            countTargetSocket()
            heldSubscribeRequests += 1
            return
          }
        }
      } catch {
        // Non-JSON protocol frames must remain byte-transparent.
      }
      try {
        server.send(message)
      } catch {
        // A deadline intentionally retires the socket; its peer can close
        // between the message callback and this forwarding attempt.
      }
    })

    server.onMessage((message) => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'event' && frame.event === 'tick') {
          serverTickCount += 1
        }
      } catch {
        // Non-JSON protocol frames must remain byte-transparent.
      }
      try {
        client.send(message)
      } catch {
        // The client can close while the real Gateway emits a final tick.
      }
    })
  })

  const page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(
    () => page.url().startsWith('opensquilla-app://desktop/chat'),
    'candidate Desktop renderer',
  )
  await waitFor(
    async () => (await page.evaluate(
      () => window.opensquillaDesktop?.getGatewayConnection?.(),
    ))?.status === 'ready',
    'candidate Desktop Gateway readiness',
  )
  // The preceding release-upgrade launch can persist this exact chat URL. In
  // that case page.goto() below may not create a new socket, so explicitly
  // reload after installing the context-wide route.
  await page.reload({ waitUntil: 'domcontentloaded' })

  const sessionUrl = new URL(page.url())
  sessionUrl.pathname = '/chat'
  sessionUrl.search = new URLSearchParams({ session: sessionKey }).toString()
  sessionUrl.hash = ''
  await page.goto(sessionUrl.toString(), { waitUntil: 'domcontentloaded' })

  const thread = page.locator('.chat-thread')
  const composer = page.locator('.chat-textarea')
  const sendButton = page.locator('.chat-send-btn.btn--primary')
  const recoveredMessage = page.getByText(expectedLastMessage, { exact: true }).first()
  const sessionRow = key => page.locator(`[data-session-key="${key}"]`)

  await waitFor(
    async () => await recoveredMessage.isVisible() && !await sendButton.isDisabled(),
    'the retained session to become live before healthy navigation',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  // The reload and initial route adoption above are setup, not part of the
  // navigation proof. Start the transport and subscription baselines only
  // after A is live so the assertions below cover exactly A -> B -> A2.
  const healthySocketCountBaseline = nextSocketIndex
  const healthyCloseCountBaseline = healthyCloseCount
  healthyNavigationSocketIds.clear()
  healthySubscribeKeys.length = 0
  await sessionRow(switchSessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => (
      new URL(page.url()).searchParams.get('session') === switchSessionKey
      && healthySubscribeKeys.includes(switchSessionKey)
      && !await sendButton.isDisabled()
    ),
    'the packaged client to switch to the synthetic peer session',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  await sessionRow(sessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => (
      new URL(page.url()).searchParams.get('session') === sessionKey
      && healthySubscribeKeys.filter(key => key === sessionKey).length === 1
      && await recoveredMessage.isVisible()
      && !await sendButton.isDisabled()
    ),
    'the packaged client to return on the original transport',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  assert.equal(
    healthyNavigationSocketIds.size,
    1,
    'healthy packaged session navigation must keep exactly one WebSocket',
  )
  assert.equal(
    nextSocketIndex,
    healthySocketCountBaseline,
    'healthy packaged session navigation must not create a replacement WebSocket',
  )
  assert.equal(
    healthyCloseCount,
    healthyCloseCountBaseline,
    'healthy packaged session navigation must not close the active WebSocket',
  )
  assert.deepEqual(
    healthySubscribeKeys,
    [switchSessionKey, sessionKey],
    'healthy packaged session navigation must subscribe B and then acquire a fresh A2 lease',
  )
  assert.equal(socketCount, 0, 'healthy navigation must not enter recovery')

  injectHang = true
  await sessionRow(switchSessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => (
      new URL(page.url()).searchParams.get('session') === switchSessionKey
      && !await sendButton.isDisabled()
    ),
    'the peer session before fault injection',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  await sessionRow(sessionKey).locator('.sidebar-history-item').click()
  await waitFor(
    async () => heldHistoryRequests > 0 && heldSubscribeRequests > 0,
    'packaged history and live requests to enter the injected hang',
  )
  await composer.waitFor({ state: 'visible', timeout: 30_000 })

  assert.equal(
    await page.locator('[data-testid="chat-session-load-state"]').count(),
    0,
    'packaged session recovery must never restore the removed blocking load page',
  )
  assert.equal(
    await page.locator(
      '[data-testid="chat-session-recovery-status"][data-recovery-state="history-loading"]',
    ).count(),
    0,
    'routine packaged history loading must not render a recovery notice',
  )
  assert.equal(
    await thread.getAttribute('aria-busy'),
    'false',
    'history recovery must not mark the complete conversation surface busy',
  )
  assert.equal(await composer.isEditable(), true, 'composer must stay editable during recovery')
  await composer.fill(preservedDraft)
  assert.equal(await composer.inputValue(), preservedDraft)
  assert.equal(
    await page.getByText(expectedLastMessage, { exact: true }).count(),
    0,
    'retained history must remain unavailable while its RPC is held',
  )

  const historyFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-error"]',
  )
  const liveFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="live-degraded"]',
  )
  const terminalStartedAt = Date.now()
  await waitFor(
    async () => await historyFailure.isVisible() && await liveFailure.isVisible(),
    'packaged session bootstrap to terminate',
    TERMINAL_RECOVERY_TIMEOUT_MS,
  )
  const terminalElapsedMs = Date.now() - terminalStartedAt

  assert.ok(
    terminalElapsedMs <= TERMINAL_RECOVERY_TIMEOUT_MS,
    `packaged recovery exceeded its terminal budget: ${terminalElapsedMs}ms`,
  )
  assert.ok(socketCount > 1, 'local bootstrap timeout must retire the blocked socket')
  assert.ok(heldHistoryRequests > 0, 'history hang was not exercised')
  assert.ok(heldSubscribeRequests > 0, 'live subscription hang was not exercised')
  assert.equal(await composer.isEditable(), true)
  assert.equal(await composer.inputValue(), preservedDraft)
  assert.equal(await sendButton.isDisabled(), true, 'live degraded state must fail closed')

  injectHang = false
  // These controls sit above the long transcript. A Playwright locator click
  // would scroll an off-screen retry into view and manufacture reader-owned
  // navigation to the top before activating it. Trigger the product action
  // in-page so this gate measures recovery of the existing live-edge lease.
  await historyFailure.locator('[data-testid="chat-session-recovery-retry"]')
    .evaluate((button) => button.click())
  await waitFor(
    () => recoveredMessage.isVisible(),
    'the retained long-session history to recover from the packaged Gateway',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  assert.equal(await historyFailure.count(), 0)

  if (await liveFailure.count()) {
    await liveFailure.locator('[data-testid="chat-session-recovery-retry"]')
      .evaluate((button) => button.click())
  }
  await waitFor(
    async () => await liveFailure.count() === 0 && !await sendButton.isDisabled(),
    'packaged live subscription to recover',
    SESSION_RECOVERY_TIMEOUT_MS,
  )

  assert.equal(await composer.inputValue(), preservedDraft)
  assert.equal(await recoveredMessage.isVisible(), true)
  assert.equal(await thread.getAttribute('aria-busy'), 'false')

  console.log(JSON.stringify({
    ok: true,
    executable: basename(executablePath),
    sessionKey,
    switchSessionKey,
    expectedLastMessage,
    healthyNavigationSocketCount: healthyNavigationSocketIds.size,
    heldHistoryRequests,
    heldSubscribeRequests,
    socketCount,
    serverTickCount,
    terminalElapsedMs,
  }, null, 2))
} finally {
  await app?.close().catch(() => {})
}
