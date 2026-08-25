import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-history-hydration'
const SESSION_A = 'agent:main:webchat:e2e-history-stale-a'
const SESSION_B = 'agent:main:webchat:e2e-history-stale-b'

function successResponse(id: string, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function replyToPing(
  ws: { send(message: string): void },
  frame: { type?: unknown },
): boolean {
  if (frame?.type !== 'ping') return false
  ws.send(JSON.stringify({ type: 'pong' }))
  return true
}

function helloResponse(tickIntervalMs: number) {
  return JSON.stringify({
    protocol: 3,
    policy: {
      tick_interval_ms: tickIntervalMs,
      concurrent_history_reads: true,
    },
  })
}

function basePayload(method: string): unknown {
  const payloads: Record<string, unknown> = {
    'agents.list': { agents: [] },
    'commands.list_for_surface': { commands: [] },
    'config.get': {
      squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
      permissions: {},
      skills: {},
    },
    'models.routing.get': { mode: 'direct' },
    'sessions.list': { sessions: [], has_more: false },
    'sessions.messages.subscribe': {
      subscribed: true,
      replay_complete: true,
      current_stream_seq: 0,
      run_status: 'idle',
    },
    'sessions.messages.hydrate': {
      subscribed: true,
      replay_complete: true,
      current_stream_seq: 0,
      run_status: 'idle',
    },
    'usage.status': { sessions: [] },
  }
  return payloads[method] ?? {}
}

function longHistoryMessages() {
  const now = Math.floor(Date.now() / 1000)
  return Array.from({ length: 50 }, (_, index) => ({
    role: index % 2 === 0 ? 'user' : 'assistant',
    text: index === 49
      ? 'Hydration complete.'
      : `History row ${index + 1}. ${'Deterministic long-session content. '.repeat(8)}`,
    id: `hydrated-message-${index + 1}`,
    timestamp: now - (50 - index) * 30,
  }))
}

async function stubApprovals(page: Page) {
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
}

test('keeps the conversation usable while startup and long history are delayed', async ({ page }) => {
  let releaseHistory: (() => void) | undefined
  let chatSendRequests = 0
  let usageRequests = 0
  const receivedMethods: string[] = []
  const sessionRequestOrder: string[] = []

  await page.setViewportSize({ width: 1280, height: 900 })
  await page.addInitScript(() => {
    const state = { emptySeen: false }
    Object.defineProperty(window, '__opensquillaHistoryHydrationTest', { value: state })
    const markEmpty = () => {
      if (document.querySelector('.chat-empty')) state.emptySeen = true
    }
    new MutationObserver(markEmpty).observe(document, { childList: true, subtree: true })
    markEmpty()
  })
  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(30000))
          return
        }
        const method = String(frame.method || '')
        receivedMethods.push(method)
        if (method.startsWith('sessions.messages.') || method === 'chat.history') {
          sessionRequestOrder.push(method)
        }
        if (method === 'chat.history') {
          releaseHistory = () => ws.send(successResponse(String(frame.id), {
            messages: longHistoryMessages(),
            has_more: true,
            oldest_cursor: 'cursor-50',
            newest_cursor: 'cursor-100',
            canonical_available: true,
            canonical_complete: true,
          }))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          ws.send(successResponse(String(frame.id), {
            key: SESSION_KEY,
            events: [],
            current_stream_seq: 0,
          }))
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(successResponse(String(frame.id), {
            subscribed: true,
            hydration_complete: false,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          }))
          return
        }
        if (method === 'sessions.messages.hydrate') {
          ws.send(successResponse(String(frame.id), {
            hydration_complete: true,
            workspaceId: null,
            run_status: 'idle',
          }))
          return
        }
        if (method === 'sessions.list') {
          ws.send(successResponse(String(frame.id), {
            sessions: [{
              key: SESSION_KEY,
              title: 'Visible while history is pending',
              sessionKind: 'chat',
              surface: 'webchat',
              conversationKind: 'direct',
              effectiveAgentId: 'main',
              updatedAt: 100,
              messageCount: 50,
              status: 'ok',
              runStatus: 'idle',
            }],
            has_more: false,
          }))
          return
        }
        if (method === 'chat.send') {
          chatSendRequests += 1
          ws.send(successResponse(String(frame.id), {
            sessionKey: SESSION_KEY,
            status: 'accepted',
            task_id: 'e2e-history-interactive-send',
            message_id: 'e2e-history-interactive-message',
          }))
          return
        }
        if (method === 'usage.status') usageRequests += 1
        ws.send(successResponse(String(frame.id), basePayload(method)))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  const hiddenRecovery = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-loading"]',
  )
  const thread = page.locator('.chat-thread')
  const composer = page.getByRole('textbox', { name: 'Message to send' })

  await expect.poll(() => Boolean(releaseHistory)).toBe(true)
  const criticalStartupOrder = [
    'sessions.messages.subscribe',
    'sessions.messages.snapshot',
    'chat.history',
  ]
  expect(receivedMethods.slice(0, criticalStartupOrder.length)).toEqual(
    criticalStartupOrder,
  )
  expect(sessionRequestOrder.slice(0, criticalStartupOrder.length)).toEqual(
    criticalStartupOrder,
  )
  // Critical ordering is preserved, but independent UI data starts as soon as
  // those frames are queued instead of waiting for the history response.
  for (const optionalMethod of [
    'onboarding.status',
    'sessions.subscribe',
    'sessions.list',
    'agents.list',
    'config.get',
    'commands.list_for_surface',
    'usage.status',
  ]) {
    await expect.poll(() => receivedMethods).toContain(optionalMethod)
  }
  await expect.poll(() => receivedMethods).toContain('sessions.messages.hydrate')
  await expect.poll(() => usageRequests).toBeGreaterThan(0)
  await expect(hiddenRecovery).toHaveCount(0)
  await expect(page.getByTestId('chat-session-load-state')).toHaveCount(0)
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(
    page.locator('.sidebar-history-row[data-session-key]')
      .filter({ hasText: 'Visible while history is pending' }),
  ).toBeVisible()
  await expect(composer).toBeEditable()
  await composer.fill('Draft remains editable while history loads.')
  await expect(composer).toHaveValue('Draft remains editable while history loads.')
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled()
  await expect(page.locator('.chat-empty')).toHaveCount(0)
  const themeButton = page.getByRole('button', { name: 'Theme', exact: true })
  await themeButton.click()
  await expect(page.getByRole('menu', { name: 'Theme' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('menu', { name: 'Theme' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect.poll(() => chatSendRequests).toBe(1)

  releaseHistory?.()
  await expect(page.getByText('Hydration complete.')).toBeVisible()
  await expect(hiddenRecovery).toHaveCount(0)
  await expect(page.getByTestId('history-load-sentinel')).toBeAttached()
  await expect(composer).toHaveValue('')
  await expect.poll(() => page.evaluate(() => {
    const state = (window as unknown as {
      __opensquillaHistoryHydrationTest?: { emptySeen?: boolean }
    }).__opensquillaHistoryHydrationTest
    return state?.emptySeen ?? true
  })).toBe(false)
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  ))).toBe(true)
})

