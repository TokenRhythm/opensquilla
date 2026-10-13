import { expect, test, type Page } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'
import { openTopbarSession } from './support/topbar-fixture'

const SUPPORT_PATH = '/control/settings/gateway#support'

function supportButton(page: Page) {
  return page.getByTestId('support-download-bundle')
}

function bundleDialog(page: Page) {
  return page.getByRole('dialog', { name: 'Download redacted support bundle', exact: true })
}

test('Gateway support downloads an explicit redacted bundle and resets the content opt-in', async ({ page }) => {
  await installSidebarFixture(page)
  const requests: unknown[] = []
  await page.route('**/api/v1/diagnostics/bundle', route => {
    requests.push(route.request().postDataJSON())
    return route.fulfill({
      contentType: 'application/zip',
      headers: { 'Content-Disposition': 'attachment; filename="synthetic-support.zip"' },
      // The transport fixture covers the browser download contract. ZIP content
      // and credential redaction remain covered by the backend bundle tests.
      body: Buffer.from([0x50, 0x4b, 0x05, 0x06, ...Array(18).fill(0)]),
    })
  })

  await page.goto(SUPPORT_PATH)
  await expect(supportButton(page)).toBeEnabled()
  await expect(page.locator('#settings-gateway-support')).toBeInViewport()
  await expect(page.getByTestId('support-diagnostics-trigger')).toHaveCount(0)
  await expect(page.getByTestId('support-copy-readiness')).toHaveCount(0)
  await expect(page.getByTestId('support-view-logs')).toBeVisible()
  await expect(page.getByTestId('support-open-local-logs')).toHaveCount(0)
  expect(requests).toEqual([])

  await supportButton(page).click()
  const dialog = bundleDialog(page)
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('checkbox')).not.toBeChecked()
  await expect(dialog.getByText('Known credential fields are redacted', { exact: true })).toBeVisible()
  await dialog.getByRole('checkbox').check()
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  expect(requests).toEqual([])

  await supportButton(page).click()
  await expect(dialog.getByRole('checkbox')).not.toBeChecked()
  const download = page.waitForEvent('download')
  await dialog.getByRole('button', { name: 'Generate and download', exact: true }).click()
  expect((await download).suggestedFilename()).toBe('synthetic-support.zip')
  expect(requests).toEqual([{ include_content: false, days: 1 }])
  await expect(dialog).toHaveCount(0)
  await expect(supportButton(page)).toBeFocused()
})

test('a remote non-owner sees the support boundary without local desktop log controls', async ({ page }) => {
  await installSidebarFixture(page, {}, {
    auth: { principal: { isOwner: false, scopes: ['operator.read'], capabilities: ['chat.read'] } },
  })
  const requests: string[] = []
  page.on('request', request => {
    if (request.url().endsWith('/api/v1/diagnostics/bundle')) requests.push(request.url())
  })
  await page.goto(SUPPORT_PATH)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(supportButton(page)).toBeVisible()
  await expect(supportButton(page)).toBeDisabled()
  await expect(page.locator('#settings-gateway-support')).toContainText(/owner/i)
  await expect(page.getByTestId('support-open-local-logs')).toHaveCount(0)
  await expect(page.locator('#settings-gateway-runtime')).toHaveCount(0)
  await expect(bundleDialog(page)).toHaveCount(0)
  expect(requests).toEqual([])
})

test('an owner connection to another Gateway cannot download from the WebUI origin', async ({ page }) => {
  let logReads = 0
  await installSidebarFixture(page, {
    'logs.tail': () => {
      logReads += 1
      return { lines: ['synthetic remote gateway log'], cursor: 1, has_more: false }
    },
  })
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla.wsUrl', 'ws://synthetic-remote.invalid/ws')
  })
  const requests: string[] = []
  page.on('request', request => {
    if (request.url().endsWith('/api/v1/diagnostics/bundle')) requests.push(request.url())
  })
  await page.goto(SUPPORT_PATH)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(page.locator('#conn-ws-url')).toHaveValue('ws://synthetic-remote.invalid/ws')
  await expect(supportButton(page)).toBeDisabled()
  await expect(page.locator('#support-bundle-unavailable')).toBeVisible()
  await expect(page.getByTestId('support-open-local-logs')).toHaveCount(0)
  expect(requests).toEqual([])
  // Log reads follow the connected WebSocket and do not download a ZIP from
  // the page origin. The remote connection keeps this on-demand action.
  expect(logReads).toBe(0)
  await page.getByTestId('support-view-logs').click()
  await expect(page.getByRole('dialog', { name: 'Gateway logs', exact: true }))
    .toContainText('synthetic remote gateway log')
  expect(logReads).toBe(1)
  expect(requests).toEqual([])
})

test('disconnected Gateway settings retain connection recovery while bundle download is unavailable', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.routeWebSocket(/\/ws$/, socket => {
    socket.close({ code: 1008, reason: 'synthetic offline gateway' })
  })
  await page.goto(SUPPORT_PATH)
  await expect(page.getByRole('dialog', { name: 'Settings', exact: true })).toBeVisible()
  await expect(supportButton(page)).toBeVisible()
  await expect(supportButton(page)).toBeDisabled()
  await expect(page.locator('#conn-ws-url')).toBeEditable()
  await expect(page.getByRole('button', { name: 'Connect', exact: true })).toBeEnabled()
  await expect(page.getByTestId('support-open-local-logs')).toHaveCount(0)
})

