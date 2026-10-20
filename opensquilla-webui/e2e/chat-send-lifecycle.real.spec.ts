import { expect, test } from './real-gateway.fixture'
import { expectComposerValue } from './utils'
import { writeFile } from 'node:fs/promises'
import type { Page } from '@playwright/test'

async function prefillState(page: Page) {
  return page.evaluate(async () => {
    let component = (document.querySelector('.chat-textarea') as any)?.__vueParentComponent
    while (component) {
      const state = component.setupState
      if (state?.chatSessionBootstrap) return {
        sessionKey: state.sessionKey, identity: state.deliveryIdentity,
        liveReady: state.chatSessionBootstrap.isLiveReady(state.sessionKey),
        livePhase: state.livePhase, bootstrap: state.chatSessionBootstrap.currentSessionBootstrap()?.generation ?? null,
        readLeasePresent: Boolean(state.sessionReadLifecycle.current()),
        pendingAutoSend: state.pendingAutoSend, composerText: state.composerText,
        attachmentWorkBusy: state.attachmentWorkBusy,
        deliveries: state.durableDelivery.snapshots(),
        walRecords: await Promise.all(state.durableDelivery.snapshots()
          .map((item: { id: string }) => state.durableDelivery.get(item.id))),
      }
      component = component.parent
    }
    return null
  })
}

test('a new-task history prefill sends once after readiness and leaves an editable next draft', async ({ page, isolatedRealGateway }, testInfo) => {
  test.setTimeout(90_000)
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    history.replaceState({ ...history.state, prefill: 'Execute the prefilled task once.', autosend: true }, '')
  })
  const sends: string[] = []
  page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
    const frame = JSON.parse(String(payload))
    if (frame.method === 'chat.send' || frame.method === 'chat.send.v2') sends.push(frame.params.clientRequestId)
  }))
  await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
  await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length, { timeout: 30_000 }).toBe(1)
  await expect(page.getByRole('button', { name: 'Stop current response' })).toBeVisible()
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible()
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible()
  await expectComposerValue(page.locator('.chat-textarea'), '')
  expect(sends).toHaveLength(1)
  await page.locator('.chat-textarea').fill('A new draft stays editable.')
  await expectComposerValue(page.locator('.chat-textarea'), 'A new draft stays editable.')
  expect((await isolatedRealGateway.readProviderCalls()).length).toBe(1)
  const state = await prefillState(page)
  expect(state?.walRecords).toHaveLength(1)
  const record = state!.walRecords[0]
  expect(sends[0]).toBe(`tr2:${record.request.request.acceptanceEpoch}:${record.ownerRequestId}`)
  expect(record.request.request.params.clientRequestId).toBe(record.ownerRequestId)
  expect(state?.deliveries).toContainEqual(expect.objectContaining({
    id: record.ownerRequestId, phase: 'accepted', isCurrentIdentity: true,
  }))
  await writeFile(testInfo.outputPath('prefill-evidence.json'), JSON.stringify({
    sends, providerCalls: await isolatedRealGateway.readProviderCalls(), state: await prefillState(page),
  }, null, 2))
  await page.screenshot({ path: testInfo.outputPath('prefill-once.png') })
})

