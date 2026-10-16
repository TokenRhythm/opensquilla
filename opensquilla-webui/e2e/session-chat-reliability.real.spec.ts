import type { Page } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { expect, test } from './real-gateway.fixture'
import { expectComposerValue } from './utils'

type Request = { type: string; id: string; method: string; params?: Record<string, unknown> }
type Send = { request: Request; acknowledgement?: Record<string, unknown> }
const SESSION_A = 'agent:main:webchat:e2e-late-ack-a'
const SESSION_B = 'agent:main:webchat:e2e-late-ack-b'

test.use({
  isolatedRealGatewayInitialSessionKeys: [SESSION_A, SESSION_B],
  isolatedRealGatewayNativeDesktop: true,
})

/** A network fault proxy, not a synthetic Gateway: production RPC, receipts,
 * SQLite and TaskRuntime execute every request against the isolated fixture. */
async function interceptAdmission(page: Page, fault: 'hold-first-ack' | 'lose-first-ack') {
  const sends: Send[] = []
  const aborts: Request[] = []
  const receipts: Request[] = []
  let release: (() => void) | null = null
  let connections = 0
  await page.routeWebSocket(/\/ws$/, browser => {
    connections++
    const server = browser.connectToServer()
    const pending = new Map<string, Send>()
    browser.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'req') {
        if (frame.method === 'chat.send') {
          const send = { request: frame }
          sends.push(send)
          pending.set(frame.id, send)
        }
        if (frame.method === 'chat.abort') aborts.push(frame)
        if (frame.method.includes('receipt')) receipts.push(frame)
      }
      server.send(raw)
    })
    server.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Record<string, unknown>
      const send = frame.type === 'res' ? pending.get(String(frame.id)) : undefined
      if (send) {
        pending.delete(String(frame.id))
        send.acknowledgement = frame
        if (send === sends[0]) {
          if (fault === 'hold-first-ack') release = () => browser.send(raw)
          else {
            // The response proves the Gateway handled the original request.
            // Drop only its acknowledgement, then break the physical socket.
            browser.close({ code: 1012, reason: 'Synthetic lost admission acknowledgement' })
            server.close({ code: 1012, reason: 'Synthetic lost admission acknowledgement' })
          }
          return
        }
      }
      browser.send(raw)
    })
  })
  return {
    sends, aborts, receipts,
    connections: () => connections,
    releaseFirstAcknowledgement() {
      if (!release) throw new Error('The real Gateway has not acknowledged the first send')
      const publish = release
      release = null
      publish()
    },
  }
}

async function observeVisibleFailures(page: Page) {
  await page.addInitScript(() => {
    const failures: string[] = []
    Object.defineProperty(window, '__sessionReliabilityFailures', { value: failures })
    const observe = () => {
      for (const node of document.querySelectorAll('.toast--danger, .chat-thread .msg-error__text')) {
        const text = node.textContent?.trim()
        if (text && !failures.includes(text)) failures.push(text)
      }
    }
    new MutationObserver(observe).observe(document, {
      subtree: true, childList: true, characterData: true, attributes: true,
    })
  })
}

async function visibleFailures(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as {
    __sessionReliabilityFailures: string[]
  }).__sessionReliabilityFailures)
}

/** Passive observation only: healthy requests, acknowledgements and events
 * travel over the production socket without a route or a response shim. */
function observeHealthyAdmission(page: Page) {
  const sends: Send[] = []
  const aborts: Request[] = []
  const receipts: Request[] = []
  const startedAt = performance.now()
  const timings: Array<{ event: string; elapsedMs: number }> = []
  const mark = (event: string) => timings.push({ event, elapsedMs: performance.now() - startedAt })
  page.on('websocket', socket => {
    const pending = new Map<string, Send>()
    socket.on('framesent', ({ payload }) => {
      const frame = JSON.parse(String(payload)) as Request
      if (frame.type !== 'req') return
      if (frame.method === 'chat.send') {
        const send = { request: frame }
        sends.push(send)
        pending.set(frame.id, send)
        mark(`send-${sends.length}`)
      }
      if (frame.method === 'chat.abort') aborts.push(frame)
      if (frame.method.includes('receipt')) receipts.push(frame)
    })
    socket.on('framereceived', ({ payload }) => {
      const frame = JSON.parse(String(payload)) as Record<string, unknown>
      const send = frame.type === 'res' ? pending.get(String(frame.id)) : undefined
      if (!send) return
      send.acknowledgement = frame
      pending.delete(String(frame.id))
      mark(`ack-${sends.indexOf(send) + 1}`)
    })
  })
  return { sends, aborts, receipts, timings, mark }
}

