import { expect, test, type Locator, type Page } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { MAIN, SIDE, installGateway, logs } from './support/real-conversation-gateway'
import { expectComposerValue } from './utils'

const composer = (host: Locator) => host.getByRole('textbox', { name: 'Message to send', exact: true })
test.afterEach(async ({ page }, info) => {
  if (info.status === info.expectedStatus) return
  const path = info.outputPath('gateway-and-drafts.json')
  await writeFile(path, JSON.stringify({
    gateway: logs.get(page), drafts: await page.getByRole('textbox', { name: 'Message to send', exact: true }).evaluateAll(elements =>
      elements.map(element => ({ text: element.textContent, html: element.innerHTML }))),
  }, null, 2))
  await info.attach('gateway-and-drafts', { contentType: 'application/json', path })
})
async function openMain(page: Page) {
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto(`/control/chat?session=${MAIN}`)
  const main = page.locator('#app-main .chat').first()
  await expect(main).toContainText('Earlier main answer')
  await expect(composer(main)).toHaveAttribute('contenteditable', 'true')
  return main
}
async function submit(main: Locator, text: string) {
  await composer(main).fill(text)
  await main.getByRole('button', { name: 'Send', exact: true }).click()
}

test('bare /side opens and focuses a temporary chat without materializing or sending', async ({ page }) => {
  const gateway = await installGateway(page)
  const main = await openMain(page)
  await submit(main, '/side')
  const side = page.getByTestId('assistant-chat-panel')
  await expect(side.getByTestId('temporary-conversation-empty')).toBeVisible()
  await expect(composer(side)).toBeFocused()
  await expectComposerValue(composer(main), '')
  await expectComposerValue(composer(side), '')
  expect(gateway.requests.filter(request => ['chat.send', 'sessions.sidechat.create'].includes(request.method))).toEqual([])
  expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
  expect(gateway.errors).toEqual([])
})

test('busy parent /btw sends only the child and leaves the parent task and queue unchanged', async ({ page }) => {
  const gateway = await installGateway(page)
  const main = await openMain(page)
  await submit(main, 'Keep the parent task running')
  await expect(main.locator('.chat-stop-btn')).toBeEnabled()
  await composer(main).fill('/btw Inspect this in the side')
  await expectComposerValue(composer(main), '/btw Inspect this in the side')
  await composer(main).press('Enter')
  const side = page.getByTestId('assistant-chat-panel')
  await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
  const sends = gateway.requests.filter(request => request.method === 'chat.send')
  expect(sends.map(request => [request.params?.sessionKey, request.params?.message])).toEqual([
    [MAIN, 'Keep the parent task running'], [SIDE, 'Inspect this in the side'],
  ])
  expect(sends[1]?.params?.queueMode).not.toBe('steer')
  await expectComposerValue(composer(main), '')
  await expect(main.locator('.chat-stop-btn')).toBeEnabled()
  await expect(side.locator('.chat-stop-btn')).toBeEnabled()
  gateway.finish(SIDE, 'Side-only answer')
  await expect(side).toContainText('Side-only answer')
  await expect(main).not.toContainText('Side-only answer')
  gateway.finish(MAIN, 'Parent completes its original work')
  await expect(main).toContainText('Parent completes its original work')
  await expect(main.locator('.chat-stop-btn')).toHaveCount(0)
  // Finishing the parent also proves the command did not leave an unsent
  // queued main turn that would drain as soon as the active task completed.
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
  expect(gateway.requests.filter(request => request.method === 'chat.abort')).toHaveLength(0)
  expect(gateway.errors).toEqual([])
})

test('failed side creation retains the parent command and retries the same tab and owner', async ({ page }) => {
  const gateway = await installGateway(page)
  gateway.failNextSideCreate()
  const main = await openMain(page)
  await submit(main, '/side Retry this child')
  const side = page.getByTestId('assistant-chat-panel')
  await expect(side.getByRole('alert')).toContainText('Synthetic side creation failure')
  await expectComposerValue(composer(main), '/side Retry this child')
  await expectComposerValue(composer(side), 'Retry this child')
  const tabs = page.getByTestId('workbench-host').getByRole('tab')
  await expect(tabs).toHaveCount(1)
  const originalTab = await tabs.first().getAttribute('aria-controls')
  await main.getByRole('button', { name: 'Send', exact: true }).click()
  await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
  await expectComposerValue(composer(main), '')
  await expect(tabs).toHaveCount(1)
  await expect(tabs.first()).toHaveAttribute('aria-controls', originalTab!)
  const creates = gateway.requests.filter(request => request.method === 'sessions.sidechat.create')
  expect(creates).toHaveLength(2)
  expect(creates[1]?.params).toEqual(creates[0]?.params)
  expect(gateway.requests.find(request => request.method === 'chat.send')?.params?.sessionKey).toBe(SIDE)
  expect(gateway.errors).toEqual([])
})

test('editing the child while creation is pending keeps both drafts and cancels the automatic send', async ({ page }) => {
  const gateway = await installGateway(page, false, false, true)
  const main = await openMain(page)
  await submit(main, '/btw Original side question')
  const side = page.getByTestId('assistant-chat-panel')
  await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.create').length).toBe(1)
  await composer(side).fill('Edited before creation finished')
  gateway.releaseCreate()
  // The launch promise has completed, not merely started history I/O. This
  // fences late bootstrap continuations before asserting no automatic send.
  await expect(page.locator('.toast')).toContainText('The side chat has not confirmed this message')
  await expectComposerValue(composer(main), '/btw Original side question')
  await expectComposerValue(composer(side), 'Edited before creation finished')
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toEqual([])
  expect(gateway.errors).toEqual([])
})

test('manual child retry after known rejection retires the parent command without a duplicate send', async ({ page }) => {
  const gateway = await installGateway(page)
  gateway.rejectNextSend(SIDE)
  const main = await openMain(page)
  await submit(main, '/side Deliver exactly once')
  const side = page.getByTestId('assistant-chat-panel')
  await expect(page.locator('.toast').filter({ hasText: 'The side chat has not confirmed this message' })).toBeVisible()
  await expectComposerValue(composer(main), '/side Deliver exactly once')
  await expectComposerValue(composer(side), 'Deliver exactly once')
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(1)
  const tabs = page.getByTestId('workbench-host').getByRole('tab')
  await expect(tabs).toHaveCount(1)
  const originalTab = await tabs.first().getAttribute('aria-controls')

  // The child owns this retry: it has no invocation-specific callback from
  // the parent. The shared sender's durable acceptance observer must retire
  // the original handoff when this ordinary Send succeeds.
  await side.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(side.locator('.chat-stop-btn')).toBeEnabled()
  await expectComposerValue(composer(side), '')
  await expectComposerValue(composer(main), '/side Deliver exactly once')
  await main.getByRole('button', { name: 'Send', exact: true }).click()
  await expectComposerValue(composer(main), '')
  await expect(tabs).toHaveCount(1)
  await expect(tabs.first()).toHaveAttribute('aria-controls', originalTab!)
  expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
  const sends = gateway.requests.filter(request => request.method === 'chat.send')
  expect(sends).toHaveLength(2) // one rejected request, one accepted manual retry
  expect(sends.every(request => request.params?.sessionKey === SIDE
    && request.params?.message === 'Deliver exactly once')).toBe(true)
  gateway.finish(SIDE, 'Only one accepted reply')
  await expect(side).toContainText('Only one accepted reply')
  await expect(main).not.toContainText('Only one accepted reply')
  expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
  expect(gateway.errors).toEqual([])
})
