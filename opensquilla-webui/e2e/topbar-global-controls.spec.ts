import { expect, test } from '@playwright/test'
import {
  expectTopbarConsoleClean,
  expectTopbarGeometry,
  openTopbarSession,
  TOPBAR_SESSION_KEY,
  type TopbarScenario,
} from './support/topbar-fixture'

test.afterEach(({ page }) => {
  expectTopbarConsoleClean(page)
})

test.describe('Chat topbar global controls', () => {
  test('non-chat stays quiet while language and themes remain reachable in Settings', async ({ page }) => {
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-non-chat`, locale: 'en',
    })
    await page.goto('/control/usage')
    await expect(page.getByTestId('route-header-host').locator('.chat-header')).toHaveCount(0)
    await expect(page.locator('.topbar').getByRole('link', { name: /^(Overview|Logs)$/ })).toHaveCount(0)
    await expect(page.getByTestId('language-switcher-trigger')).toHaveCount(0)
    await expect(page.locator('.topbar .theme-menu-wrap')).toHaveCount(0)
    await expect(page.getByTestId('connection-status')).toHaveCount(0)

    await page.locator('.sidebar-foot button[data-icon="settings"]').click()
    await page.locator('#settings-rail-general').click()
    await expect(page.getByTestId('settings-language-group').getByRole('radio')).toHaveCount(6)
    await expect(page.getByTestId('settings-language-en')).toBeChecked()
    await page.locator('#settings-rail-interface').click()
    await expect(page.locator('input[name="appearance-theme"]')).toHaveCount(10)
    await expect(page.locator('.settings-modal')).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(page.locator('.settings-modal')).toHaveCount(0)
    await expect(page.locator('.sidebar-foot button[data-icon="settings"]')).toBeFocused()
  })

  test('web has no desktop update entry and wide approval stays direct', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-web-wide`, locale: 'en', approvalCount: 1,
    })
    await expect(page.getByTestId('chat-system-status')).toHaveAttribute('data-layout', 'wide')
    await expect(page.getByTestId('connection-status')).toHaveCount(0)
    await expect(page.getByTestId('chat-system-approval')).toHaveCount(1)
    await expect(page.getByTestId('chat-system-approval')).toBeVisible()
    await expect(page.getByTestId('sidebar-desktop-update')).toHaveCount(0)
    await expect(page.getByTestId('desktop-update-indicator')).toHaveCount(0)
    await expectTopbarGeometry(page)
  })

  test('compact keeps approval direct and closes the session menu on activation', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 900 })
    const sessionKey = `${TOPBAR_SESSION_KEY}-compact-pressure`
    await openTopbarSession(page, { sessionKey, locale: 'zh-Hans', approvalCount: 2 })
    await expect(page.getByTestId('chat-system-status')).toHaveAttribute('data-layout', 'compact')
    await expect(page.getByTestId('connection-status')).toHaveCount(0)
    await expect(page.getByTestId('chat-system-status-trigger')).toHaveCount(0)
    await page.getByTestId('chat-session-actions-trigger').click()
    await expect(page.getByTestId('chat-session-actions-menu')).toBeVisible()
    await page.getByTestId('chat-system-approval').click()
    await expect(page.getByTestId('chat-session-actions-menu')).toHaveCount(0)
    await expect(page.getByTestId('chat-system-approval')).toHaveCount(1)
    await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(sessionKey)
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })

  test('tight keeps approval direct and update reachable through the sidebar', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-tight-pressure`, locale: 'de', approvalCount: 3,
      update: { status: 'available', latestVersion: '2.0.0' },
    })
    await expect(page.getByTestId('chat-system-status')).toHaveAttribute('data-layout', 'tight')
    await expect(page.getByTestId('connection-status')).toHaveCount(0)
    await expect(page.getByTestId('chat-system-approval')).toBeVisible()
    await expect(page.getByTestId('desktop-update-indicator')).toHaveCount(0)
    await expect(page.getByTestId('sidebar-update-dot')).toBeVisible()
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
    await page.getByTestId('sidebar-toggle-collapsed').click()
    await page.getByTestId('sidebar-desktop-update').click()
    await expect(page).toHaveURL(/\/settings\/gateway#updates$/)
    await expect(page.locator('#settings-gateway-updates')).toBeFocused()
    await expect(page.getByTestId('settings-update-download')).toBeVisible()
  })

  test('desktop update lifecycle preserves header layout and the focused sidebar entry', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    const harness = await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-update-lifecycle`, locale: 'en',
      update: { status: 'available', latestVersion: '2.0.0' },
    })
    const header = page.locator('.chat-header')
    const update = page.getByTestId('sidebar-desktop-update')
    const initialSessionLayout = await header.getAttribute('data-layout')
    await expect(page.getByTestId('chat-system-status')).toHaveCount(0)
    await update.focus()
    const states = [
      { status: 'downloading' as const, progress: 37 },
      { status: 'downloaded' as const, progress: 100 },
      { status: 'error' as const, progress: null, errorCode: 'download_failed',
        error: 'synthetic raw error that must not become the control label' },
    ]
    for (const state of states) {
      await harness.pushUpdate(state)
      await expect(update).toBeVisible()
      await expect(update).toBeFocused()
      await expect(update).not.toHaveAttribute('aria-label', /synthetic raw error/)
      await expect(page.getByTestId('chat-system-status')).toHaveCount(0)
      await expect(header).toHaveAttribute('data-layout', initialSessionLayout || '')
      await expectTopbarGeometry(page)
    }
    await expect(update).toHaveAttribute('data-state', 'danger')
  })

  test('disconnection stays direct while the sidebar update remains reachable', async ({ page }) => {
    await page.setViewportSize({ width: 480, height: 800 })
    const harness = await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-disconnected`, locale: 'en',
      update: { status: 'available', latestVersion: '2.0.0' },
    })
    await harness.disconnect()
    await expect(page.getByTestId('connection-status')).toHaveAttribute('data-state', 'danger', { timeout: 10_000 })
    await expect(page.getByTestId('connection-status')).toHaveClass(/disconnected/)
    await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'disconnected')
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
    await page.getByTestId('sidebar-toggle-collapsed').click()
    await page.getByTestId('sidebar-desktop-update').click()
    await expect(page).toHaveURL(/\/settings\/gateway#updates$/)
    await expect(page.locator('#settings-gateway-updates')).toBeFocused()
  })

  test('update, language, and theme destinations share the existing Settings dialog', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-settings-update`, locale: 'en',
      update: { status: 'available', latestVersion: '2.0.0' },
    })
    await page.getByTestId('sidebar-desktop-update').click()
    await expect(page).toHaveURL(/\/settings\/gateway#updates$/)
    await expect(page.locator('#settings-gateway-updates')).toBeFocused()
    await expect(page.getByTestId('settings-update-download')).toBeVisible()
    await page.locator('#settings-rail-general').click()
    await expect(page.getByTestId('settings-language-group')).toBeVisible()
    await page.locator('#settings-rail-interface').click()
    await expect(page.locator('.appearance-theme:has(input[name="appearance-theme"])')).toBeVisible()
    await expect(page.locator('.settings-modal')).toHaveCount(1)
    await expect(page.locator('[data-chat-topbar-popover]')).toHaveCount(0)
    await page.keyboard.press('Escape')
    await expect(page.locator('.settings-modal')).toHaveCount(0)
    await expect(page.getByTestId('sidebar-desktop-update')).toBeFocused()
  })

  test('200% zoom-equivalent metrics keep every essential control reachable', async ({ page, context }) => {
    const cdp = await context.newCDPSession(page)
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 720, height: 500, deviceScaleFactor: 2, mobile: false,
      screenWidth: 1440, screenHeight: 1000,
    })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-zoom-200`, locale: 'de', approvalCount: 1,
      update: { status: 'available', latestVersion: '2.0.0' },
    })
    await expect.poll(() => page.evaluate(() => ({
      width: window.innerWidth, scale: window.devicePixelRatio,
    }))).toEqual({ width: 720, scale: 2 })
    await expect(page.getByTestId('chat-system-approval')).toBeVisible()
    await expect(page.getByTestId('sidebar-update-dot')).toBeVisible()
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })

  test('forced colors and reduced motion preserve independent severity and geometry', async ({ page }) => {
    await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 400, height: 800 })
    const harness = await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-forced-colors`, locale: 'en', approvalCount: 1,
      update: { status: 'error', errorCode: 'download_failed' },
    })
    await harness.disconnect()
    const connection = page.getByTestId('connection-status')
    await expect(connection).toHaveAttribute('data-state', 'danger')
    await expect(page.getByTestId('chat-system-approval')).toHaveAttribute('data-state', 'warning')
    const stateStyle = await connection.evaluate(element => {
      const style = getComputedStyle(element)
      return { animationName: style.animationName, forcedColorAdjust: style.forcedColorAdjust,
        borderWidth: style.borderTopWidth }
    })
    expect(stateStyle).toEqual({ animationName: 'none', forcedColorAdjust: 'none', borderWidth: '2px' })
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })
})

