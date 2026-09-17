import { expect, test } from './real-gateway.fixture'

test('real Gateway accepts the next chat turn without changing sessions', async ({
  page,
  isolatedRealGateway,
}) => {
  test.setTimeout(90_000)
  const rendererErrors: string[] = []
  page.on('pageerror', error => rendererErrors.push(error.message))
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.goto(`${isolatedRealGateway.controlUrl}chat/new`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 20_000 })

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
  await expect(input).toHaveValue('Continue with a second synthetic chat turn.')
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
