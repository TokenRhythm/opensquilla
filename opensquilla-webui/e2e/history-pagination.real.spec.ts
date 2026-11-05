import { expect, test } from './real-gateway.fixture'

const SESSION = 'agent:main:webchat:long-history-pagination'

type HistoryPage = {
  items: { message_id: string }[]
  before_cursor: string | null
  has_more_before: boolean
  canonical_complete: boolean
  compaction_summaries: Record<string, unknown>[]
  turn_outcomes: Record<string, unknown>[]
}

test.use({
  isolatedRealGatewayInitialHistory: [{
    sessionKey: SESSION, messageCount: 60, summaryCount: 64, oversizedActivityMessage: 5,
  }],
})
test.setTimeout(90_000)

test('loads through oversized summaries and activity metadata without skipping older messages', async ({ page, isolatedRealGateway }) => {
  const pages: HistoryPage[] = []
  const failures: unknown[] = []
  const cursors: (string | null)[] = []
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  page.on('websocket', socket => {
    const historyRequests = new Map<string, string | null>()
    socket.on('framesent', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type === 'req' && frame.method === 'sessions.history.page.v2') {
        historyRequests.set(frame.id, frame.params.cursor ?? null)
      }
    })
    socket.on('framereceived', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type !== 'res' || !historyRequests.has(frame.id)) return
      const cursor = historyRequests.get(frame.id)!
      historyRequests.delete(frame.id)
      if (!frame.ok) { failures.push(frame.error); return }
      expect(Buffer.byteLength(JSON.stringify(frame.payload))).toBeLessThanOrEqual(1_048_576)
      pages.push(frame.payload)
      cursors.push(cursor)
    })
  })

  await page.goto(isolatedRealGateway.controlUrl + 'chat?session=' + encodeURIComponent(SESSION))
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 20_000 })
  await expect.poll(() => pages.length).toBeGreaterThan(0)
  await expect(page.locator('[data-message-id="history-message-60"]')).toBeVisible()
  await expect(page.getByTestId('history-metadata-limited')).toHaveCount(1)
  const thread = page.locator('.chat-thread')
  for (let attempt = 0; attempt < 8 && pages.at(-1)?.has_more_before; attempt += 1) {
    const count = pages.length
    await thread.hover()
    await page.mouse.wheel(0, -100_000)
    await expect.poll(() => pages.length).toBeGreaterThan(count)
  }

  expect(failures).toEqual([])
  expect(pages.at(-1)?.has_more_before).toBe(false)
  expect(pages.every(page => page.canonical_complete)).toBe(true)
  expect(new Set(pages.flatMap(page => page.items.map(item => item.message_id)))).toEqual(
    new Set(Array.from({ length: 60 }, (_, index) => `history-message-${index + 1}`)),
  )
  expect(new Set(cursors.filter(cursor => cursor !== null)).size).toBe(cursors.filter(cursor => cursor !== null).length)
  expect(pages.some(page => page.turn_outcomes.some(outcome => outcome.activity_snapshot_omitted === 'history_page_budget'))).toBe(true)
  await expect(page.locator('[data-message-id="history-message-1"]')).toBeVisible()
  await expect(page.getByTestId('history-load-failure')).toHaveCount(0)
  await expect(page.getByTestId('history-metadata-limited')).toContainText('Messages are still available.')
})

test('explains limited metadata in an independent search context window', async ({ page, isolatedRealGateway }) => {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.goto(isolatedRealGateway.controlUrl + 'chat?session=' + encodeURIComponent(SESSION))
  await expect(page.locator('[data-message-id="history-message-60"]')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByTestId('history-metadata-limited')).toHaveCount(1)

  await page.getByTestId('chat-session-actions-trigger').click()
  await page.getByTestId('chat-session-action-find').click()
  await page.getByRole('textbox', { name: 'Find in conversation', exact: true }).fill('Historical answer 5.')
  await expect(page.getByTestId('chat-find-metadata-limited')).toContainText('Messages are still available.', { timeout: 20_000 })
  await expect(page.locator('[data-message-id="history-message-5"]')).toBeVisible()
  await expect(page.getByText('Showing context around a search result', { exact: true })).toBeVisible()
  await expect(page.getByTestId('history-load-sentinel')).toHaveCount(0)

  await page.getByRole('button', { name: 'Close find (Esc)', exact: true }).click()
  await expect(page.getByTestId('chat-find-metadata-limited')).toHaveCount(0)
  await expect(page.getByTestId('history-metadata-limited')).toHaveCount(1)
  await expect(page.locator('[data-message-id="history-message-60"]')).toBeVisible()
})
