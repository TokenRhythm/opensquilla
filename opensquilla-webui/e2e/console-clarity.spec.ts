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
      'Skills & Channels', 'Cron', 'View usage',
    ])
    await expect(page.locator('.sidebar-core').getByRole('link', { name: /^(Overview|Logs)$/ })).toHaveCount(0)
  })

  for (const path of ['overview', 'health']) {
    test(`/${path} compatibility link opens standalone Usage`, async ({ page }) => {
      await openControl(page, path)
      await expect(page).toHaveURL(/\/usage$/)
      await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible()
      await expect(page.locator('.route-hub__tabs, .ov-stage, .lg-stage')).toHaveCount(0)
    })
  }

  test('a cold /logs link focuses the on-demand log entry and closes without reopening Settings', async ({ page }) => {
    await openControl(page, 'logs')
    await expect(page).toHaveURL(/\/settings\/gateway#logs$/)
    const dialog = settingsDialog(page)
    await expect(dialog).toBeVisible()
    await expect(dialog.locator('#settings-gateway-logs')).toBeInViewport()
    await expect(dialog.locator('#settings-gateway-logs')).toBeFocused()
    await expect(dialog.getByTestId('support-download-bundle')).toBeVisible()
    await expect(dialog.getByTestId('support-view-logs')).toBeVisible()
    await expect(page.getByRole('dialog', { name: 'Gateway logs', exact: true })).toHaveCount(0)
    await expect(page.getByTestId('support-copy-readiness')).toHaveCount(0)
    await expect(page.locator('.lg-stage')).toHaveCount(0)

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(page).toHaveURL(/\/chat$/)
    await page.reload()
    await expect(dialog).toHaveCount(0)
    await expect(page.locator('.chat-textarea')).toBeVisible()
  })

  test('Usage and Gateway support stay within the target responsive viewports', async ({ page }) => {
    const scenarios = [
      { width: 320, height: 800, locale: 'zh-Hans', path: 'usage' },
      { width: 390, height: 844, locale: 'en', path: 'logs' },
      { width: 768, height: 900, locale: 'zh-Hans', path: 'logs' },
      { width: 1440, height: 1000, locale: 'en', path: 'usage' },
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

      if (scenario.path === 'logs') {
        await expect(page.getByTestId('support-download-bundle')).toBeInViewport()
      } else {
        await expect(page.locator('.route-hub__tabs')).toHaveCount(0)
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

  test('the sidebar Settings entry remains available from Usage', async ({ page }) => {
    await openControl(page, 'usage')
    await page.locator('.sidebar-foot .sidebar-fn-item').click()
    await expect(settingsDialog(page)).toBeVisible()
  })
})