test('recovers from stuck automatic metadata before sending', async ({ page }) => {
  let socketCount = 0
  let heldConfigRequests = 0
  let chatSendSocket = 0

  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    const socketNumber = ++socketCount
    let metadataStuck = false
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(helloResponse(30000))
          return
        }
        if (socketNumber === 1 && method === 'config.get') {
          heldConfigRequests += 1
          metadataStuck = true
          return
        }
        // Model the Gateway's serial dispatcher: once the optional read is
        // stuck, every later request on this socket remains queued behind it.
        if (socketNumber === 1 && metadataStuck) return
        if (method === 'sessions.messages.snapshot') {
          ws.send(successResponse(String(frame.id), {
            key: SESSION_KEY,
            events: [],
            current_stream_seq: 0,
          }))
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(successResponse(String(frame.id), {
            subscribed: true,
            hydration_complete: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          }))
          return
        }
        if (method === 'sessions.messages.hydrate') {
          ws.send(successResponse(String(frame.id), {
            hydration_complete: true,
            run_status: 'idle',
          }))
          return
        }
        if (method === 'chat.history') {
          ws.send(successResponse(String(frame.id), {
            messages: [],
            has_more: false,
            oldest_cursor: null,
            newest_cursor: null,
            canonical_available: true,
            canonical_complete: true,
          }))
          return
        }
        if (method === 'chat.send') {
          chatSendSocket = socketNumber
          ws.send(successResponse(String(frame.id), {
            sessionKey: SESSION_KEY,
            status: 'accepted',
            task_id: 'e2e-after-metadata-reconnect',
            message_id: 'e2e-after-metadata-reconnect-message',
          }))
          return
        }
        ws.send(successResponse(String(frame.id), basePayload(method)))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect.poll(() => heldConfigRequests).toBeGreaterThan(0)
  // The optional metadata budget is 10 seconds; leave time for the timeout
  // handler to retire the stuck socket and finish the replacement handshake.
  await expect.poll(() => socketCount, { timeout: 15_000 }).toBeGreaterThan(1)

  const composer = page.getByRole('textbox', { name: 'Message to send' })
  const send = page.getByRole('button', { name: 'Send', exact: true })
  await expect(composer).toBeEditable()
  await composer.fill('Send after automatic metadata recovery.')
  await expect(send).toBeEnabled()
  await send.click()

  await expect.poll(() => chatSendSocket).toBeGreaterThan(1)
  await expect(composer).toHaveValue('')
})

