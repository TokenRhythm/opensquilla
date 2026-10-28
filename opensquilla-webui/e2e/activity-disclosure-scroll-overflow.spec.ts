import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2e-activity-overflow'
const OTHER_SESSION_KEY = 'agent:main:webchat:e2e-other-activity'
const TURN_ID = 'turn-activity-overflow'
const BASE_TIME = 1_800_000_000_000
const LAST_MESSAGE = '[data-chat-message-key="overflow-answer"]'
const NARRATION = Array.from({ length: 14 }, (_, index) =>
  `Progress paragraph ${index + 1}: inspected the synthetic artifact and checked its contents.`,
).join('\n\n')
const ANSWER = 'The synthetic artifact is ready.\n\n' + 'This is the final visible answer. '.repeat(12)

function completedHistory() {
  return chatHistoryPayload([
    // A scrollable, non-virtualized transcript distinguishes leaked overflow
    // from legitimate empty space below a short conversation or virtual padding.
    ...Array.from({ length: 8 }, (_, index) => ({
      role: index % 2 ? 'assistant' : 'user',
      id: `previous-${index}`,
      timestamp: (BASE_TIME - 100_000 + index * 1_000) / 1_000,
      text: index % 2 ? 'Synthetic history. '.repeat(40) : `Previous question ${index}`,
    })),
    {
      role: 'user', id: 'overflow-user', text: 'Create a synthetic artifact.',
      timestamp: (BASE_TIME - 1_000) / 1_000,
      turn_context: { turn_id: TURN_ID },
    },
    {
      role: 'assistant', id: 'overflow-answer',
      timestamp: (BASE_TIME + 27_000) / 1_000,
      turn_context: { turn_id: TURN_ID },
      // Persist the canonical aggregate as the backend does. The activity
      // snapshot separates the intermediate narration from the final answer.
      text: `${NARRATION}\n\n${ANSWER}`,
      tool_calls: [
        { type: 'text', text: NARRATION, presentation: 'intermediate' },
        { type: 'text', text: ANSWER, presentation: 'answer' },
      ],
    },
  ], {
    turn_outcomes: [{
      turn_id: TURN_ID, task_id: TURN_ID, status: 'succeeded',
      outcome: { kind: 'completed' }, started_at: BASE_TIME, finished_at: BASE_TIME + 27_000,
      activity_snapshot: {
        version: 2, task_id: TURN_ID, turn_id: TURN_ID, complete: true,
        reasoning_utf16_length: 0,
        entries: [
          {
            type: 'segment', id: 'text:0', order: 1, segment_type: 'text',
            text_index: 0, text_utf16_length: NARRATION.length,
            at: BASE_TIME, ended_at: BASE_TIME + 25_000,
          },
          {
            type: 'phase', id: 'provider:retry_wait:1', order: 2,
            kind: 'provider', phase: 'retry_wait', reason: 'rate_limited',
            http_status: 429, retry_after_ms: 1_000,
            at: BASE_TIME + 25_000, ended_at: BASE_TIME + 26_000,
          },
          {
            type: 'segment', id: 'text:1', order: 3, segment_type: 'text',
            text_index: 1, text_utf16_length: ANSWER.length,
            at: BASE_TIME + 26_000, ended_at: BASE_TIME + 27_000,
          },
        ],
      },
    }],
  })
}

async function installHistoryFixture(page: Page) {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.route('**/api/system/update', route => route.fulfill({ json: { available: false } }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public-assets/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as {
        type: string; id?: string; method?: string; params?: Record<string, unknown>
      }
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse())
        return
      }
      const key = String(frame.params?.key ?? frame.params?.sessionKey ?? SESSION_KEY)
      const payloads: Record<string, unknown> = {
        'chat.history': key === OTHER_SESSION_KEY
          ? chatHistoryPayload([{ role: 'assistant', id: 'other-answer', text: 'Another task is ready.' }])
          : completedHistory(),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': {
          sessions: [SESSION_KEY, OTHER_SESSION_KEY].map((sessionKey, index) => ({
            key: sessionKey, title: index ? 'Other activity task' : 'Activity overflow task',
            sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct',
            effectiveAgentId: 'main', updatedAt: BASE_TIME / 1_000,
            messageCount: index ? 1 : 10, status: 'ok', runStatus: 'idle',
          })),
          count: 2, ts: BASE_TIME / 1_000, has_more: false,
        },
        'onboarding.status': { audioConfigured: false },
        'usage.status': { sessions: [] },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method ?? ''] ?? {} }))
    })
  })
}

