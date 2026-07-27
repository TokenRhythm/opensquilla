import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-history-hydration'

function successResponse(id: string, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function basePayload(method: string): unknown {
  const payloads: Record<string, unknown> = {
    'agents.list': { agents: [] },
    'commands.list_for_surface': { commands: [] },
    'models.routing.get': { mode: 'direct' },
    'sessions.list': { sessions: [], has_more: false },
    'sessions.messages.subscribe': {
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

test('shows hydration from the first frame while startup and long history are delayed', async ({ page }) => {
  let releaseConfig: (() => void) | undefined
  let releaseHistory: (() => void) | undefined

  await page.setViewportSize({ width: 390, height: 844 })
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
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        if (frame.method === 'config.get') {
          releaseConfig = () => ws.send(successResponse(String(frame.id), {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          }))
          return
        }
        if (frame.method === 'chat.history') {
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
        ws.send(successResponse(String(frame.id), basePayload(String(frame.method))))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  const loadState = page.getByTestId('chat-session-load-state')
  const loadAnnouncer = page.getByTestId('chat-session-load-announcer')
  const thread = page.locator('.chat-thread')

  await expect.poll(() => Boolean(releaseConfig)).toBe(true)
  // History starts in parallel with feature configuration instead of waiting
  // behind it; both responses remain held so the visible pending state is
  // deterministic.
  await expect.poll(() => Boolean(releaseHistory)).toBe(true)
  await expect(loadState).toContainText('Loading conversation…')
  await expect(loadState).toContainText('Restoring recent messages and session state.')
  await expect(loadState).not.toHaveAttribute('role', 'status')
  await expect(loadAnnouncer).toContainText('Loading conversation…')
  await expect(loadAnnouncer).toHaveAttribute('role', 'status')
  await expect(thread.getByTestId('chat-session-load-announcer')).toHaveCount(0)
  await expect(thread).toHaveAttribute('aria-busy', 'true')
  await expect(page.locator('.chat-empty')).toHaveCount(0)
  await expect(page.getByTestId('history-load-sentinel')).toHaveCount(0)

  // The session becomes usable while the unrelated config response is still
  // held, proving configuration no longer gates history hydration.
  releaseHistory?.()
  await expect(page.getByText('Hydration complete.')).toBeVisible()
  await expect(loadState).toHaveCount(0)
  await expect(loadAnnouncer).toHaveText('')
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(page.getByTestId('history-load-sentinel')).toBeAttached()
  releaseConfig?.()
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

test('shows a recoverable initial failure and retries it', async ({ page }) => {
  let historyRequests = 0
  let releaseRetry: (() => void) | undefined

  await stubApprovals(page)
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
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
  const loadState = page.getByTestId('chat-session-load-state')
  const retry = page.getByTestId('chat-session-load-retry')
  const thread = page.locator('.chat-thread')

  await expect(loadState).toContainText('Conversation temporarily unavailable')
  await expect(loadState).toContainText(
    'The connection may have been interrupted, or history is temporarily unavailable.',
  )
  await expect(loadState).toHaveAttribute('role', 'alert')
  await expect(thread).toHaveAttribute('aria-busy', 'false')
  await expect(page.locator('.chat-empty')).toHaveCount(0)
  await expect(page.getByTestId('history-load-sentinel')).toHaveCount(0)

  await retry.click()
  await expect.poll(() => Boolean(releaseRetry)).toBe(true)
  await expect(loadState).toContainText('Reloading conversation')
  await expect(loadState).toContainText('Restoring conversation history…')
  await expect(page.getByTestId('chat-session-load-retrying')).toBeDisabled()
  await expect(thread).toHaveAttribute('aria-busy', 'true')
  await expect(thread).toBeFocused()

  releaseRetry?.()
  await expect(page.getByText('History recovered after retry.')).toBeVisible()
  await expect(loadState).toHaveCount(0)
  expect(historyRequests).toBe(2)
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
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
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
  const loadState = page.getByTestId('chat-session-load-state')

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
