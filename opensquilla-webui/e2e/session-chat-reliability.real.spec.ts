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