for (const editBeforeIdentity of [false, true]) {
  test(editBeforeIdentity
    ? 'editing a history prefill before first identity binding cancels automatic admission'
    : 'a history prefill waits for first identity binding then dispatches once', async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(120_000)
    const prompt = 'Keep this prefill until the first delivery identity is available.'
    const sends: Array<Record<string, unknown>> = []
    const reads: Array<{ method: string; id: string }> = []
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('websocket', socket => socket.on('framesent', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.method === 'chat.send' || frame.method === 'chat.send.v2') sends.push(frame.params)
      if (typeof frame.method === 'string' && /^sessions\.(subscribe|read)/.test(frame.method)) {
        reads.push({ method: frame.method, id: frame.id })
      }
    }))
    await page.addInitScript(prompt => {
      localStorage.setItem('opensquilla-locale', 'en')
      if (localStorage.getItem('prefill-identity-fixture-seeded')) return
      localStorage.setItem('prefill-identity-fixture-seeded', '1')
      history.replaceState({ ...history.state, prefill: prompt, autosend: true }, '')
      // Only schedule the native digest later. The real app derives the same
      // identity and retains its actual Hello/read/WAL/dispatch owners.
      const digest = crypto.subtle.digest.bind(crypto.subtle)
      let release!: () => void
      const gate = new Promise<void>(resolve => { release = resolve })
      Object.assign(window, { releasePrefillIdentityFixture: release })
      crypto.subtle.digest = async (...args) => {
        let deliveryTarget = false
        try {
          const value = JSON.parse(new TextDecoder().decode(args[1]))
          deliveryTarget = Array.isArray(value) && value.length === 2
            && typeof value[1] === 'string' && JSON.parse(value[1])?.[0] === 'browser'
        } catch {}
        if (deliveryTarget) await gate
        return digest(...args)
      }
    }, prompt)
    let before: unknown
    let beforeReload: Awaited<ReturnType<typeof prefillState>> = null
    try {
      await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
      await expect.poll(() => prefillState(page), { timeout: 30_000 }).toMatchObject({
        liveReady: true, identity: null, pendingAutoSend: prompt,
      })
      before = await prefillState(page)
      expect(sends).toEqual([])
      expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)
      const input = page.locator('.chat-textarea')
      const edited = 'My edited draft must remain unsent.'
      if (editBeforeIdentity) await input.fill(edited)
      await page.evaluate(() => (window as any).releasePrefillIdentityFixture())
      await expect.poll(async () => (await prefillState(page))?.identity, { timeout: 15_000 }).toBeTruthy()
      // The first identity also replaces the provisional read authority.
      // Admission still waits for that real subscription to install.
      await expect.poll(async () => (await prefillState(page))?.liveReady, { timeout: 30_000 }).toBe(true)
      if (editBeforeIdentity) {
        await expect.poll(async () => (await prefillState(page))?.pendingAutoSend).toBe('')
        await expectComposerValue(input, edited)
        await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeEnabled()
        expect(sends).toEqual([])
        expect((await isolatedRealGateway.readProviderCalls()).length).toBe(0)
      } else {
        await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
        expect(sends).toHaveLength(1)
        expect(sends[0]?.message).toBe(prompt)
        await isolatedRealGateway.releaseFirstTask()
        await expect(page.locator('.msg-ai').filter({ hasText: 'Task one completed after the lifecycle checks.' })).toBeVisible()
        await expectComposerValue(input, '')
      }
      beforeReload = await prefillState(page)
      if (editBeforeIdentity) expect(beforeReload?.walRecords).toEqual([])
      else {
        expect(beforeReload?.walRecords).toHaveLength(1)
        const record = beforeReload!.walRecords[0]
        expect(record.phase).toBe('accepted')
        expect(record.request.request.params.clientRequestId).toBe(record.ownerRequestId)
        expect(sends[0]?.clientRequestId).toBe(`tr2:${record.request.request.acceptanceEpoch}:${record.ownerRequestId}`)
      }
      await page.reload()
      await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 30_000 })
      await expect(input).toBeVisible()
      if (editBeforeIdentity) await expectComposerValue(input, edited)
      else await expect(page.locator('.msg-user').filter({ hasText: prompt })).toHaveCount(1)
      expect(sends).toHaveLength(editBeforeIdentity ? 0 : 1)
      expect((await isolatedRealGateway.readProviderCalls()).length).toBe(editBeforeIdentity ? 0 : 1)
      if (editBeforeIdentity) expect((await prefillState(page))?.deliveries).toEqual([])
      expect(errors).toEqual([])
      await page.screenshot({ path: testInfo.outputPath('prefill-after-identity.png') })
    } finally {
      await writeFile(testInfo.outputPath('prefill-identity-evidence.json'), JSON.stringify({
        before, beforeReload, after: await prefillState(page).catch(() => null), sends, reads, errors,
        providerCalls: await isolatedRealGateway.readProviderCalls(),
      }, null, 2))
    }
  })
}