test('healthy consecutive turns preserve answers and the next draft through A → B → A and reload', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await observeVisibleFailures(page)
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = observeHealthyAdmission(page)
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_A)}`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  const recovery = page.getByTestId('chat-delivery-recovery')
  const firstPrompt = 'Complete this ordinary first turn without an admission fault.'
  const secondPrompt = 'Continue this same conversation with an ordinary second turn.'
  const firstAnswer = 'Task one completed after the lifecycle checks.'
  const secondAnswer = 'Task two completed after Goal removal.'
  const answer = (text: string) => page.locator('.msg-ai').filter({ hasText: text })

  await input.fill(firstPrompt)
  await expect(recovery).toHaveCount(0)
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.sends[0]?.acknowledgement?.ok).toBe(true)
  await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
  await expect(stop).toBeVisible()
  await expect(recovery).toHaveCount(0)
  await input.fill(secondPrompt)
  network.mark('release-first-provider')
  await isolatedRealGateway.releaseFirstTask()
  await expect(answer(firstAnswer)).toBeVisible({ timeout: 30_000 })
  await expect(stop).toHaveCount(0)
  await expectComposerValue(input, secondPrompt)
  network.mark('first-answer-visible')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.sends[1]?.acknowledgement?.ok).toBe(true)
  await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(2)
  await expect(stop).toBeVisible()
  const draft = 'Keep this unsent next draft after completion, navigation and reload.'
  await input.fill(draft)
  await expect(recovery).toHaveCount(0)
  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expectComposerValue(input, '')
  await expect(stop).toHaveCount(0)
  await expect(recovery).toHaveCount(0)
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_A)
  await expect(answer(firstAnswer)).toBeVisible()
  await expectComposerValue(input, draft)
  await expect(stop).toBeVisible()
  await expect(recovery).toHaveCount(0)
  expect(network.aborts).toEqual([])
  network.mark('release-second-provider')
  await isolatedRealGateway.releaseSecondTask()
  await expect(answer(secondAnswer)).toBeVisible({ timeout: 30_000 })
  await expect(stop).toHaveCount(0)
  await expect(recovery).toHaveCount(0)
  await expectComposerValue(input, draft)
  network.mark('second-answer-visible')

  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expectComposerValue(input, '')
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_A)
  await expect(answer(firstAnswer)).toHaveCount(1)
  await expect(answer(secondAnswer)).toHaveCount(1)
  await expectComposerValue(input, draft)
  expect(await visibleFailures(page)).toEqual([])
  await page.reload()
  await expect(answer(firstAnswer)).toBeVisible({ timeout: 30_000 })
  await expect(answer(secondAnswer)).toBeVisible()
  await expect(answer(firstAnswer)).toHaveCount(1)
  await expect(answer(secondAnswer)).toHaveCount(1)
  await expect(page.locator('.msg-user').filter({ hasText: firstPrompt })).toHaveCount(1)
  await expect(page.locator('.msg-user').filter({ hasText: secondPrompt })).toHaveCount(1)
  await expectComposerValue(input, draft)
  await expect(send).toBeEnabled()
  await expect(stop).toHaveCount(0)
  await expect(recovery).toHaveCount(0)
  network.mark('answers-and-draft-restored')

  expect(network.sends.map(item => item.request.params?.sessionKey)).toEqual([SESSION_A, SESSION_A])
  expect(network.aborts).toEqual([])
  expect(network.receipts).toEqual([])
  const providerCalls = await isolatedRealGateway.readProviderCalls()
  expect(providerCalls.map(call => call.callNumber)).toEqual([1, 2])
  expect(providerCalls[1]?.firstReplyInAssistantHistory).toBe(true)
  expect(await visibleFailures(page)).toEqual([])
  expect(rendererErrors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('healthy-consecutive-turns-after-reload.png') })
  const evidencePath = testInfo.outputPath('healthy-consecutive-turns.json')
  await writeFile(evidencePath, JSON.stringify({ userAgent: await page.evaluate(() => navigator.userAgent),
    sends: network.sends, aborts: network.aborts, receipts: network.receipts,
    providerCalls, timings: network.timings }, null, 2))
  await testInfo.attach('healthy-consecutive-turns', { path: evidencePath, contentType: 'application/json' })
})

test('healthy explicit Stop cancels only its accepted task and preserves the next draft after reload', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(90_000)
  await observeVisibleFailures(page)
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = observeHealthyAdmission(page)
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_B)}`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  const recovery = page.getByTestId('chat-delivery-recovery')
  const prompt = 'Wait for my explicit Stop on this ordinary accepted task.'
  const draft = 'This next draft is unsent and must survive my explicit Stop.'
  await input.fill(prompt)
  await expect(recovery).toHaveCount(0)
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.sends[0]?.acknowledgement?.ok).toBe(true)
  await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
  await expect(stop).toBeVisible()
  await expect(recovery).toHaveCount(0)
  await input.fill(draft)
  await stop.click()
  await expect.poll(() => network.aborts.length).toBe(1)
  await expect(stop).toHaveCount(0, { timeout: 15_000 })
  await expectComposerValue(input, draft)
  await expect(recovery).toHaveCount(0)
  expect(await visibleFailures(page)).toEqual([])
  await page.reload()
  await expectComposerValue(input, draft)
  await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
  await expect(stop).toHaveCount(0)
  await expect(send).toBeEnabled()
  await expect(recovery).toHaveCount(0)

  expect(network.sends.map(item => item.request.params?.sessionKey)).toEqual([SESSION_B])
  expect(network.aborts.map(item => item.params?.sessionKey)).toEqual([SESSION_B])
  const accepted = network.sends[0]!.acknowledgement?.payload as Record<string, unknown>
  expect(accepted.task_id).toEqual(expect.any(String))
  expect(network.aborts[0]!.params?.taskId).toBe(accepted.task_id)
  expect(network.receipts).toEqual([])
  const providerCalls = await isolatedRealGateway.readProviderCalls()
  expect(providerCalls.map(call => call.callNumber)).toEqual([1])
  expect(await visibleFailures(page)).toEqual([])
  expect(rendererErrors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('healthy-explicit-stop-after-reload.png') })
  const evidencePath = testInfo.outputPath('healthy-explicit-stop.json')
  await writeFile(evidencePath, JSON.stringify({ userAgent: await page.evaluate(() => navigator.userAgent),
    sends: network.sends, aborts: network.aborts, receipts: network.receipts,
    providerCalls, timings: network.timings }, null, 2))
  await testInfo.attach('healthy-explicit-stop', { path: evidencePath, contentType: 'application/json' })
})

