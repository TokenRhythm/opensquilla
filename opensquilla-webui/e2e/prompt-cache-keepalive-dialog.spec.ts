import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-keepalive-contrast'
const KEEPALIVE_METHODS = [
  'sessions.promptCacheKeepalive.status',
  'sessions.promptCacheKeepalive.set',
]

type RpcFrame = {
  id?: string | number
  method?: string
  type?: string
}

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function channel(value: number): number {
  const normalized = value / 255
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4
}

function luminance(color: string): number {
  const match = color.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/)
  if (!match) throw new Error(`Unexpected computed color: ${color}`)
  return 0.2126 * channel(Number(match[1]))
    + 0.7152 * channel(Number(match[2]))
    + 0.0722 * channel(Number(match[3]))
}

function contrast(foreground: string, background: string): number {
  const lighter = Math.max(luminance(foreground), luminance(background))
  const darker = Math.min(luminance(foreground), luminance(background))
  return (lighter + 0.05) / (darker + 0.05)
}

async function installMockGateway(page: Page) {
  await page.route('**/api/system/update', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({}),
  }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ enabled: false }),
  }))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: RpcFrame
      try {
        frame = JSON.parse(String(message)) as RpcFrame
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')

      if (method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          features: { methods: KEEPALIVE_METHODS, events: [] },
          policy: { tick_interval_ms: 30000 },
          auth: {
            runModePolicy: {
              allowedRunModes: ['safe', 'full'],
              defaultRunMode: 'full',
            },
          },
        }))
        return
      }

      if (method === 'chat.history') {
        ws.send(response(frame.id, {
          messages: [{
            role: 'assistant',
            text: 'Synthetic keepalive settings conversation.',
            message_id: 'keepalive-contrast-message',
            timestamp: '2026-08-16T00:00:00Z',
          }],
          has_more: false,
          canonical_complete: true,
        }))
        return
      }

      if (method === 'sessions.messages.subscribe') {
        ws.send(response(frame.id, {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
          active_task: null,
        }))
        return
      }

      if (method === 'sessions.promptCacheKeepalive.status') {
        ws.send(response(frame.id, {
          enabled: false,
          ttlSeconds: 300,
          intervalSeconds: 240,
          idleTimeoutSeconds: 3600,
          idleExpiresAt: null,
          state: 'off',
          reason: null,
          hasSnapshot: false,
          lastCacheHitTokens: 0,
          provider: 'synthetic',
          model: 'synthetic-model',
        }))
        return
      }

      if (method === 'sandbox.run_mode.preference.get') {
        ws.send(response(frame.id, { runMode: 'full', source: 'config' }))
        return
      }

      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sandbox.capability.status': { available: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.unsubscribe': { subscribed: false },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })
}

async function openDisabledDialog(page: Page, theme: 'dark' | 'light') {
  await page.addInitScript(value => localStorage.setItem('opensquilla-theme', value), theme)
  await installMockGateway(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10000 })
  await expect(page.getByText('Synthetic keepalive settings conversation.')).toBeVisible()
  await page.locator('.chat-composer').getByRole('button', { name: 'More' }).click()
  await page.getByTestId('chat-composer-action-keepalive').click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.locator('.keepalive-dialog__timing')).toHaveClass(/is-disabled/)
  await page.waitForTimeout(250)
}

async function assertDisabledTooltipContrast(page: Page) {
  const timing = page.locator('.keepalive-dialog__timing')
  const inputs = timing.locator('input')
  const labels = timing.locator('.keepalive-dialog__field-label > label')
  const inputWraps = timing.locator('.keepalive-dialog__input-wrap')
  const helpButtons = timing.locator('.keepalive-dialog__field-help')

  await expect(inputs).toHaveCount(2)
  await expect(helpButtons).toHaveCount(2)
  for (let index = 0; index < 2; index += 1) {
    await expect(inputs.nth(index)).toBeDisabled()
    await expect(labels.nth(index)).toHaveCSS('opacity', '0.5')
    await expect(inputWraps.nth(index)).toHaveCSS('opacity', '0.5')

    const help = helpButtons.nth(index)
    const tooltip = help.locator('[role="tooltip"]')
    await help.hover()
    await expect(tooltip).toBeVisible()

    const hoverMetrics = await tooltip.evaluate(element => {
      const tooltipStyle = getComputedStyle(element)
      const helpElement = element.parentElement as HTMLElement
      const dialog = element.closest<HTMLElement>('.keepalive-dialog')!
      const ancestorOpacities: number[] = []
      let current: HTMLElement | null = element as HTMLElement
      while (current) {
        ancestorOpacities.push(Number.parseFloat(getComputedStyle(current).opacity))
        if (current === dialog) break
        current = current.parentElement
      }
      return {
        ancestorOpacities,
        helpColor: getComputedStyle(helpElement).color,
        surfaceColor: getComputedStyle(dialog).backgroundColor,
        tooltipBackground: tooltipStyle.backgroundColor,
        tooltipColor: tooltipStyle.color,
      }
    })

    expect(Math.min(...hoverMetrics.ancestorOpacities)).toBeGreaterThanOrEqual(0.999)
    expect(contrast(hoverMetrics.tooltipColor, hoverMetrics.tooltipBackground)).toBeGreaterThanOrEqual(4.5)
    expect(contrast(hoverMetrics.helpColor, hoverMetrics.surfaceColor)).toBeGreaterThanOrEqual(3)

    await page.mouse.move(1, 1)
    await help.focus()
    await expect(help).toBeFocused()
    await expect(tooltip).toBeVisible()
    const focusColors = await help.evaluate(element => ({
      color: getComputedStyle(element).color,
      surface: getComputedStyle(element.closest<HTMLElement>('.keepalive-dialog')!).backgroundColor,
    }))
    expect(contrast(focusColors.color, focusColors.surface)).toBeGreaterThanOrEqual(3)
  }
}

for (const theme of ['dark', 'light'] as const) {
  test(`disabled timing help stays readable in ${theme} theme`, async ({ page }) => {
    await openDisabledDialog(page, theme)
    await assertDisabledTooltipContrast(page)
  })
}

test.describe('narrow viewport', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('keeps disabled timing help readable below the 560px breakpoint', async ({ page }) => {
    await openDisabledDialog(page, 'light')
    expect(await page.locator('.keepalive-dialog__timing').evaluate(element => (
      getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).length
    ))).toBe(1)
    await assertDisabledTooltipContrast(page)
  })
})