test('shows a recoverable initial failure and retries it', async ({ page }) => {
  let historyRequests = 0
  let releaseRetry: (() => void) | undefined

  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(30000))
          return
        }
        if (frame.method === 'config.get') {
          ws.send(successResponse(String(frame.id), {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          }))
          return
        }
        if (frame.method === 'chat.history') {
          historyRequests += 1
          if (historyRequests <= 2) {
            ws.send(JSON.stringify({
              type: 'res',
              id: String(frame.id),
              ok: false,
              error: { code: 'HISTORY_UNAVAILABLE', message: 'offline', retryable: true },
            }))
          } else {
            releaseRetry = () => ws.send(successResponse(String(frame.id), {
              messages: [{
                role: 'assistant',
                text: 'History recovered after retry.',
                id: 'history-recovered',
                timestamp: Math.floor(Date.now() / 1000),
              }],
              has_more: false,
              canonical_available: true,
              canonical_complete: true,
            }))
          }
          return
        }
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  const loadState = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-error"]',
  )
  const retry = loadState.getByTestId('chat-session-recovery-retry')
  const thread = page.locator('.chat-thread')
  const composer = page.getByRole('textbox', { name: 'Message to send' })

  await expect(loadState).toContainText('Conversation history temporarily unavailable')
  await expect(loadState).toContainText(
    'The connection may have been interrupted, or history is temporarily unavailable.',
  )
  await expect(loadState).toHaveAttribute('role', 'alert')
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(composer).toBeEditable()
  await expect(page.locator('.chat-empty')).toHaveCount(0)

  await retry.click()
  await expect.poll(() => Boolean(releaseRetry)).toBe(true)
  const retrying = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-retrying"]',
  )
  await expect(retrying).toContainText('Reloading conversation history…')
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(thread).toBeFocused()

  releaseRetry?.()
  await expect(page.getByText('History recovered after retry.')).toBeVisible()
  await expect(retrying).toHaveCount(0)
  expect(historyRequests).toBe(3)
})