test('late acceptance after A → B send → A never cancels the accepted A task', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = await interceptAdmission(page, 'hold-first-ack')
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  // Both sessions are already materialized. Every transition stays on the
  // same /chat route name, so ChatView and its pending-send owner are reused.
  await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(SESSION_A)}`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const stop = page.getByRole('button', { name: 'Stop current response', exact: true })
  await input.fill('Keep the first accepted session running while its acknowledgement is delayed.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.sends[0]?.acknowledgement?.ok).toBe(true)
  await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
  const firstSession = String(network.sends[0]!.request.params?.sessionKey)
  expect(firstSession).toBe(SESSION_A)

  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await input.fill('Run a second session while the first response acknowledgement is delayed.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.sends.length).toBe(2)
  const secondSession = String(network.sends[1]!.request.params?.sessionKey)
  expect(secondSession).toBe(SESSION_B)
  await expect(stop).toBeVisible()
  await input.fill('The second session draft must survive navigation.')

  await page.locator(`[data-session-key="${firstSession}"] .sidebar-history-item`).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(firstSession)
  await expect(stop).toBeVisible()
  network.releaseFirstAcknowledgement()
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
    .toBeVisible({ timeout: 30_000 })
  expect(network.aborts).toEqual([])
  expect(network.sends).toHaveLength(2)
  await expect(stop).toHaveCount(0)

  // A shared provider may serialize its held streams; acceptance of B is
  // sufficient for the navigation race, and B can run after A completes.
  await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length,
    { timeout: 30_000 }).toBe(2)

  // Explicit Stop must retain its authority after stale-ACK cancellation is removed.
  await page.locator(`[data-session-key="${secondSession}"] .sidebar-history-item`).click()
  await expect(stop).toBeVisible()
  await expectComposerValue(input, 'The second session draft must survive navigation.')
  await stop.click()
  await expect.poll(() => network.aborts.length).toBe(1)
  expect(network.aborts[0]!.params?.sessionKey).toBe(secondSession)
  await expect(stop).toHaveCount(0, { timeout: 15_000 })
  expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1, 2])
  expect(rendererErrors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('late-ack-navigation-explicit-stop.png') })
  const evidencePath = testInfo.outputPath('admission-network-observations.json')
  await writeFile(evidencePath, JSON.stringify({ userAgent: await page.evaluate(() => navigator.userAgent),
    sends: network.sends, aborts: network.aborts,
    providerCalls: await isolatedRealGateway.readProviderCalls() }, null, 2))
  await testInfo.attach('admission-network-observations', { path: evidencePath, contentType: 'application/json' })
})

test('a lost acceptance acknowledgement recovers by receipt without a false failure or duplicate execution', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(90_000)
  await observeVisibleFailures(page)
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  const network = await interceptAdmission(page, 'lose-first-ack')
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const prompt = 'Recover this one accepted request after losing its acknowledgement.'
  await input.fill(prompt)
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.sends[0]?.acknowledgement?.ok).toBe(true)
  await expect.poll(() => network.connections()).toBeGreaterThan(1)
  await expect.poll(() => network.receipts.length).toBeGreaterThan(0)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeVisible()
  const draft = 'Preserve my next message while the accepted request recovers.'
  await input.fill(draft)
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
    .toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
  await expectComposerValue(input, draft)
  await expect(send).toBeEnabled()
  expect(network.sends).toHaveLength(1)
  expect(network.aborts).toEqual([])
  expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
  expect(await visibleFailures(page)).toEqual([])
  expect(rendererErrors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('lost-ack-recovered-without-false-failure.png') })
  const evidencePath = testInfo.outputPath('lost-ack-recovery.json')
  await writeFile(evidencePath, JSON.stringify({ userAgent: await page.evaluate(() => navigator.userAgent),
    sends: network.sends, receipts: network.receipts,
    connections: network.connections(), failures: await visibleFailures(page),
    providerCalls: await isolatedRealGateway.readProviderCalls() }, null, 2))
  await testInfo.attach('lost-ack-recovery', { path: evidencePath, contentType: 'application/json' })
})
