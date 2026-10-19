import type { Page } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { expect, test } from './real-gateway.fixture'
import { expectComposerValue } from './utils'

type Request = {
  type: string; id: string; method: string; params?: Record<string, unknown>
  observedConnection?: number; observedAt?: number
}
type Response = {
  type?: string; id?: string; ok?: boolean; payload?: Record<string, unknown>
  error?: { code?: string; accepted?: boolean | null }
}
const networks = new WeakMap<Page, Awaited<ReturnType<typeof loseFirstAcknowledgement>>>()

// Real production admission, workspace guards, receipt storage and execution.
// Only the response transport is faulty; no synthetic acceptance/retirement is
// supplied. Read-only lookup can be masked to keep a real accepted ACK unknown.
async function loseFirstAcknowledgement(page: Page, maskLookup: boolean) {
  const sends: Request[] = []
  const lookups: Request[] = []
  const resolves: Request[] = []
  const resolveResponses: Response[] = []
  const acknowledgements: Response[] = []
  const aborts: Request[] = []
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  let connections = 0
  let dropped = false
  await page.routeWebSocket(/\/ws$/, browser => {
    const observedConnection = ++connections
    const server = browser.connectToServer()
    const requests = new Map<string, Request>()
    browser.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'req') {
        requests.set(frame.id, frame)
        if (frame.method === 'chat.send') sends.push(frame)
        if (frame.method === 'turns.receipt.get') lookups.push({ ...frame, observedConnection, observedAt: Date.now() })
        if (frame.method === 'turns.receipt.resolve') resolves.push(frame)
        if (frame.method === 'chat.abort') aborts.push(frame)
      }
      server.send(raw)
    })
    server.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Response
      const request = frame.type === 'res' ? requests.get(String(frame.id)) : undefined
      if (request) requests.delete(request.id)
      if (request?.method === 'chat.send') {
        acknowledgements.push(frame)
        if (!dropped) {
          dropped = true
          browser.close({ code: 1012, reason: 'Synthetic lost first admission result' })
          server.close({ code: 1012, reason: 'Synthetic lost first admission result' })
          return
        }
      }
      if (request?.method === 'turns.receipt.resolve') resolveResponses.push(frame)
      if (maskLookup && request?.method === 'turns.receipt.get' && frame.ok) {
        browser.send(JSON.stringify({ ...frame, payload: { status: 'not_found', accepted: null } }))
        return
      }
      browser.send(raw)
    })
  })
  return { sends, lookups, resolves, resolveResponses, acknowledgements, aborts, pageErrors }
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.addInitScript(() => {
    // Observe the rendered UI across the fault/reload boundary. This test-only
    // key records transient banners without changing application storage.
    new MutationObserver(() => {
      const notice = document.querySelector('[data-testid="chat-session-recovery-status"]')
      if (!notice) return
      const key = 'e2e-session-notice-observations'
      const observed = JSON.parse(sessionStorage.getItem(key) || '[]') as unknown[]
      if (observed.length < 10) {
        observed.push({ path: location.pathname, text: notice.textContent })
        sessionStorage.setItem(key, JSON.stringify(observed))
      }
    }).observe(document, { childList: true, subtree: true })
  })
})

test.afterEach(async ({ page, isolatedRealGateway }, testInfo) => {
  const network = networks.get(page)
  if (!network) return
  const path = testInfo.outputPath('unknown-delivery-network.json')
  await writeFile(path, JSON.stringify({ ...network,
    providerCalls: await isolatedRealGateway.readProviderCalls(),
    sessionNoticeObservations: await page.evaluate(() =>
      JSON.parse(sessionStorage.getItem('e2e-session-notice-observations') || '[]')).catch(() => []),
  }, null, 2))
  await testInfo.attach('unknown-delivery-network', { path, contentType: 'application/json' })
})

async function expectNoSessionNoticeObserved(page: Page) {
  expect(await page.evaluate(() =>
    JSON.parse(sessionStorage.getItem('e2e-session-notice-observations') || '[]'))).toEqual([])
}

