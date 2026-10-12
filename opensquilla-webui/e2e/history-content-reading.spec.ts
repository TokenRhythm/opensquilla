import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:e2e-content-reading'

async function recordCopies(page: Page) {
  await page.addInitScript(() => {
    const copied: string[] = []
    Object.defineProperty(window, '__historyCopiedText', { value: copied })
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text: string) => { copied.push(text) } },
    })
  })
  return () => page.evaluate(() => (window as unknown as { __historyCopiedText: string[] }).__historyCopiedText)
}

async function openHistory(
  page: Page,
  contentRef: Record<string, unknown>,
  role = 'assistant',
  historyMessages?: Record<string, unknown>[],
) {
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
        'chat.history': chatHistoryPayload(historyMessages ?? [
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
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
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

for (const role of ['user', 'assistant']) {
  test(`${role} Copy waits for complete history instead of reporting a copied preview`, async ({ page }) => {
    const body = 'COPY-BEGIN ' + 'readable text '.repeat(2600) + ' 中文🙂 COPY-END'
    const bytes = Buffer.from(body)
    const preview = bytes.subarray(0, 16384).toString('utf-8')
    let release!: () => void
    const ready = new Promise<void>(resolve => { release = resolve })
    let reads = 0
    const copiedText = await recordCopies(page)
    await page.route('**/api/content/read?**', async route => {
      reads += 1
      await ready
      const url = new URL(route.request().url())
      const raw = url.searchParams.get('view') === 'raw'
      const offset = Number(url.searchParams.get('offset'))
      const limit = Number(url.searchParams.get('limit'))
      await route.fulfill({
        status: raw ? 206 : 200, contentType: 'text/plain; charset=utf-8',
        body: raw ? bytes.subarray(offset, offset + limit) : body,
      })
    })
    try {
      await openHistory(page, {}, role, [{
        id: 'answer', message_id: 'answer', role, text: preview, timestamp: 1_800_000_001,
        preview_complete: false,
        content_ref: { version: 1, sessionKey: SESSION, sessionId: 'session-1', messageId: 'answer',
          revision: 'r1', view: role === 'user' ? 'raw' : 'display', byteLength: bytes.length },
      }])
      const message = page.locator(`[data-message-id="answer"].${role === 'user' ? 'msg-user' : 'msg-ai'}`)
      await expect.poll(() => reads).toBe(1)
      await message.getByTitle('Copy', { exact: true }).click()
      expect(await copiedText()).toHaveLength(0)
      await expect(message.locator('.msg-copy-live')).not.toHaveText('Copied')
      release()
      await expect.poll(copiedText).toHaveLength(1)
      const copied = (await copiedText())[0]!
      if (role === 'user') expect(copied).toBe(body)
      else expect(copied).toContain(body)
      await expect(message.locator('.msg-copy-live')).toHaveText('Copied')
    } finally {
      release()
    }
  })
}

for (const copyWhileLoading of [false, true]) {
  test(`complete history restores the assistant segments before and after Steer${copyWhileLoading ? ' while copying' : ''}`, async ({ page }) => {
    const before = 'BEFORE-STEER ' + 'red theme '.repeat(1900) + '🙂 PREFIX-END\n\n'
    const after = 'AFTER-STEER ' + 'blue theme '.repeat(1700) + '🙂 SUFFIX-END'
    const body = before + after
    let release!: () => void
    const ready = new Promise<void>(resolve => { release = resolve })
    let reads = 0
    const copiedText = await recordCopies(page)
    await page.route('**/api/content/read?**', async route => {
      reads += 1
      await ready
      await route.fulfill({ contentType: 'text/plain; charset=utf-8', body })
    })
    try {
      await openHistory(page, {}, 'assistant', [
        { id: 'question', role: 'user', text: 'Make the theme red.', timestamp: 1_800_000_000,
          turn_context: { turn_id: 'turn-steer' } },
        { id: 'steer', role: 'user', text: 'Change the theme to blue.', timestamp: 1_800_000_001,
          turn_context: { turn_id: 'turn-steer', disposition: 'applied', model_call_id: '2.0', applied_iteration: 2 } },
        { id: 'answer', message_id: 'answer', role: 'assistant', text: body.slice(0, 16384),
          timestamp: 1_800_000_002, turn_context: { turn_id: 'turn-steer' }, preview_complete: false,
          content_ref: { version: 1, sessionKey: SESSION, sessionId: 'session-1', messageId: 'answer',
            revision: 'r1', view: 'display', byteLength: Buffer.byteLength(body) },
          usage: { model_call_segments: [{ model_call_id: '2.0', iteration: 2,
            start_codepoint: Array.from(before).length, end_codepoint: Array.from(body).length }] },
        },
      ])
      await expect.poll(() => reads).toBe(1)
      if (copyWhileLoading) {
        await page.locator('[data-message-id="answer"].msg-ai').getByTitle('Copy', { exact: true }).click()
        expect(await copiedText()).toHaveLength(0)
      }
      release()
      await expect(page.locator('.chat-thread')).toContainText('SUFFIX-END')
      const rows = page.locator('.chat-thread .msg-user, .chat-thread .msg-ai')
      await expect(rows).toHaveCount(4)
      await expect(rows.nth(0)).toContainText('Make the theme red.')
      await expect(rows.nth(1)).toContainText('BEFORE-STEER')
      await expect(rows.nth(1)).toContainText('PREFIX-END')
      await expect(rows.nth(1)).not.toContainText('AFTER-STEER')
      await expect(rows.nth(2)).toContainText('Change the theme to blue.')
      await expect(rows.nth(3)).toContainText('AFTER-STEER')
      await expect(rows.nth(3)).not.toContainText('BEFORE-STEER')
      if (copyWhileLoading) {
        // Copy retains the aggregate range clicked before hydration, even though
        // that same durable body is now rendered as two segments.
        await expect.poll(copiedText).toHaveLength(1)
        expect((await copiedText())[0]).toContain(body)
        expect(reads).toBe(2)
      } else {
        expect(reads).toBe(1)
      }
    } finally {
      release()
    }
  })
}

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
