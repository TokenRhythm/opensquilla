import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'

async function openControl(page: Page, path = '') {
  await page.goto(CONTROL_URL + path)
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
}

const settingsDialog = (page: Page) => page.getByRole('dialog', { name: 'Settings' })

test.describe('Console clarity', () => {
  // The DEV-only parts/fold parity check logs `[live-turn parity]` on any
  // fold/key divergence between message.parts and the rendered timeline. Treat
  // it as a hard failure so a regression is caught in CI, not eyeballed.
  let parityErrors: string[]

  test.beforeEach(({ page }) => {
    parityErrors = []
    page.on('console', msg => {
      if (msg.type() === 'error' && msg.text().includes('[live-turn parity]')) {
        parityErrors.push(msg.text())
      }
    })
  })

  test.afterEach(() => {
    expect(parityErrors, 'live-turn parts/fold parity check reported a divergence').toEqual([])
  })

  test('flat navigation removes the Console fold and keeps Settings distinct', async ({ page }) => {
    await openControl(page)

    const settingsRow = page.locator('.sidebar-foot .sidebar-fn-item')
    await expect(settingsRow).toHaveAttribute('data-icon', 'settings')
    await expect(page.locator('.sidebar-nav-group-toggle')).toHaveCount(0)
    await expect(page.locator('.sidebar-core .sidebar-fn-label')).toHaveText([
      'Sessions', 'Overview', 'Skills & Channels', 'Cron',
    ])
  })

  test('/health deep link redirects to /overview with the readiness report inline', async ({ page }) => {
    await openControl(page, 'health')

    await expect(page).toHaveURL(/\/overview$/)
    await expect(page.locator('#overview-health')).toBeVisible()
    await expect(page.locator('section[aria-label="Health findings"]')).toBeVisible()
  })

  test('Overview stays focused on status and routes runtime logs through diagnostics', async ({ page }) => {
    await openControl(page, 'overview')

    const hub = page.getByRole('navigation', { name: 'Overview' })
    await expect(hub.getByRole('link')).toHaveText(['Status', 'Usage'])
    await expect(page.locator('.ov-grid')).toHaveCount(0)
    await expect(page.locator('.ov-recent')).toHaveCount(0)
    await expect(page.locator('.ov-event-log')).toHaveCount(0)

    await page.getByRole('button', { name: 'Support & diagnostics' }).click()
    await page.getByRole('menuitem', { name: /View runtime logs/ }).click()
    await expect(page).toHaveURL(/\/logs$/)
    await expect(page.getByRole('heading', { name: 'Logs', level: 1 })).toBeVisible()
    await expect(page.locator('.sidebar-core').getByRole('link', { name: 'Overview' }))
      .toHaveClass(/is-active/)

    const breadcrumb = page.getByRole('navigation', { name: 'Breadcrumb' })
    await expect(breadcrumb.getByRole('link', { name: 'Overview', exact: true }))
      .toHaveAttribute('href', '/control/overview')
    await breadcrumb.getByRole('link', { name: 'Overview', exact: true }).click()
    await expect(page).toHaveURL(/\/overview$/)
    await page.goBack()
    await expect(page).toHaveURL(/\/logs$/)

    await page.getByRole('button', { name: 'Support & diagnostics' }).click()
    await expect(page.getByRole('menuitem', { name: /View runtime logs/ })).toHaveCount(0)
    await page.goBack()
    await expect(page).toHaveURL(/\/overview$/)
  })

  test('Overview and Logs stay within the target responsive viewports', async ({ page }) => {
    const scenarios = [
      { width: 320, height: 800, locale: 'zh-Hans', path: 'overview' },
      { width: 390, height: 844, locale: 'en', path: 'logs' },
      { width: 768, height: 900, locale: 'zh-Hans', path: 'logs' },
      { width: 1440, height: 1000, locale: 'en', path: 'overview' },
    ] as const

    for (const scenario of scenarios) {
      await page.setViewportSize({ width: scenario.width, height: scenario.height })
      await page.goto(CONTROL_URL)
      await page.evaluate((locale) => {
        localStorage.setItem('opensquilla-locale', locale)
      }, scenario.locale)
      await openControl(page, scenario.path)

      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth)
      expect(overflow).toBeLessThanOrEqual(0)

      if (scenario.path === 'overview' && scenario.width <= 320) {
        const tabs = await page.locator('.route-hub__tabs').boundingBox()
        const actions = await page.locator('.route-hub__actions').boundingBox()
        expect(tabs).not.toBeNull()
        expect(actions).not.toBeNull()
        expect(tabs!.x + tabs!.width).toBeLessThanOrEqual(actions!.x)
      }
    }
  })

  test('Channels aligns its refresh action with the hub tabs only on desktop', async ({ page }) => {
    for (const scenario of [
      { width: 900, height: 800, inline: true },
      { width: 390, height: 844, inline: false },
    ]) {
      await page.setViewportSize({ width: scenario.width, height: scenario.height })
      await openControl(page, 'channels')

      const tabs = await page.locator('.route-hub__tabs').boundingBox()
      const actions = await page.locator('.ch-stage__actions').boundingBox()
      expect(tabs).not.toBeNull()
      expect(actions).not.toBeNull()

      if (scenario.inline) {
        const tabsCenter = tabs!.y + tabs!.height / 2
        const actionsCenter = actions!.y + actions!.height / 2
        expect(Math.abs(tabsCenter - actionsCenter)).toBeLessThanOrEqual(2)
      } else {
        expect(actions!.y).toBeGreaterThanOrEqual(tabs!.y + tabs!.height)
      }

      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth)
      expect(overflow).toBeLessThanOrEqual(0)
    }
  })

  test('Status impact count jumps to the inline readiness report', async ({ page }) => {
    await openControl(page, 'overview')

    await page.locator('.ov-count').first().click()
    // Still on Overview — the card scrolls instead of navigating away.
    await expect(page).toHaveURL(/\/overview$/)
    await expect(page.locator('#overview-health')).toBeInViewport()
  })

  test('Agents header link opens the Settings modal', async ({ page }) => {
    await openControl(page, 'agents')

    await page.locator('.ag-stage__actions').getByRole('button', { name: 'open settings' }).click()
    await expect(settingsDialog(page)).toBeVisible()
  })
})
