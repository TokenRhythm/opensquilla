import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:e2e-content-reading'

async function openHistory(page: Page, contentRef: Record<string, unknown>, role = 'assistant') {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public-assets/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  for (const endpoint of ['approvals', 'system/update', 'elevated-mode']) {
    await page.route(`**/api/${endpoint}`, route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ pending: [], enabled: false, mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
    }))
  }
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') { ws.send(helloOkResponse()); return }
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload([
          { id: 'question', role: 'user', text: 'Show the answer.', timestamp: 1_800_000_000 },
          {
            id: 'answer', message_id: 'answer', role,
            text: 'Visible preview.', timestamp: 1_800_000_001,
            content_ref: { version: 1, sessionKey: SESSION, sessionId: 'session-1', messageId: 'answer', revision: 'r1', ...contentRef },
          },
        ]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {} }))
    })
  })
  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION))
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
}

test('ordinary assistant history hydrates automatically and offers local retry after failure', async ({ page }) => {
  const body = 'Readable answer. '.repeat(2048) + '\n\n**ANSWER-END**'
  let reads = 0
  await page.route('**/api/content/read?**', route => {
    const url = new URL(route.request().url())
    expect(url.searchParams.get('view')).toBe('display')
    expect(url.searchParams.get('revision')).toBe('r1')
    reads += 1
    return route.fulfill({
      status: reads === 1 ? 503 : 200,
      contentType: 'text/plain; charset=utf-8', body: reads === 1 ? 'Temporarily unavailable' : body,
    })
  })
  // Stored protocol JSON length need not equal its projected visible text size.
  await openHistory(page, { view: 'display', byteLength: 2 * 1024 * 1024 })
  const hydration = page.getByTestId('chat-history-content-hydration')
  await expect(hydration.locator('.chat-history-content-hydration__error')).toBeVisible()
  await expect(page.locator('.chat-thread')).toContainText('Visible preview.')
  expect(reads).toBe(1)
  await hydration.getByRole('button', { name: /retry/i }).click()
  await expect(page.locator('.chat-thread')).toContainText('ANSWER-END')
  await expect(page.locator('.chat-thread strong')).toHaveText('ANSWER-END')
  await expect(page.locator('.chat-thread')).not.toContainText('Visible preview.')
  await expect(hydration).toHaveCount(0)
  await expect(page.getByTestId('chat-content-export')).toHaveCount(0)
  expect(reads).toBe(2)
})

test('ordinary user history hydrates ranges into one message without manual pages', async ({ page }) => {
  const paragraphs = ('Readable 中文 🌊 text. '.repeat(512) + '\n\n').repeat(40)
  const body = Buffer.from('## First section\n\n' + paragraphs + '## Second section\n\n' + paragraphs + '**ANSWER-END**')
  expect(body.byteLength).toBeGreaterThan(1024 * 1024)
  const offsets: number[] = []
  await page.route('**/api/content/read?**', route => {
    const url = new URL(route.request().url())
    const offset = Number(url.searchParams.get('offset'))
    const limit = Number(url.searchParams.get('limit'))
    expect(limit).toBeLessThanOrEqual(256 * 1024)
    offsets.push(offset)
    return route.fulfill({ status: 206, contentType: 'text/plain; charset=utf-8', body: body.subarray(offset, offset + limit) })
  })
  await openHistory(page, { view: 'raw', byteLength: body.byteLength }, 'user')
  const thread = page.locator('.chat-thread')
  await expect(thread.locator('.msg-user[data-message-id="answer"]')).toContainText(body.toString('utf-8'))
  await expect(thread).not.toContainText('Visible preview.')
  await expect(page.getByTestId('chat-history-content-hydration')).toHaveCount(0)
  for (const id of ['chat-history-content-page', 'chat-history-content-next', 'chat-history-content-previous']) {
    await expect(page.getByTestId(id)).toHaveCount(0)
  }
  await expect(page.getByRole('button', { name: /read full message/i })).toHaveCount(0)
  await expect(page.getByTestId('chat-content-export')).toHaveCount(0)
  expect(offsets).toEqual(Array.from({ length: Math.ceil(body.byteLength / (256 * 1024)) }, (_, index) => index * 256 * 1024))
})

test('history beyond the existing raw read capacity stays an explicit preview without a pager', async ({ page }) => {
  let reads = 0
  await page.route('**/api/content/read?**', route => {
    reads += 1
    return route.fulfill({ status: 500, body: 'Unexpected full-body read' })
  })
  await openHistory(page, { view: 'raw', byteLength: 128 * 1024 * 1024 }, 'user')
  const hydration = page.getByTestId('chat-history-content-hydration')
  await expect(hydration).toContainText('preview')
  await expect(hydration.getByRole('button')).toHaveCount(0)
  await expect(page.locator('.chat-thread')).toContainText('Visible preview.')
  for (const id of ['chat-history-content-page', 'chat-history-content-next', 'chat-history-content-previous']) {
    await expect(page.getByTestId(id)).toHaveCount(0)
  }
  expect(reads).toBe(0)
})
