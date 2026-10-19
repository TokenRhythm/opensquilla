import type { Page, TestInfo } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { expect, test } from './real-gateway.fixture'
import { expectComposerValue } from './utils'

type Request = { type: string; id: string; method: string; params?: Record<string, unknown> }
type Response = { type?: string; id?: string; ok?: boolean; payload?: Record<string, unknown> }
const SESSION_A = 'agent:main:webchat:e2e-delivery-feedback-a'
const SESSION_B = 'agent:main:webchat:e2e-delivery-feedback-b'
const networks = new WeakMap<Page, Awaited<ReturnType<typeof deliveryFault>>>()

test.use({
  isolatedRealGatewayInitialSessionKeys: [SESSION_A, SESSION_B],
  isolatedRealGatewayNativeDesktop: true,
})

test.afterEach(async ({ page }, testInfo) => {
  const network = networks.get(page)
  if (network) await attachEvidence(page, testInfo, network)
})

/** Production Gateway admission, receipts, cancellation and TaskRuntime are real.
 * The proxy injects long display history, loses one ACK, and temporarily masks
 * receipt reads. This proves recovery UI under faults, not fault frequency. */
async function deliveryFault(page: Page) {
  const sends: Request[] = []
  const receipts: Request[] = []
  const aborts: Request[] = []
  const dispositions: Array<{ observedConnection: number; observedAt: number; payload: Record<string, unknown> }> = []
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
    const observedConnection = connections
    const server = browser.connectToServer()
    const requests = new Map<string, Request>()
    browser.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'req') {
        requests.set(frame.id, frame)
        if (frame.method === 'chat.send') sends.push(frame)
        if (frame.method === 'turns.receipt.get') receipts.push({ ...frame, observedConnection, observedAt: Date.now() } as Request)
        if (frame.method === 'chat.abort') aborts.push(frame)
      }
      server.send(raw)
    })
    server.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Response
      const eventFrame = frame as Response & { event?: string }
      if (eventFrame.type === 'event' && eventFrame.event === 'session.event.input_disposition') {
        dispositions.push({ observedConnection, observedAt: Date.now(), payload: eventFrame.payload ?? {} })
      }
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
    sends, receipts, aborts, dispositions,
    acknowledgement: () => acknowledgement,
    connections: () => connections,
    revealReceipts() { receiptsVisible = true },
  }
}

async function attachEvidence(page: Page, testInfo: TestInfo, network: Awaited<ReturnType<typeof deliveryFault>>) {
  const evidencePath = testInfo.outputPath('delivery-feedback-network.json')
  await writeFile(evidencePath, JSON.stringify({
    userAgent: await page.evaluate(() => navigator.userAgent).catch(() => 'browser closed after failure'),
    sends: network.sends, receipts: network.receipts, aborts: network.aborts, dispositions: network.dispositions,
    acknowledgement: network.acknowledgement(), connections: network.connections(),
  }, null, 2))
  await testInfo.attach('delivery-feedback-network', { path: evidencePath, contentType: 'application/json' })
}

async function expectBoundedReceiptRounds(page: Page, network: Awaited<ReturnType<typeof deliveryFault>>) {
  await expect.poll(() => network.receipts.length, { timeout: 30_000 }).toBeGreaterThanOrEqual(4)
  // Each existing round is four reads separated by 0.25/1/4 seconds. A newer
  // authenticated disposition may legitimately schedule one further round.
  await page.waitForTimeout(6_000)
  const requestId = network.sends[0]!.params!.clientRequestId
  const changedTokens = new Set(network.dispositions
    .filter(event => event.payload.client_request_id === requestId)
    .map(event => JSON.stringify([event.payload.revision, event.payload.promoted_turn_id,
      event.payload.target_turn_id, event.payload.disposition])))
  const generations = new Set(network.receipts.map(receipt => (receipt as Request & { observedConnection: number }).observedConnection))
  expect(network.receipts.length).toBeLessThanOrEqual(4 * (generations.size + changedTokens.size))
  const readsAtBudget = network.receipts.length
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'))
    document.dispatchEvent(new Event('visibilitychange'))
    window.dispatchEvent(new Event('online'))
  })
  await page.waitForTimeout(6_000)
  expect(network.receipts).toHaveLength(readsAtBudget)
}