const THEME_PAIRWISE_CASES: Array<{
  theme: string
  width: number
  locale: 'zh-Hans' | 'de'
  state: Pick<TopbarScenario, 'approvalCount' | 'update'>
}> = [
  { theme: 'ember', width: 400, locale: 'zh-Hans', state: { approvalCount: 1 } },
  {
    theme: 'miami',
    width: 480,
    locale: 'de',
    state: {},
  },
  {
    theme: 'vapor',
    width: 768,
    locale: 'zh-Hans',
    state: { update: { status: 'available', latestVersion: '2.0.0' } },
  },
  { theme: 'synthwave', width: 959, locale: 'de', state: {} },
  {
    theme: 'terminal',
    width: 1440,
    locale: 'zh-Hans',
    state: { approvalCount: 2 },
  },
]

test.describe('Chat topbar theme geometry', () => {
  for (const scenario of THEME_PAIRWISE_CASES) {
    test(`${scenario.theme} stays usable at ${scenario.width}px`, async ({ page }) => {
      await page.setViewportSize({ width: scenario.width, height: 900 })
      await openTopbarSession(page, {
        sessionKey: `${TOPBAR_SESSION_KEY}-theme-${scenario.theme}`,
        theme: scenario.theme,
        locale: scenario.locale,
        ...scenario.state,
      })
      await expectTopbarGeometry(page, {
        minimumTargetSize: scenario.width <= 768 ? 44 : undefined,
      })
    })
  }
})