test('terminates stalled history and live hydration despite ongoing ticks, then recovers on a new socket', async ({ page }) => {
  test.setTimeout(30_000)

  let allowRecovery = false
  let socketCount = 0
  let tickCount = 0
  let heldHistoryRequests = 0
  let heldSubscribeRequests = 0
  let recoveredHistorySocket = 0
  let recoveredSubscribeSocket = 0
  const tickSenders: Array<() => void> = []

  await page.clock.install({ time: new Date('2026-07-28T00:00:00Z') })
  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    const socketId = ++socketCount
    let tickSeq = 0
    const sendTick = () => {
      try {
        ws.send(JSON.stringify({
          type: 'event',
          event: 'tick',
          seq: ++tickSeq,
          payload: { socket_id: socketId },
        }))
        tickCount += 1
      } catch {
        // A timed-out socket is intentionally retired while its replacement
        // continues the same fault-injection scenario.
      }
    }
    sendTick()
    tickSenders.push(sendTick)

    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(1000))
          return
        }
        if (frame.method === 'sessions.messages.snapshot') {
          ws.send(successResponse(String(frame.id), {
            key: SESSION_KEY,
            events: [],
            current_stream_seq: 0,
          }))
          return
        }
        if (frame.method === 'chat.history') {
          if (!allowRecovery) {
            heldHistoryRequests += 1
            return
          }
          recoveredHistorySocket = socketId
          ws.send(successResponse(String(frame.id), {
            messages: [{
              role: 'assistant',
              text: 'History recovered on a fresh connection.',
              id: 'history-recovered-fresh-socket',
              timestamp: Math.floor(Date.now() / 1000),
            }],
            has_more: false,
            oldest_cursor: null,
            newest_cursor: 'history-recovered-fresh-socket',
            canonical_available: true,
            canonical_complete: true,
          }))
          return
        }
        if (frame.method === 'sessions.messages.subscribe') {
          if (!allowRecovery) {
            heldSubscribeRequests += 1
            return
          }
          recoveredSubscribeSocket = socketId
          ws.send(successResponse(String(frame.id), {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          }))
          return
        }
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect.poll(() => heldHistoryRequests).toBeGreaterThan(0)
  await expect.poll(() => heldSubscribeRequests).toBeGreaterThan(0)

  const thread = page.locator('.chat-thread')
  // Keep this recovery proof locale-independent: browser locale follows the
  // host on developer machines, so accessible names are not always English.
  const composer = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn')

  await expect(page.getByTestId('chat-session-load-state')).toHaveCount(0)
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(composer).toBeEditable()
  await composer.fill('Keep this draft through timeout and reconnect.')
  await expect(composer).toHaveValue('Keep this draft through timeout and reconnect.')

  // Advance past the 15-second aggregate bootstrap budget one second at a
  // time, delivering a server tick after each increment. This models a socket
  // that remains healthy while individual RPCs never produce responses.
  // Leave enough headroom for the RPC round-trip between reading the mocked
  // clock and pausing it. A 1 ms target can already be in the past on a busy
  // hosted runner, which makes Playwright reject the retry before exercising
  // the recovery contract.
  await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1_000)
  for (let elapsed = 0; elapsed < 16_000; elapsed += 1000) {
    await page.clock.runFor(1000)
    tickSenders.forEach(sendTick => sendTick())
  }
  expect(tickCount).toBeGreaterThan(15)

  const historyFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-error"]',
  )
  await expect(historyFailure).toBeVisible()
  await expect(historyFailure).toHaveAttribute('role', 'alert')
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(composer).toBeEditable()
  await expect(composer).toHaveValue('Keep this draft through timeout and reconnect.')
  await expect(send).toBeDisabled()
  expect(socketCount).toBeGreaterThan(1)

  allowRecovery = true
  await historyFailure.getByTestId('chat-session-recovery-retry').click()
  await expect(page.getByText('History recovered on a fresh connection.')).toBeVisible()
  expect(recoveredHistorySocket).toBeGreaterThan(1)
  await expect(composer).toHaveValue('Keep this draft through timeout and reconnect.')

  const liveFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="live-degraded"]',
  )
  // The page clock is paused for this deterministic timeout scenario. Keep
  // advancing it after the history retry so an in-flight subscribe on the
  // recycled socket can either recover or reach its bounded degraded state.
  for (let elapsed = 0; elapsed < 16_000 && !await send.isEnabled(); elapsed += 1000) {
    await page.clock.runFor(1000)
    tickSenders.forEach(sendTick => sendTick())
  }
  // Depending on whether the replacement socket connected before or after
  // recovery was allowed, the live phase may already be ready or may expose
  // its explicit retry control. Both paths must converge without losing the
  // recovered history or composer draft.
  await expect.poll(async () => (
    (await send.isEnabled()) || (await liveFailure.isVisible())
  )).toBe(true)
  if (await liveFailure.isVisible()) {
    await liveFailure.getByTestId('chat-session-recovery-retry').click()
  }
  await expect.poll(() => recoveredSubscribeSocket).toBeGreaterThan(1)
  await expect(liveFailure).toHaveCount(0)
  await expect(send).toBeEnabled()
  await expect(composer).toHaveValue('Keep this draft through timeout and reconnect.')
})