test('real Gateway accepts the next chat turn without changing sessions', async ({
  page,
  isolatedRealGateway,
}) => {
  test.setTimeout(90_000)
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 20_000 })

  const input = page.locator('.chat-textarea')
  const send = page.locator('.chat-send-btn[aria-label="Send"]')
  const stop = page.getByRole('button', { name: 'Stop current response' })
  const callNumbers = async () => (await isolatedRealGateway.readProviderCalls())
    .map(call => call.callNumber)

  await input.fill('Complete the first synthetic chat turn.')
  await send.click()
  await expect.poll(callNumbers, { timeout: 30_000 }).toEqual([1])
  await expect(stop).toBeVisible()
  await input.fill('Continue with a second synthetic chat turn.')
  await isolatedRealGateway.releaseFirstTask()
  await expect(page.locator('.msg-ai')).toContainText(
    'Task one completed after the lifecycle checks.',
    { timeout: 30_000 },
  )
  await expect(stop).toHaveCount(0)
  await expect(send).toBeEnabled()
  await expectComposerValue(input, 'Continue with a second synthetic chat turn.')
  const sessionUrl = page.url()

  await send.click()
  await expect.poll(callNumbers, { timeout: 30_000 }).toEqual([1, 2])
  await expect(stop).toBeVisible()
  expect(page.url()).toBe(sessionUrl)
  expect((await isolatedRealGateway.readProviderCalls())[1])
    .toMatchObject({ firstReplyInAssistantHistory: true })
  await isolatedRealGateway.releaseSecondTask()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Task two completed after Goal removal.' }))
    .toBeVisible({ timeout: 30_000 })
  await expect(send).toBeEnabled()
  expect(rendererErrors).toEqual([])
})

test.describe('anonymous guest conversation', () => {
  test.use({ isolatedRealGatewayAuthMode: 'token' })

  test('subscribes and completes two turns in the same guest-owned session', async ({
    page, isolatedRealGateway,
  }, testInfo) => {
    test.setTimeout(90_000)
    const deniedSubscriptions: string[] = []
    const readResponses: Array<{ method: string; ok: boolean; code?: string }> = []
    page.on('websocket', socket => {
      const subscriptions = new Map<string, string>()
      socket.on('framesent', ({ payload }) => {
        const frame = JSON.parse(String(payload))
        if (frame.method === 'chat.send' || frame.method === 'chat.send.v2' || frame.method === 'sessions.messages.subscribe'
          || /^sessions\.read\..*\.v2$/.test(frame.method ?? '')
          || frame.method === 'sessions.history.page.v2') subscriptions.set(frame.id, frame.method)
      })
      socket.on('framereceived', ({ payload }) => {
        const frame = JSON.parse(String(payload))
        const method = subscriptions.get(frame.id)
        if (method) {
          readResponses.push({ method, ok: frame.ok, ...(frame.error ? { code: frame.error.code } : {}) })
          if (frame.error) deniedSubscriptions.push(frame.error.code)
        }
      })
    })
    try {
      await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
      await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
      const input = page.locator('.chat-textarea')
      const send = page.locator('.chat-send-btn[aria-label="Send"]')
      await input.fill('Complete the first synthetic guest turn.')
      await expect(send).toBeEnabled({ timeout: 20_000 })
      await send.click()
      await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(1)
      await isolatedRealGateway.releaseFirstTask()
      await expect(page.locator('.msg-ai')).toContainText('Task one completed after the lifecycle checks.')
      await expect(send).toBeEnabled()
      const session = new URL(page.url()).searchParams.get('session')
      expect(session).toMatch(/^agent:main:webchat:guest:[0-9a-f]{64}:[a-z0-9]+$/)
      await input.fill('Complete the second synthetic guest turn.')
      await send.click()
      await expect.poll(async () => (await isolatedRealGateway.readProviderCalls()).length).toBe(2)
      await isolatedRealGateway.releaseSecondTask()
      await expect(page.locator('.msg-ai').filter({ hasText: 'Task two completed after Goal removal.' })).toBeVisible()
      await expect(send).toBeEnabled()
      expect(new URL(page.url()).searchParams.get('session')).toBe(session)
      expect((await isolatedRealGateway.readProviderCalls())[1].firstReplyInAssistantHistory).toBe(true)
      expect(deniedSubscriptions).toEqual([])
      for (const method of ['sessions.read.open.v2', 'sessions.read.install.v2', 'sessions.history.page.v2']) {
        expect(readResponses.some(response => response.method === method && response.ok)).toBe(true)
      }
    } finally {
      await writeFile(testInfo.outputPath('guest-responses.json'), JSON.stringify(readResponses, null, 2))
      await testInfo.attach('guest-session-read-responses', {
        body: JSON.stringify(readResponses, null, 2), contentType: 'application/json',
      })
    }
  })
})
