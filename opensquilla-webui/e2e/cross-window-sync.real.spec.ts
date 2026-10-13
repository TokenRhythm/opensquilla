import { expect, test } from './real-gateway.fixture'

test('real Gateway synchronizes an observer user turn and both sidebar terminal states', async ({
  page: sender,
  context,
  isolatedRealGateway,
}, testInfo) => {
  test.setTimeout(120_000)
  const rendererErrors: string[] = []
  const watchErrors = (page: typeof sender) => {
    page.on('pageerror', error => rendererErrors.push(error.message))
  }
  watchErrors(sender)
  await context.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await sender.goto(`${isolatedRealGateway.controlUrl}chat/new`)
  await expect(sender.locator('.conn-pill.connected')).toBeVisible({ timeout: 20_000 })

  const firstPrompt = 'Complete a shared turn before testing observer synchronization.'
  const secondPrompt = 'This new user message must appear in the observing window before completion.'
  const firstReply = 'Task one completed after the lifecycle checks.'
  const calls = async () => (await isolatedRealGateway.readProviderCalls()).map(call => call.callNumber)
  const send = sender.locator('.chat-send-btn[aria-label="Send"]')
  const senderStop = sender.getByRole('button', { name: 'Stop current response', exact: true })

  await sender.locator('.chat-textarea').fill(firstPrompt)
  await send.click()
  await expect.poll(calls, { timeout: 30_000 }).toEqual([1])
  await expect(senderStop).toBeVisible()
  const sessionUrl = sender.url()
  const sessionKey = new URL(sessionUrl).searchParams.get('session')
  expect(sessionKey).toBeTruthy()

  // A second real page subscribes to the same production Gateway and SQLite
  // session. Neither page intercepts WebSockets or constructs event payloads.
  const observer = await context.newPage()
  watchErrors(observer)
  await observer.goto(sessionUrl)
  await expect(observer.locator('.conn-pill.connected')).toBeVisible({ timeout: 20_000 })
  await expect(observer.locator('.msg-user').filter({ hasText: firstPrompt })).toHaveCount(1)
  const pages = [sender, observer]
  const runningIndicator = (page: typeof sender) => page.locator(
    `[data-session-key="${sessionKey}"] .sidebar-task-attention--running`,
  )
  for (const page of pages) await expect(runningIndicator(page)).toBeVisible()

  // First prove successful completion clears the indicator in both windows.
  await isolatedRealGateway.releaseFirstTask()
  for (const page of pages) {
    await expect(page.locator('.msg-ai').filter({ hasText: firstReply }))
      .toBeVisible({ timeout: 30_000 })
    await expect(runningIndicator(page)).toHaveCount(0)
  }
  await expect(send).toBeEnabled()
  const documentOrigins = await Promise.all(pages.map(page => page.evaluate(() => performance.timeOrigin)))

  await sender.locator('.chat-textarea').fill(secondPrompt)
  await send.click()
  await expect.poll(calls, { timeout: 30_000 }).toEqual([1, 2])
  // The provider has not been released: this assertion cannot pass because a
  // terminal history refresh happened to recover the missing user message.
  await expect(observer.locator('.msg-user').filter({ hasText: secondPrompt }))
    .toHaveCount(1, { timeout: 10_000 })
  await expect(sender.locator('.msg-user').filter({ hasText: secondPrompt })).toHaveCount(1)
  for (const page of pages) {
    await expect(runningIndicator(page)).toBeVisible()
    await expect(page.locator('.msg-ai').filter({ hasText: firstReply })).toBeVisible()
  }
  expect((await isolatedRealGateway.readProviderEvents())
    .some(event => event.event === 'provider.released' && event.callNumber === 2)).toBe(false)
  await observer.screenshot({ path: testInfo.outputPath('observer-user-before-completion.png') })

  // Stop the held second task through the real UI/RPC path. Both independently
  // maintained sidebar directories must settle without a reload or navigation.
  await senderStop.click()
  for (const page of pages) {
    await expect(page.getByRole('button', { name: 'Stop current response', exact: true }))
      .toHaveCount(0, { timeout: 15_000 })
    await expect(runningIndicator(page)).toHaveCount(0)
    await expect(page.locator('.msg-user').filter({ hasText: secondPrompt })).toHaveCount(1)
    await expect(page.locator('.msg-ai').filter({ hasText: firstReply })).toBeVisible()
    expect(page.url()).toBe(sessionUrl)
  }
  expect(await Promise.all(pages.map(page => page.evaluate(() => performance.timeOrigin))))
    .toEqual(documentOrigins)
  await observer.screenshot({ path: testInfo.outputPath('observer-after-cancellation.png') })
  expect(rendererErrors).toEqual([])
})
