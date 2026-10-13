import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_A = 'agent:main:webchat:web-browser-a'
const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

async function installWebBrowserUse(page: Page) {
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
}

test.describe('Web Browser Use', () => {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    test(`offers Browser Use only with a desktop browser bridge at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await installWebBrowserUse(page)
      const composer = page.locator('.chat-textarea')
      await composer.fill('Keep this composer draft')
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      const menu = page.getByRole('menu', { name: 'Add', exact: true })
      await expect(menu).toBeVisible()
      await expect(menu.getByRole('menuitem', { name: /^Browser Use\b/ })).toHaveCount(0)
      await expect(page.getByRole('dialog', { name: 'Browser Use' })).toHaveCount(0)
      await expect(composer).toHaveValue('Keep this composer draft')
    })
  }
})