async function waitForExhaustedQuietRecovery(page: Page, network: Awaited<ReturnType<typeof loseFirstAcknowledgement>>) {
  const check = page.getByTestId('chat-composer-recovery')
  await expect.poll(() => network.lookups.length, { timeout: 30_000 }).toBeGreaterThanOrEqual(4)
  await expect(check).toBeEnabled({ timeout: 30_000 })
  await expect(check.locator('.loading-spinner')).toHaveCount(0)
  await page.waitForTimeout(6_000)
  const reads = network.lookups.length
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'))
    document.dispatchEvent(new Event('visibilitychange'))
    window.dispatchEvent(new Event('online'))
  })
  await page.waitForTimeout(6_000)
  expect(network.lookups).toHaveLength(reads)
  expect(network.resolves).toHaveLength(0)
  expect(network.sends).toHaveLength(1)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(page.locator('.msg-user .loading-spinner')).toHaveCount(0)
  return check
}

test.describe('pre-acceptance workspace failure', () => {
  test.use({ isolatedRealGatewayWorkspaceInitiallyMissing: true })

  test('lost rejection is safely retired before an explicit fresh request executes once', async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(120_000)
    const network = await loseFirstAcknowledgement(page, false)
    networks.set(page, network)
    await page.goto(`${isolatedRealGateway.controlUrl}chat/new?agent=main`)
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    const message = 'Keep this message until it can be accepted exactly once.'
    await input.fill(message)
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.acknowledgements.length, { timeout: 30_000 }).toBe(1)
    expect(network.acknowledgements[0]).toMatchObject({
      ok: false, error: { code: 'WORKSPACE_UNAVAILABLE', accepted: false },
    })
    expect(await isolatedRealGateway.readProviderCalls()).toHaveLength(0)
    const check = await waitForExhaustedQuietRecovery(page, network)
    expect(network.lookups).toHaveLength(4)
    await isolatedRealGateway.createOwnedWorkspace()
    await check.click()
    await expect.poll(() => network.resolveResponses.length).toBe(1)
    expect(network.resolveResponses[0]).toMatchObject({
      ok: true, payload: { status: 'retired', accepted: false },
    })
    expect(network.resolves[0]!.params).toEqual({
      operation: 'chat.send', originalRequest: network.sends[0]!.params,
    })
    await expectComposerValue(input, message)
    await expect(send).toBeEnabled()
    expect(network.sends).toHaveLength(1)
    expect(await isolatedRealGateway.readProviderCalls()).toHaveLength(0)
    await page.screenshot({ path: testInfo.outputPath('retired-draft-ready.png') })
    await send.click()
    await expect.poll(() => network.sends.length).toBe(2)
    expect(network.sends[1]!.params!.clientRequestId).not.toBe(network.sends[0]!.params!.clientRequestId)
    expect(network.sends[1]!.params!.message).toBe(message)
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
    await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
      .toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
    expect(network.sends).toHaveLength(2)
    expect(network.aborts).toHaveLength(0)
    expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
    expect(network.pageErrors).toHaveLength(0)
    await expectNoSessionNoticeObserved(page)
    await page.screenshot({ path: testInfo.outputPath('retired-replacement-completed.png') })
  })

  test('missing original session after reload still offers safe recovery and a fresh send', async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(150_000)
    const network = await loseFirstAcknowledgement(page, false)
    networks.set(page, network)
    await page.goto(`${isolatedRealGateway.controlUrl}chat/new?agent=main`)
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    const message = 'Restore this first message even when its original session never existed.'
    await input.fill(message)
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.acknowledgements.length, { timeout: 30_000 }).toBe(1)
    expect(network.acknowledgements[0]).toMatchObject({
      ok: false, error: { code: 'WORKSPACE_UNAVAILABLE', accepted: false },
    })
    await waitForExhaustedQuietRecovery(page, network)
    const originalSessionKey = String(network.sends[0]!.params!.sessionKey)
    // A document navigation restores the unknown WAL against the exact absent
    // session URL, matching reopening the failed session rather than /chat/new.
    await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(originalSessionKey)}`)
    await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(originalSessionKey)
    const check = await waitForExhaustedQuietRecovery(page, network)
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
    expect(await isolatedRealGateway.readProviderCalls()).toHaveLength(0)
    await isolatedRealGateway.createOwnedWorkspace()
    await page.screenshot({ path: testInfo.outputPath('missing-session-reload-check.png') })
    await check.click()
    await expect.poll(() => network.resolveResponses.length, { timeout: 30_000 }).toBe(1)
    expect(network.resolveResponses[0]).toMatchObject({
      ok: true, payload: { status: 'retired', accepted: false },
    })
    await expectComposerValue(input, message)
    await expect(send).toBeEnabled()
    expect(network.sends).toHaveLength(1)
    expect(await isolatedRealGateway.readProviderCalls()).toHaveLength(0)
    await send.click()
    await expect.poll(() => network.acknowledgements.length, { timeout: 30_000 }).toBe(2)
    expect(network.acknowledgements[1]).toMatchObject({ ok: true, payload: { accepted: true } })
    expect(network.sends[1]!.params!.clientRequestId).not.toBe(network.sends[0]!.params!.clientRequestId)
    expect(network.sends[1]!.params!.sessionKey).not.toBe(originalSessionKey)
    expect(network.sends[1]!.params!.message).toBe(message)
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
    await expect(page.locator('.chat-send-btn .loading-spinner, .msg-user .loading-spinner')).toHaveCount(0)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
      .toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
    expect(network.sends).toHaveLength(2)
    expect(network.aborts).toHaveLength(0)
    expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
    expect(network.pageErrors).toHaveLength(0)
    await expectNoSessionNoticeObserved(page)
    await page.screenshot({ path: testInfo.outputPath('missing-session-reload-recovered.png') })
  })

  test('retirement survives reload before the missing-session route handoff', async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(150_000)
    const network = await loseFirstAcknowledgement(page, false)
    networks.set(page, network)
    await page.goto(`${isolatedRealGateway.controlUrl}chat/new?agent=main`)
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    const message = 'Keep this draft across a reload after retirement but before route handoff.'
    const attachmentText = 'Retain these attachment bytes until the replacement request is accepted.'
    await input.fill(message)
    await page.locator('input[type="file"]').setInputFiles({
      name: 'retirement-draft.txt', mimeType: 'text/plain', buffer: Buffer.from(attachmentText),
    })
    await expect(page.locator('.attachment-chip')).toContainText('retirement-draft.txt')
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
    await expect(send).toBeEnabled()
    await send.click()
    await expect.poll(() => network.acknowledgements.length, { timeout: 30_000 }).toBe(1)
    expect(network.acknowledgements[0]).toMatchObject({
      ok: false, error: { code: 'WORKSPACE_UNAVAILABLE', accepted: false },
    })
    await waitForExhaustedQuietRecovery(page, network)
    const originalSessionKey = String(network.sends[0]!.params!.sessionKey)
    await page.goto(`${isolatedRealGateway.controlUrl}chat?session=${encodeURIComponent(originalSessionKey)}`)
    const check = await waitForExhaustedQuietRecovery(page, network)
    await isolatedRealGateway.createOwnedWorkspace()
    // Interrupt ordinary browser navigation at the point the application has
    // authoritative retirement proof and is about to move to a fresh draft.
    // Never edit application storage or synthesize a retirement result.
    await page.evaluate(() => {
      const original = history.replaceState.bind(history)
      history.replaceState = (data, unused, url) => {
        if (url && new URL(String(url), location.href).pathname.endsWith('/chat/new')
          && !sessionStorage.getItem('e2e-retirement-route-reload')) {
          sessionStorage.setItem('e2e-retirement-route-reload', '1')
          location.reload()
          return
        }
        original(data, unused, url)
      }
    })
    page.on('dialog', async dialog => {
      if (dialog.type() === 'beforeunload') await dialog.accept()
      else await dialog.dismiss()
    })
    await check.click()
    await expect.poll(() => network.resolveResponses.length, { timeout: 30_000 }).toBe(1)
    expect(network.resolveResponses[0]).toMatchObject({
      ok: true, payload: { status: 'retired', accepted: false },
    })
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('e2e-retirement-route-reload'))
      .catch(() => null), { timeout: 30_000 }).toBe('1')
    await page.waitForURL(url => url.pathname.endsWith('/chat/new'), { timeout: 30_000 })
    expect(await page.evaluate(() => {
      const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
      return navigation?.type
    })).toBe('reload')
    await expectComposerValue(input, message)
    await expect(page.locator('.attachment-chip')).toContainText('retirement-draft.txt')
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
    await expect(send).toBeEnabled()
    expect(network.sends).toHaveLength(1)
    expect(network.resolves).toHaveLength(1)
    expect(await isolatedRealGateway.readProviderCalls()).toHaveLength(0)
    await page.screenshot({ path: testInfo.outputPath('retired-crash-window-draft-restored.png') })
    await send.click()
    await expect.poll(() => network.acknowledgements.length, { timeout: 30_000 }).toBe(2)
    expect(network.acknowledgements[1]).toMatchObject({ ok: true, payload: { accepted: true } })
    expect(network.sends[1]!.params!.clientRequestId).not.toBe(network.sends[0]!.params!.clientRequestId)
    expect(network.sends[1]!.params!.sessionKey).not.toBe(originalSessionKey)
    expect(network.sends[1]!.params!.message).toBe(message)
    const attachments = network.sends[1]!.params!.attachments as Array<Record<string, unknown>>
    expect(attachments).toHaveLength(1)
    expect(attachments[0]).toMatchObject({ name: 'retirement-draft.txt', mime: 'text/plain' })
    expect(Buffer.from(String(attachments[0]!.data), 'base64').toString()).toBe(attachmentText)
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toBeEnabled()
    await expect(page.locator('.chat-send-btn .loading-spinner, .msg-user .loading-spinner')).toHaveCount(0)
    await isolatedRealGateway.releaseFirstTask()
    await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
      .toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true })).toHaveCount(0)
    expect(network.sends).toHaveLength(2)
    expect(network.aborts).toHaveLength(0)
    expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
    expect(network.pageErrors).toHaveLength(0)
    await expectNoSessionNoticeObserved(page)
    await page.screenshot({ path: testInfo.outputPath('retired-crash-window-send-completed.png') })
  })
})

test('lost accepted acknowledgement is found by explicit checking without a second send', async ({
  page, isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  const network = await loseFirstAcknowledgement(page, true)
  networks.set(page, network)
  await page.goto(`${isolatedRealGateway.controlUrl}chat/new?agent=main`)
  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  await input.fill('This request is accepted even when its acknowledgement is lost.')
  await expect(send).toBeEnabled()
  await send.click()
  await expect.poll(() => network.acknowledgements[0]?.ok, { timeout: 30_000 }).toBe(true)
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' }))
    .toBeVisible({ timeout: 30_000 })
  const draft = 'The next draft must survive resolving the previous accepted request.'
  await input.fill(draft)
  const check = await waitForExhaustedQuietRecovery(page, network)
  await check.click()
  await expect.poll(() => network.resolveResponses.length).toBe(1)
  expect(network.resolveResponses[0]).toMatchObject({
    ok: true, payload: { status: 'found', accepted: true },
  })
  expect(network.resolves[0]!.params).toEqual({
    operation: 'chat.send', originalRequest: network.sends[0]!.params,
  })
  await expectComposerValue(input, draft)
  await expect(send).toBeEnabled()
  await expect(check).toHaveCount(0)
  await expect(page.locator('.chat-send-btn .loading-spinner')).toHaveCount(0)
  expect(network.sends).toHaveLength(1)
  expect(network.aborts).toHaveLength(0)
  expect((await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)).toEqual([1])
  expect(network.pageErrors).toHaveLength(0)
  await expectNoSessionNoticeObserved(page)
  await page.screenshot({ path: testInfo.outputPath('accepted-found-without-resend.png') })
})