async function expectQuietExecutionNearComposer(page: Page) {
  await expect(page.getByTestId('chat-delivery-recovery')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(page.getByTestId('chat-composer-recovery')).toHaveCount(0)
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  await expect(stop).toBeEnabled()
  await expect(stop).toBeInViewport()
  const stopBox = await stop.boundingBox()
  const composerBox = await page.locator('.chat-textarea').boundingBox()
  expect(stopBox).not.toBeNull()
  expect(composerBox).not.toBeNull()
  expect(stopBox!.y).toBeGreaterThanOrEqual(composerBox!.y)
  expect(stopBox!.y - composerBox!.y - composerBox!.height).toBeLessThan(100)
  await expect(page.locator('.chat-send-btn .loading-spinner, .msg-user .loading-spinner')).toHaveCount(0)
  await expect(page.locator('.toast--danger, .msg-error__text')).toHaveCount(0)
}

test('long-chat known execution stays quiet after the receipt budget and keeps Stop in the composer', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = await deliveryFault(page)
  networks.set(page, network)
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_A)}`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const status = page.getByTestId('chat-delivery-recovery')
  const check = page.getByTestId('chat-composer-recovery')
  await expect(page.getByText('Historical message 50.', { exact: false }).first()).toBeVisible()
  await expect(status).toHaveCount(0)
  await input.fill('Recover this accepted message without replacing my next draft.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.acknowledgement()?.ok).toBe(true)
  await expectBoundedReceiptRounds(page, network)
  await expect(status).toHaveCount(0)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  const draft = 'My next ordinary message stays editable.'
  await input.fill(draft)
  await expect.poll(() => page.locator('.chat-thread').evaluate(el =>
    el.scrollHeight > el.clientHeight * 3 && el.scrollTop > el.clientHeight)).toBe(true)
  await expectQuietExecutionNearComposer(page)
  await page.screenshot({ path: testInfo.outputPath('delivery-budget-near-composer.png') })

  await page.setViewportSize({ width: 375, height: 812 })
  await expectQuietExecutionNearComposer(page)
  await expect(input).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeInViewport()
  await page.screenshot({ path: testInfo.outputPath('delivery-budget-mobile.png') })
  // Current task ownership is authoritative even though the separate durable
  // receipt remains unknown. Stop takes precedence; no second recovery button
  // or global/local recovery panel is added during execution.
  await expect(check).toHaveCount(0)
  await expectQuietExecutionNearComposer(page)
  await expectComposerValue(input, draft)
  expect(network.sends).toHaveLength(1)
  expect(network.aborts).toHaveLength(0)
  for (const receipt of network.receipts) {
    expect(receipt.params).toEqual({ operation: 'chat.send', originalRequest: network.sends[0]!.params })
  }

  await page.setViewportSize({ width: 1440, height: 900 })
  network.revealReceipts()
  await expectComposerValue(input, draft)
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
    .toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
  if (await check.count()) await check.click()
  await expect(send).toBeEnabled()
  expect(network.sends).toHaveLength(1)
  expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
  await attachEvidence(page, testInfo, network)
})

test('unknown receipt preserves quiet session-scoped execution and explicit Stop targets only its original task', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = await deliveryFault(page)
  networks.set(page, network)
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_A)}`)
  const input = page.locator('.chat-textarea')
  const status = page.getByTestId('chat-delivery-recovery')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  await input.fill('Keep this one request identifiable while its receipt is unavailable.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.acknowledgement()?.ok).toBe(true)
  await expectBoundedReceiptRounds(page, network)
  await expect(status).toHaveCount(0)
  const draft = 'A draft must survive checking another session.'
  await input.fill(draft)
  await expectQuietExecutionNearComposer(page)

  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expect(status).toHaveCount(0)
  await expect(send).toBeVisible()
  expect(network.aborts).toHaveLength(0)
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_A)
  await expectComposerValue(input, draft)
  await expectQuietExecutionNearComposer(page)
  await expect(stop).toBeEnabled()
  await stop.click()
  // Releasing the receipt also permits a persisted pre-acceptance Stop to
  // resolve. A known task may already be stopped through live task ownership.
  network.revealReceipts()
  const check = page.getByTestId('chat-composer-recovery')
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