test('preserves a Sessions Hub auto-send draft when live recovery terminates', async ({ page }) => {
  test.setTimeout(30_000)

  let allowSubscription = false
  let heldSubscribeRequests = 0
  let chatSendRequests = 0
  const tickSenders: Array<() => void> = []

  await page.clock.install({ time: new Date('2026-07-28T00:00:00Z') })
  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    let tickSeq = 0
    const sendTick = () => {
      try {
        ws.send(JSON.stringify({
          type: 'event',
          event: 'tick',
          seq: ++tickSeq,
          payload: {},
        }))
      } catch {}
    }
    sendTick()
    tickSenders.push(sendTick)
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(1000))
          return
        }
        if (frame.method === 'sessions.messages.snapshot') {
          ws.send(successResponse(String(frame.id), {
            key: String(frame.params?.key || ''),
            events: [],
            current_stream_seq: 0,
          }))
          return
        }
        if (frame.method === 'sessions.messages.subscribe') {
          if (!allowSubscription) {
            heldSubscribeRequests += 1
            return
          }
          ws.send(successResponse(String(frame.id), {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          }))
          return
        }
        if (frame.method === 'chat.send') {
          chatSendRequests += 1
          ws.send(successResponse(String(frame.id), {
            sessionKey: String(frame.params?.sessionKey || ''),
            task_id: 'unexpected-auto-send',
          }))
          return
        }
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'sessions')
  const taskText = 'Keep this Sessions Hub draft until live updates recover.'
  await page.locator('.hub-task__input').fill(taskText)
  await page.getByRole('button', { name: 'Start task' }).click()
  await expect(page).toHaveURL(/\/chat\/new/)
  await expect.poll(() => heldSubscribeRequests).toBeGreaterThan(0)

  const composer = page.getByRole('textbox', { name: 'Message to send' })
  const send = page.getByRole('button', { name: 'Send', exact: true })
  await expect(composer).toHaveValue(taskText)
  await expect(composer).toBeEditable()
  await expect(send).toBeDisabled()

  await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1_000)
  const liveFailure = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="live-degraded"]',
  )
  // A replacement socket can begin one fresh bounded live-recovery budget
  // after the original subscribe stalls. Advance through both budgets instead
  // of assuming the first 16 seconds always lands on the terminal frame.
  for (let elapsed = 0; elapsed < 40_000 && !await liveFailure.isVisible(); elapsed += 1000) {
    await page.clock.runFor(1000)
    tickSenders.forEach(sendTick => sendTick())
  }

  await expect(liveFailure).toBeVisible()
  await expect(composer).toHaveValue(taskText)
  await composer.press('Enter')
  await expect(composer).toHaveValue(taskText)
  expect(chatSendRequests).toBe(0)

  allowSubscription = true
  await liveFailure.getByTestId('chat-session-recovery-retry').click()
  await expect(liveFailure).toHaveCount(0)
  await expect(send).toBeEnabled()
  await expect(composer).toHaveValue(taskText)
  expect(chatSendRequests).toBe(0)
})

test('cancels delayed auto-send when the user edits the draft before live is ready', async ({ page }) => {
  let releaseSubscription: (() => void) | undefined
  let chatSendRequests = 0

  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(30000))
          return
        }
        if (frame.method === 'sessions.messages.snapshot') {
          ws.send(successResponse(String(frame.id), {
            key: String(frame.params?.key || ''),
            events: [],
            current_stream_seq: 0,
          }))
          return
        }
        if (frame.method === 'sessions.messages.subscribe') {
          releaseSubscription = () => ws.send(successResponse(String(frame.id), {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          }))
          return
        }
        if (frame.method === 'chat.send') {
          chatSendRequests += 1
          ws.send(successResponse(String(frame.id), {
            sessionKey: String(frame.params?.sessionKey || ''),
            task_id: 'must-not-send-edited-draft',
          }))
          return
        }
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'sessions')
  const original = 'Original Sessions Hub task.'
  const edited = 'User-edited draft must remain unsent.'
  await page.locator('.hub-task__input').fill(original)
  await page.getByRole('button', { name: 'Start task' }).click()
  await expect(page).toHaveURL(/\/chat\/new/)
  await expect.poll(() => Boolean(releaseSubscription)).toBe(true)

  const composer = page.getByRole('textbox', { name: 'Message to send' })
  await expect(composer).toHaveValue(original)
  await composer.fill(edited)
  releaseSubscription?.()

  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled()
  await expect(composer).toHaveValue(edited)
  expect(chatSendRequests).toBe(0)
})