async function expectNoTailOverflow(page: Page) {
  await expect(page.locator(`${LAST_MESSAGE} .assistant-answer`)).toContainText(ANSWER)
  await expect(page.locator('.chat-message-list')).toHaveAttribute('data-virtualized', 'false')
  // Reaching scrollTop + clientHeight === scrollHeight alone misses this bug:
  // the browser can be at the bottom of hundreds of pixels of invisible overflow.
  await expect.poll(() => page.locator('.chat-thread').evaluate(thread => {
    const list = thread.querySelector<HTMLElement>('.chat-message-list')!
    const listEnd = list.getBoundingClientRect().bottom
      - thread.getBoundingClientRect().top + thread.scrollTop
    return thread.scrollHeight - Math.max(thread.clientHeight, listEnd)
  })).toBeLessThanOrEqual(1)
  await page.locator('.chat-thread').evaluate(thread => { thread.scrollTop = thread.scrollHeight })
  await expect.poll(() => page.locator('.chat-thread').evaluate(thread =>
    Math.abs(thread.scrollHeight - thread.scrollTop - thread.clientHeight),
  )).toBeLessThanOrEqual(1)
  await expect.poll(() => page.locator('.chat-thread').evaluate(thread => {
    const row = thread.querySelector<HTMLElement>('[data-chat-message-key="overflow-answer"]')!
    return thread.getBoundingClientRect().bottom - row.getBoundingClientRect().bottom
  })).toBeLessThanOrEqual(32)
  await expect(page.locator(LAST_MESSAGE)).toBeInViewport()
}

for (const width of [900, 1600]) {
  for (const reducedMotion of ['no-preference', 'reduce'] as const) {
    test(`collapsed historical activity has no scroll tail at ${width}px (${reducedMotion})`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.emulateMedia({ reducedMotion })
      await installHistoryFixture(page)
      await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)

      const row = page.locator(LAST_MESSAGE)
      const summary = row.locator('.assistant-activity__summary')
      const body = row.locator('.assistant-activity__body')
      const retryAnnouncement = row.locator('.assistant-activity-timeline .sr-only')
      await expect(summary).toHaveAttribute('aria-expanded', 'false')
      await expect(retryAnnouncement).toHaveCount(1)
      await expect(body).toHaveAttribute('aria-hidden', 'true')
      await expect.poll(() => body.evaluate(el => el.getBoundingClientRect().height)).toBe(0)
      await expectNoTailOverflow(page)

      // Preserve keyboard disclosure and the accessible retry text. Verify both
      // animation modes and repeated collapse after inspecting the retained body.
      for (let cycle = 0; cycle < 2; cycle++) {
        await summary.press('Enter')
        await expect(summary).toHaveAttribute('aria-expanded', 'true')
        await expect(body).toHaveAttribute('aria-hidden', 'false')
        await expect(body).not.toHaveAttribute('inert')
        await expect(row.getByText('Progress paragraph 14:', { exact: false })).toBeVisible()
        await expect(retryAnnouncement).not.toBeEmpty()
        await expectNoTailOverflow(page)
        await summary.press('Space')
        await expect(summary).toHaveAttribute('aria-expanded', 'false')
        await expect(body).toHaveAttribute('inert')
        await expect.poll(() => body.evaluate(el => el.getBoundingClientRect().height)).toBe(0)
        await expectNoTailOverflow(page)
      }

      await page.reload()
      await expect(summary).toHaveAttribute('aria-expanded', 'false')
      await expect(retryAnnouncement).toHaveCount(1)
      await expectNoTailOverflow(page)

      await page.locator(`[data-session-key="${OTHER_SESSION_KEY}"] .sidebar-history-item`).click()
      await expect(page.locator('[data-chat-message-key="other-answer"]')).toContainText('Another task is ready.')
      await expect(row).toHaveCount(0)
      await page.locator(`[data-session-key="${SESSION_KEY}"] .sidebar-history-item`).click()
      await expect(summary).toHaveAttribute('aria-expanded', 'false')
      await expect(retryAnnouncement).toHaveCount(1)
      await expectNoTailOverflow(page)
    })
  }
}