test('Desktop Gateway settings preserve native log reveal and keep the CLI handoff absent', async ({ page }) => {
  // Synthetic Desktop bridge coverage; native startup and actual log-file
  // reveal remain the responsibility of the Desktop acceptance suite.
  await openTopbarSession(page, { locale: 'en', update: {} })
  await page.goto(SUPPORT_PATH)
  const dialog = page.getByRole('dialog', { name: 'Settings', exact: true })
  await expect(dialog).toBeVisible()
  await expect(supportButton(page)).toBeEnabled()
  await expect(dialog.locator('#settings-gateway-runtime')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'CLI handoff', exact: true })).toHaveCount(0)
  const reveal = dialog.getByTestId('support-open-local-logs')
  await expect(dialog.locator('#settings-gateway-support').getByTestId('support-open-local-logs')).toBeVisible()
  await expect(dialog.locator('#settings-gateway-runtime').getByTestId('support-open-local-logs')).toHaveCount(0)
  await expect(reveal).toBeEnabled()
  await page.evaluate(() => {
    const bridge = window.opensquillaDesktop!
    const state = Object.assign(window, { __supportRevealCalls: 0 })
    bridge.revealGatewayLog = async () => {
      state.__supportRevealCalls += 1
      return true
    }
  })
  await reveal.click()
  expect(await page.evaluate(() => (
    (window as Window & { __supportRevealCalls?: number }).__supportRevealCalls
  ))).toBe(1)

  // Native log access belongs to the local Desktop process, independently of
  // the renderer's Gateway connection and the two network-backed support actions.
  const connectionActions = dialog.locator('#settings-connection-details')
  await expect(connectionActions).not.toHaveAttribute('open')
  await connectionActions.locator('summary').click()
  await connectionActions.getByRole('button', { name: 'Disconnect', exact: true }).click()
  await expect(dialog.getByTestId('support-view-logs')).toBeDisabled()
  await expect(supportButton(page)).toBeDisabled()
  await expect(reveal).toBeEnabled()
  await reveal.click()
  expect(await page.evaluate(() => (
    (window as Window & { __supportRevealCalls?: number }).__supportRevealCalls
  ))).toBe(2)
})

test('legacy diagnostic links do not resume readiness or log polling', async ({ page }) => {
  await page.clock.install()
  const diagnosticCalls: string[] = []
  const record = (method: string, payload: unknown) => () => {
    diagnosticCalls.push(method)
    return payload
  }
  await installSidebarFixture(page, {
    'doctor.status': record('doctor.status', { status: 'ready', ready: true, findings: [], agentId: 'main' }),
    'logs.status': record('logs.status', {
      gateway_file_log: {}, raw_turn_call_log: {}, diagnostics_enabled: {},
    }),
    'logs.tail': record('logs.tail', { lines: [], cursor: 0, has_more: false }),
  })
  for (const legacy of ['overview', 'health', 'logs']) {
    await page.goto(`/control/${legacy}`)
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page).toHaveURL(legacy === 'logs'
      ? /\/settings\/gateway#logs$/ : /\/usage$/)
    await page.clock.runFor(65_000)
    expect(diagnosticCalls).toEqual([])
  }
})

test('Gateway logs load once on open and once per refresh, without polling while open or closed', async ({ page }) => {
  await page.clock.install()
  const diagnosticCalls: string[] = []
  await installSidebarFixture(page, {
    'doctor.status': () => { diagnosticCalls.push('doctor.status'); return {} },
    'logs.status': () => { diagnosticCalls.push('logs.status'); return {} },
    'logs.tail': () => {
      diagnosticCalls.push('logs.tail')
      return { lines: ['synthetic on-demand gateway log'], cursor: 1, has_more: false }
    },
  })
  await page.goto('/control/logs')
  await expect(page).toHaveURL(/\/settings\/gateway#logs$/)
  const trigger = page.getByTestId('support-view-logs')
  await expect(trigger).toBeEnabled()
  await expect(page.locator('#settings-gateway-logs')).toBeFocused()
  await page.clock.runFor(65_000)
  expect(diagnosticCalls).toEqual([])

  await trigger.click()
  const dialog = page.getByRole('dialog', { name: 'Gateway logs', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('synthetic on-demand gateway log')
  expect(diagnosticCalls).toEqual(['logs.tail'])
  await page.clock.runFor(65_000)
  expect(diagnosticCalls).toEqual(['logs.tail'])

  await dialog.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect.poll(() => diagnosticCalls.length).toBe(2)
  expect(diagnosticCalls).toEqual(['logs.tail', 'logs.tail'])
  await dialog.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(trigger).toBeFocused()
  await page.clock.runFor(65_000)
  expect(diagnosticCalls).toEqual(['logs.tail', 'logs.tail'])
  await page.getByRole('dialog', { name: 'Settings', exact: true })
    .getByRole('button', { name: 'Close', exact: true }).click()
  await page.clock.runFor(1_000)
  await expect(page).toHaveURL(/\/chat$/)
})