test('keeps loaded messages visible when an earlier page fails and retries inline', async ({ page }) => {
  let historyRequests = 0
  let releaseEarlierRetry: (() => void) | undefined

  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(30000))
          return
        }
        if (frame.method === 'config.get') {
          ws.send(successResponse(String(frame.id), {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          }))
          return
        }
        if (frame.method === 'chat.history') {
          historyRequests += 1
          if (historyRequests === 1) {
            ws.send(successResponse(String(frame.id), {
              messages: longHistoryMessages(),
              has_more: true,
              oldest_cursor: 'cursor-50',
              newest_cursor: 'cursor-100',
              canonical_available: true,
              canonical_complete: true,
            }))
          } else if (historyRequests === 2) {
            ws.send(JSON.stringify({
              type: 'res',
              id: String(frame.id),
              ok: false,
              error: { code: 'HISTORY_UNAVAILABLE', message: 'offline', retryable: true },
            }))
          } else {
            releaseEarlierRetry = () => ws.send(successResponse(String(frame.id), {
              messages: [{
                role: 'assistant',
                text: 'Earlier page recovered.',
                id: 'earlier-message',
                timestamp: Math.floor(Date.now() / 1000) - 3600,
              }],
              has_more: false,
              oldest_cursor: null,
              newest_cursor: 'cursor-50',
              canonical_available: true,
              canonical_complete: true,
            }))
          }
          return
        }
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  const thread = page.locator('.chat-thread')
  const loadState = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="history-error"]',
  )

  await expect(page.getByText('Hydration complete.')).toBeVisible()
  await thread.evaluate(element => element.scrollTo({ top: 0 }))
  await expect.poll(() => historyRequests).toBe(2)

  const retry = page.getByTestId('history-load-retry')
  await expect(retry).toContainText('Earlier messages failed to load · Retry')
  await expect(loadState).toHaveCount(0)
  await expect(page.getByText(/History row 1\./).first()).toBeVisible()

  await retry.click()
  await expect.poll(() => Boolean(releaseEarlierRetry)).toBe(true)
  await expect(page.getByText('Loading earlier messages…')).toBeVisible()
  await expect(thread).toBeFocused()

  releaseEarlierRetry?.()
  await expect(page.getByText('Earlier page recovered.')).toBeVisible()
  await expect(retry).toHaveCount(0)
  expect(historyRequests).toBe(3)
})

test('ignores a late history response after navigating to another session', async ({ page }) => {
  let releaseOldHistory: (() => void) | undefined

  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (replyToPing(ws, frame)) return
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloResponse(30000))
          return
        }
        if (frame.method === 'sessions.list') {
          ws.send(successResponse(String(frame.id), {
            sessions: [
              {
                key: SESSION_B,
                title: 'History Session B',
                sessionKind: 'chat',
                surface: 'webchat',
                conversationKind: 'direct',
                effectiveAgentId: 'main',
                updatedAt: 200,
                messageCount: 1,
                status: 'ok',
                runStatus: 'idle',
              },
              {
                key: SESSION_A,
                title: 'History Session A',
                sessionKind: 'chat',
                surface: 'webchat',
                conversationKind: 'direct',
                effectiveAgentId: 'main',
                updatedAt: 100,
                messageCount: 1,
                status: 'ok',
                runStatus: 'idle',
              },
            ],
            has_more: false,
          }))
          return
        }
        if (frame.method === 'sessions.messages.snapshot') {
          ws.send(successResponse(String(frame.id), {
            key: String(frame.params?.key || ''),
            events: [],
            current_stream_seq: 0,
          }))
          return
        }
        if (frame.method === 'chat.history') {
          const requestedSession = String(frame.params?.sessionKey || '')
          if (requestedSession === SESSION_A) {
            releaseOldHistory = () => ws.send(successResponse(String(frame.id), {
              messages: [{
                role: 'assistant',
                text: 'Late history from session A must stay hidden.',
                id: 'late-session-a-history',
                timestamp: Math.floor(Date.now() / 1000) - 60,
              }],
              has_more: false,
              canonical_available: true,
              canonical_complete: true,
            }))
            return
          }
          if (requestedSession === SESSION_B) {
            ws.send(successResponse(String(frame.id), {
              messages: [{
                role: 'assistant',
                text: 'Current history from session B.',
                id: 'current-session-b-history',
                timestamp: Math.floor(Date.now() / 1000),
              }],
              has_more: false,
              canonical_available: true,
              canonical_complete: true,
            }))
            return
          }
        }
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_A))
  await expect.poll(() => Boolean(releaseOldHistory)).toBe(true)

  await page
    .locator('.sidebar-history-row[data-family="chats"]')
    .filter({ hasText: 'History Session B' })
    .locator('.sidebar-history-item')
    .click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expect(page.getByText('Current history from session B.')).toBeVisible()

  releaseOldHistory?.()
  await page.waitForTimeout(100)

  await expect(page.getByText('Current history from session B.')).toBeVisible()
  await expect(page.getByText('Late history from session A must stay hidden.')).toHaveCount(0)
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
})
