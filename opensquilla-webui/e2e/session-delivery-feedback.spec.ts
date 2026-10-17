import type { Page, TestInfo } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { expect, test } from './real-gateway.fixture'
import { expectComposerValue } from './utils'

type Request = { type: string; id: string; method: string; params?: Record<string, unknown> }
type Response = { type?: string; id?: string; ok?: boolean; payload?: Record<string, unknown> }
const SESSION_A = 'agent:main:webchat:e2e-delivery-feedback-a'
const SESSION_B = 'agent:main:webchat:e2e-delivery-feedback-b'

test.use({
  isolatedRealGatewayInitialSessionKeys: [SESSION_A, SESSION_B],
  isolatedRealGatewayNativeDesktop: true,
})

/** Production Gateway admission, receipts, cancellation and TaskRuntime are real.
 * The proxy injects long display history, loses one ACK, and temporarily masks
 * receipt reads. This proves recovery UI under faults, not fault frequency. */
async function deliveryFault(page: Page) {
  const sends: Request[] = []
  const receipts: Request[] = []
  const aborts: Request[] = []
  let acknowledgement: Response | undefined
  let receiptsVisible = false
  let connections = 0
  const displayHistory = Array.from({ length: 50 }, (_, index) => ({
    id: `delivery-feedback-history-${index}`,
    role: index % 2 ? 'assistant' : 'user',
    text: `Historical message ${index + 1}. ` + 'Ordinary long conversation context. '.repeat(12),
    timestamp: `2026-01-01T12:${String(index).padStart(2, '0')}:00Z`,
  }))
  await page.routeWebSocket(/\/ws$/, browser => {
    connections++
    const server = browser.connectToServer()
    const requests = new Map<string, Request>()
    browser.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'req') {
        requests.set(frame.id, frame)
        if (frame.method === 'chat.send') sends.push(frame)
        if (frame.method === 'turns.receipt.get') receipts.push(frame)
        if (frame.method === 'chat.abort') aborts.push(frame)
      }
      server.send(raw)
    })
    server.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Response
      const request = frame.type === 'res' ? requests.get(String(frame.id)) : undefined
      if (request) requests.delete(request.id)
      if (request === sends[0] && request?.method === 'chat.send') {
        acknowledgement = frame
        browser.close({ code: 1012, reason: 'Synthetic lost admission acknowledgement' })
        server.close({ code: 1012, reason: 'Synthetic lost admission acknowledgement' })
        return
      }
      if (request?.method === 'turns.receipt.get' && frame.ok && !receiptsVisible) {
        browser.send(JSON.stringify({ ...frame, payload: { status: 'not_found', accepted: null } }))
        return
      }
      if (request?.method === 'sessions.history.page.v2' && frame.ok && frame.payload
        && request.params?.key === SESSION_A) {
        const items = displayHistory.map((message, index) => ({
          message_id: message.id, item_id: message.id, order: String(index + 1),
          role: message.role, preview: message.text, preview_complete: true,
          source_revision: `delivery-feedback-${index}`, content_availability: 'ready',
          contents: [], message,
        }))
        browser.send(JSON.stringify({ ...frame, payload: {
          ...frame.payload, items: [...items, ...(frame.payload.items as unknown[] || [])],
        } }))
        return
      }
      if (request?.method === 'chat.history' && frame.ok && frame.payload
        && (request.params?.sessionKey || request.params?.key) === SESSION_A) {
        const messages = [...displayHistory, ...(frame.payload.messages as unknown[] || [])]
        browser.send(JSON.stringify({ ...frame, payload: { ...frame.payload, messages, loaded_count: messages.length } }))
        return
      }
      browser.send(raw)
    })
  })
  return {
    sends, receipts, aborts,
    acknowledgement: () => acknowledgement,
    connections: () => connections,
    revealReceipts() { receiptsVisible = true },
  }
}

async function attachEvidence(page: Page, testInfo: TestInfo, network: Awaited<ReturnType<typeof deliveryFault>>) {
  const evidencePath = testInfo.outputPath('delivery-feedback-network.json')
  await writeFile(evidencePath, JSON.stringify({
    userAgent: await page.evaluate(() => navigator.userAgent),
    sends: network.sends, receipts: network.receipts, aborts: network.aborts,
    acknowledgement: network.acknowledgement(), connections: network.connections(),
  }, null, 2))
  await testInfo.attach('delivery-feedback-network', { path: evidencePath, contentType: 'application/json' })
}

