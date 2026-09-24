import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_A = 'agent:main:webchat:web-browser-a'
const SITE = 'https://browser-workbench.example.test'
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

async function installWebBrowserUse(page: Page) {
  const visits = new Map<string, number>()
  await page.addInitScript(() => {
    window.OPENSQUILLA_FEATURES = {
      ...(window.OPENSQUILLA_FEATURES || {}),
      artifactWorkbench: true,
    }
  })
  await page.route('**/api/**', route => route.fulfill({ status: 404 }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.route('**/opensquilla-mark.png', route => route.fulfill({
    contentType: 'image/png', body: PNG_1x1,
  }))
  // Context routing also covers the first navigation of an external window.
  await page.context().route(`${SITE}/**`, route => {
    const path = new URL(route.request().url()).pathname
    visits.set(path, (visits.get(path) || 0) + 1)
    return route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html lang="en"><meta charset="utf-8">
        <title>Browser fixture</title><h1>Browser fixture ${path}</h1>
        <label>Draft value <input name="draft"></label>
        <button type="button" id="increment">Increment</button>
        <output aria-label="Counter">0</output>
        <script>
          document.querySelector('#increment').onclick = () => {
            const counter = document.querySelector('output');
            counter.value = String(Number(counter.value) + 1);
          };
        </script></html>`,
    })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as Record<string, unknown>
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      if (method === 'connect') {
        ws.send(helloOkResponse({ auth: {
          principal: { isOwner: true, authenticated: true, authState: 'authenticated' },
          runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
        } }))
        return
      }
      const params = frame.params as Record<string, unknown> | undefined
      const key = String(params?.key || params?.sessionKey || SESSION_A)
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': {
          sessions: [{
            key: SESSION_A, title: 'Web browser task',
            sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct',
            effectiveAgentId: 'main', updatedAt: 1_800_000_000,
            messageCount: 0, status: 'ok', runStatus: 'idle',
          }],
          count: 1, ts: 1_800_000_000, has_more: false,
        },
        'chat.history': chatHistoryPayload(),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
        payload: payloads[method] ?? {} }))
    })
  })
  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION_A))
  await expect(page.locator('.conn-pill')).toBeVisible()
  await expect(page.getByTestId('topbar-workbench-toggle')).toHaveCount(0)
  await expect(page.getByTestId('workbench-host')).toBeHidden()
  expect(await page.evaluate(() => Boolean(window.opensquillaDesktop))).toBe(false)
  return visits
}

async function openBrowserFromComposer(page: Page) {
  const add = page.getByRole('button', { name: 'Add', exact: true })
  await add.click()
  const menu = page.getByRole('menu', { name: 'Add', exact: true })
  await menu.getByRole('menuitem', { name: /^Browser Use\b/ }).click()
  await expect(menu).toBeHidden()
  await expect(add).toHaveAttribute('aria-expanded', 'false')
}

test.describe('Web Browser Use', () => {
  test('opens a webpage in a separate window from Add without creating a sidebar or changing the draft', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const visits = await installWebBrowserUse(page)
    const composer = page.locator('.chat-textarea')
    const draft = 'Keep this composer draft'
    await composer.fill(draft)
    await openBrowserFromComposer(page)
    const dialog = page.getByRole('dialog', { name: 'Browser Use', exact: true })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('Webpages open in a new window.')
    const address = dialog.getByRole('textbox', { name: 'Web address' })
    await expect(address).toBeFocused()
    await address.fill('javascript:alert(1)')
    await dialog.getByRole('button', { name: 'Open externally', exact: true }).click()
    await expect(dialog.getByRole('alert')).toHaveText('Enter a valid HTTP or HTTPS web address.')
    await expect(address).toHaveAttribute('aria-invalid', 'true')
    expect(page.context().pages()).toHaveLength(1)
    expect(visits.size).toBe(0)

    await address.fill(`${SITE}/external`)
    const opened = page.waitForEvent('popup')
    await dialog.getByRole('button', { name: 'Open externally', exact: true }).click()
    const popup = await opened
    await expect(dialog).toBeHidden()
    await expect(popup).toHaveURL(`${SITE}/external`)
    await expect(popup.getByRole('heading')).toHaveText('Browser fixture /external')
    await popup.getByRole('button', { name: 'Increment' }).click()
    await expect(popup.getByLabel('Counter')).toHaveText('1')
    expect(await popup.evaluate(() => window.opener === null)).toBe(true)
    expect(await popup.evaluate(() => document.referrer)).toBe('')
    await popup.close()
    await expect(composer).toHaveValue(draft)
    await expect(page.getByTestId('topbar-workbench-toggle')).toHaveCount(0)
    await expect(page.getByTestId('workbench-host')).toBeHidden()
    await expect(page.locator('.browser-preview iframe')).toHaveCount(0)
  })

  test('keeps Browser Use available on mobile and cancels its dialog without opening a window', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const visits = await installWebBrowserUse(page)
    const composer = page.locator('.chat-textarea')
    await composer.fill('Mobile draft')
    await openBrowserFromComposer(page)
    const dialog = page.getByRole('dialog', { name: 'Browser Use', exact: true })
    const address = dialog.getByRole('textbox', { name: 'Web address' })
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    await address.fill(`${SITE}/cancelled`)
    await address.press('Escape')
    await expect(dialog).toBeHidden()
    await expect(composer).toHaveValue('Mobile draft')
    await openBrowserFromComposer(page)
    await expect(address).toHaveValue('')
    await dialog.getByRole('button', { name: 'Close', exact: true }).click()
    await expect(dialog).toBeHidden()
    await expect(composer).toHaveValue('Mobile draft')
    await expect(page.getByTestId('topbar-workbench-toggle')).toHaveCount(0)
    await expect(page.getByTestId('workbench-host')).toBeHidden()
    expect(page.context().pages()).toHaveLength(1)
    expect(visits.size).toBe(0)
  })
})
