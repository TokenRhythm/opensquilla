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
const label = requiredOption('--label')

if (!/^[A-Za-z0-9._-]{1,80}$/.test(label)) {
  throw new Error('Label must contain only ASCII letters, digits, dot, underscore, or dash')
}

const expectedLastMessage =
  `Synthetic retained history message ${String(LONG_SESSION_MESSAGE_COUNT).padStart(4, '0')} (${label})`
const preservedDraft = 'Synthetic draft preserved through packaged session recovery.'

let app
let injectHang = true
let socketCount = 0
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
  const page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(() => page.url().includes('/control/chat'), 'candidate Control UI')

  await page.routeWebSocket(/\/ws$/, (client) => {
    socketCount += 1
    const server = client.connectToServer()

    client.onMessage((message) => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'req' && injectHang) {
          if (frame.method === 'chat.history') {
            heldHistoryRequests += 1
            return
          }
          if (frame.method === 'sessions.messages.subscribe') {
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

  const sessionUrl = new URL(page.url())
  sessionUrl.pathname = '/control/chat'
  sessionUrl.search = new URLSearchParams({ session: sessionKey }).toString()
  sessionUrl.hash = ''
  await page.goto(sessionUrl.toString(), { waitUntil: 'domcontentloaded' })

  const thread = page.locator('.chat-thread')
  const composer = page.locator('.chat-textarea')
  const sendButton = page.locator('.chat-send-btn.btn--primary')
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
    ).isVisible(),
    true,
    'packaged history recovery must use the compact non-blocking status',
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
  const recoveredMessage = page.getByText(expectedLastMessage, { exact: true }).first()
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
  await historyFailure.locator('[data-testid="chat-session-recovery-retry"]').click()
  await waitFor(
    () => recoveredMessage.isVisible(),
    'the retained long-session history to recover from the packaged Gateway',
    SESSION_RECOVERY_TIMEOUT_MS,
  )
  assert.equal(await historyFailure.count(), 0)

  if (await liveFailure.count()) {
    await liveFailure.locator('[data-testid="chat-session-recovery-retry"]').click()
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
    expectedLastMessage,
    heldHistoryRequests,
    heldSubscribeRequests,
    socketCount,
    serverTickCount,
    terminalElapsedMs,
  }, null, 2))
} finally {
  await app?.close().catch(() => {})
}