async function expectFeedbackNearComposer(page: Page) {
  const status = page.getByTestId('chat-delivery-recovery')
  const check = page.getByTestId('chat-delivery-recovery-check')
  await expect(status).toBeInViewport()
  await expect(check).toBeInViewport()
  const statusBox = await status.boundingBox()
  const composerBox = await page.locator('.chat-textarea').boundingBox()
  expect(statusBox).not.toBeNull()
  expect(composerBox).not.toBeNull()
  expect(statusBox!.y + statusBox!.height).toBeLessThanOrEqual(composerBox!.y)
  expect(composerBox!.y - statusBox!.y - statusBox!.height).toBeLessThan(100)
  await expect(page.locator('.toast--danger, .msg-error__text')).toHaveCount(0)
}

test('long-chat delivery feedback stays visible after the receipt budget and checking preserves the draft', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = await deliveryFault(page)
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_A)}`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const status = page.getByTestId('chat-delivery-recovery')
  const check = page.getByTestId('chat-delivery-recovery-check')
  await expect(page.getByText('Historical message 50.', { exact: false }).first()).toBeVisible()
  await expect(status).toHaveCount(0)
  await input.fill('Recover this accepted message without replacing my next draft.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.acknowledgement()?.ok).toBe(true)
  await expect(status).toContainText('Automatic checks have paused', { timeout: 30_000 })
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  const draft = 'My next ordinary message stays editable.'
  await input.fill(draft)
  await expect.poll(() => page.locator('.chat-thread').evaluate(el =>
    el.scrollHeight > el.clientHeight * 3 && el.scrollTop > el.clientHeight)).toBe(true)
  await expectFeedbackNearComposer(page)
  await page.screenshot({ path: testInfo.outputPath('delivery-budget-near-composer.png') })

  await page.setViewportSize({ width: 375, height: 812 })
  await expectFeedbackNearComposer(page)
  await expect(input).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeInViewport()
  await page.screenshot({ path: testInfo.outputPath('delivery-budget-mobile.png') })
  const checksBefore = network.receipts.length
  await check.click()
  await expect.poll(() => network.receipts.length).toBeGreaterThan(checksBefore)
  await expect(check).toBeEnabled({ timeout: 30_000 })
  await expectFeedbackNearComposer(page)
  await expectComposerValue(input, draft)
  expect(network.sends).toHaveLength(1)
  expect(network.aborts).toHaveLength(0)
  for (const receipt of network.receipts) {
    expect(receipt.params).toEqual({ operation: 'chat.send', originalRequest: network.sends[0]!.params })
  }

  await page.setViewportSize({ width: 1440, height: 900 })
  network.revealReceipts()
  await check.click()
  await expect(status).toHaveCount(0, { timeout: 15_000 })
  await expectComposerValue(input, draft)
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
    .toBeVisible({ timeout: 30_000 })
  await expect(send).toBeEnabled()
  expect(network.sends).toHaveLength(1)
  expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
  await attachEvidence(page, testInfo, network)
})

test('unknown delivery feedback follows its session and explicit Stop targets only its original task', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = await deliveryFault(page)
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_A)}`)
  const input = page.locator('.chat-textarea')
  const status = page.getByTestId('chat-delivery-recovery')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  await input.fill('Keep this one request identifiable while its receipt is unavailable.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.acknowledgement()?.ok).toBe(true)
  await expect(status).toContainText('Automatic checks have paused', { timeout: 30_000 })
  const draft = 'A draft must survive checking another session.'
  await input.fill(draft)
  await expectFeedbackNearComposer(page)

  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expect(status).toHaveCount(0)
  await expect(send).toBeVisible()
  expect(network.aborts).toHaveLength(0)
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_A)
  await expectComposerValue(input, draft)
  await expectFeedbackNearComposer(page)
  await expect(stop).toBeEnabled()
  await stop.click()
  // Releasing the receipt also permits a persisted pre-acceptance Stop to
  // resolve. A known task may already be stopped through live task ownership.
  network.revealReceipts()
  const check = page.getByTestId('chat-delivery-recovery-check')
  if (await check.count()) await check.first().click()
  await expect.poll(() => network.aborts.length, { timeout: 15_000 }).toBe(1)
  const response = network.acknowledgement()!.payload!
  expect(network.aborts[0]!.params).toMatchObject({
    sessionKey: SESSION_A, taskId: response.taskId || response.task_id, scope: 'task',
  })
  await expect(status).toHaveCount(0, { timeout: 15_000 })
  await expect(stop).toHaveCount(0, { timeout: 15_000 })
  await expectComposerValue(input, draft)
  expect(network.sends).toHaveLength(1)
  expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
  await page.screenshot({ path: testInfo.outputPath('delivery-explicit-stop-preserves-draft.png') })
  await attachEvidence(page, testInfo, network)
})
