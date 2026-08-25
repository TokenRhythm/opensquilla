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
  test('non-chat language and theme popovers are mutually exclusive', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
    await page.goto('/control/sessions')

    const language = page.getByTestId('language-switcher-trigger')
    const theme = page.getByRole('button', { name: 'Theme', exact: true })
    await expect(language).toBeVisible()
    await language.click()
    await expect(page.locator('[data-chat-topbar-popover="language"]')).toHaveCount(1)

    await theme.click()
    await expect(page.locator('[data-chat-topbar-popover="language"]')).toHaveCount(0)
    await expect(page.locator('[data-chat-topbar-popover="theme"]')).toHaveCount(1)
    await expect(page.locator('[data-chat-topbar-popover]')).toHaveCount(1)

    await language.click()
    await expect(page.locator('[data-chat-topbar-popover="theme"]')).toHaveCount(0)
    await expect(page.locator('[data-chat-topbar-popover="language"]')).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(page.locator('[data-chat-topbar-popover]')).toHaveCount(0)
  })

  test('web keeps Desktop updates absent while wide connection and approval stay direct', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-web-wide`,
      locale: 'en',
      approvalCount: 1,
    })

    const system = page.getByTestId('chat-system-status')
    await expect(system).toHaveAttribute('data-layout', 'wide')
    await expect(page.getByTestId('connection-status')).toBeVisible()
    await expect(page.getByTestId('chat-system-approval')).toHaveCount(1)
    await expect(page.getByTestId('desktop-update-indicator')).toHaveCount(0)
    await expect(page.getByTestId('chat-system-update')).toHaveCount(0)
    await expectTopbarGeometry(page)
  })

  test('compact keeps approval and active audio reachable exactly once', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 900 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-compact-pressure`,
      locale: 'zh-Hans',
      approvalCount: 2,
      bgm: { enabled: true, playing: true },
    })

    const system = page.getByTestId('chat-system-status')
    const systemTrigger = page.getByTestId('chat-system-status-trigger')
    const sessionTrigger = page.getByTestId('chat-session-actions-trigger')
    await expect(system).toHaveAttribute('data-layout', 'compact')
    await expect(page.getByTestId('connection-status')).toHaveCount(1)
    await expect(page.getByTestId('bgm-toggle')).toHaveCount(1)
    await expect(page.getByTestId('bgm-toggle')).toHaveAccessibleName('暂停背景音乐')
    await expect(page.getByTestId('bgm-menu-trigger')).toHaveCount(0)

    await sessionTrigger.click()
    await expect(page.getByTestId('chat-session-actions-menu')).toBeVisible()
    await systemTrigger.click()
    await expect(page.getByTestId('chat-session-actions-menu')).toHaveCount(0)
    await expect(page.getByTestId('chat-system-status-menu')).toBeVisible()
    await expect(page.getByTestId('chat-system-approval')).toHaveCount(1)
    await expect(page.getByTestId('chat-system-connection')).toHaveCount(0)
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('chat-system-status-menu')).toHaveCount(0)
    await expect(systemTrigger).toBeFocused()
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })

  test('tight combines connection, approval, and update without hiding pause', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-tight-pressure`,
      locale: 'de',
      approvalCount: 3,
      bgm: { enabled: true, playing: true },
      update: {
        status: 'available',
        latestVersion: '2.0.0',
      },
    })

    const system = page.getByTestId('chat-system-status')
    const trigger = page.getByTestId('chat-system-status-trigger')
    await expect(system).toHaveAttribute('data-layout', 'tight')
    await expect(page.getByTestId('connection-status')).toHaveCount(0)
    await expect(page.getByTestId('desktop-update-indicator')).toHaveCount(0)
    await expect(page.getByTestId('bgm-toggle')).toHaveCount(1)
    await trigger.click()
    await expect(page.getByTestId('chat-system-connection')).toHaveCount(1)
    await expect(page.getByTestId('chat-system-approval')).toHaveCount(1)
    await expect(page.getByTestId('chat-system-update')).toHaveCount(1)
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })

    await page.getByTestId('chat-system-update').click()
    await expect(page).toHaveURL(/\/settings\/runtime$/)
  })

  test('Desktop update lifecycle preserves wide layout and focused control', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    const harness = await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-update-lifecycle`,
      locale: 'en',
      update: {
        status: 'available',
        latestVersion: '2.0.0',
      },
    })
    const system = page.getByTestId('chat-system-status')
    const header = page.locator('.chat-header')
    const update = page.getByTestId('desktop-update-indicator')
    const initialSystemLayout = await system.getAttribute('data-layout')
    const initialSessionLayout = await header.getAttribute('data-layout')
    await expect(system).toHaveAttribute('data-layout', 'wide')
    await update.focus()

    const states = [
      { status: 'downloading' as const, progress: 37 },
      { status: 'downloaded' as const, progress: 100 },
      {
        status: 'error' as const,
        progress: null,
        errorCode: 'download_failed',
        error: 'synthetic raw error that must not become the control label',
      },
    ]
    for (const state of states) {
      await harness.pushUpdate(state)
      await expect(update).toBeVisible()
      await expect(update).toBeFocused()
      await expect(system).toHaveAttribute('data-layout', initialSystemLayout || '')
      await expect(header).toHaveAttribute('data-layout', initialSessionLayout || '')
      await expectTopbarGeometry(page)
    }
  })

  test('disconnection remains dominant while update and active audio stay reachable', async ({ page }) => {
    await page.setViewportSize({ width: 480, height: 800 })
    const harness = await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-disconnected`,
      locale: 'en',
      bgm: { enabled: true, playing: true },
      update: {
        status: 'available',
        latestVersion: '2.0.0',
      },
    })

    await harness.disconnect()
    const system = page.getByTestId('chat-system-status')
    await expect(system).toHaveAttribute('data-severity', 'danger', { timeout: 10_000 })
    await expect(page.getByTestId('connection-status')).toHaveClass(/disconnected/)
    await expect(page.getByTestId('bgm-toggle')).toHaveCount(1)
    await page.getByTestId('chat-system-status-trigger').click()
    await expect(page.getByTestId('chat-system-update')).toHaveCount(1)
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })

  test('wide update, language, and theme popovers are mutually exclusive', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-popover-update`,
      locale: 'en',
      update: {
        status: 'available',
        latestVersion: '2.0.0',
      },
    })

    const update = page.getByTestId('desktop-update-indicator')
    const language = page.getByTestId('language-switcher-trigger')
    const theme = page.locator(
      '.topbar-right > .theme-menu-wrap:not(.lang-menu-wrap):not(.bgm-menu-wrap) > button',
    )
    await update.click()
    await expect(page.locator('[data-chat-topbar-popover="desktop-update"]')).toHaveCount(1)
    await language.click()
    await expect(page.locator('[data-chat-topbar-popover="desktop-update"]')).toHaveCount(0)
    await expect(page.locator('[data-chat-topbar-popover="language"]')).toHaveCount(1)
    await theme.click()
    await expect(page.locator('[data-chat-topbar-popover="language"]')).toHaveCount(0)
    await expect(page.locator('[data-chat-topbar-popover="theme"]')).toHaveCount(1)
    await page.keyboard.press('Escape')
    await expect(page.locator('[data-chat-topbar-popover]')).toHaveCount(0)
    await expect(theme).toBeFocused()
  })

  test('wide BGM picker yields ownership to the language menu', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-popover-bgm`,
      locale: 'en',
      bgm: { enabled: true, playing: false },
    })

    const bgm = page.getByTestId('bgm-menu-trigger')
    const language = page.getByTestId('language-switcher-trigger')
    await bgm.click()
    await expect(page.locator('[data-chat-topbar-popover="bgm"]')).toHaveCount(1)
    await language.click()
    await expect(page.locator('[data-chat-topbar-popover="bgm"]')).toHaveCount(0)
    await expect(page.locator('[data-chat-topbar-popover="language"]')).toHaveCount(1)
  })

  test('200% zoom-equivalent metrics keep every essential control reachable', async ({ page, context }) => {
    const cdp = await context.newCDPSession(page)
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 720,
      height: 500,
      deviceScaleFactor: 2,
      mobile: false,
      screenWidth: 1440,
      screenHeight: 1000,
    })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-zoom-200`,
      locale: 'de',
      approvalCount: 1,
      bgm: { enabled: true, playing: true },
      update: { status: 'available', latestVersion: '2.0.0' },
    })

    await expect.poll(() => page.evaluate(() => ({
      width: window.innerWidth,
      scale: window.devicePixelRatio,
    }))).toEqual({ width: 720, scale: 2 })
    await expect(page.getByTestId('chat-system-status-trigger')).toBeVisible()
    await expect(page.getByTestId('bgm-toggle')).toBeVisible()
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })

  test('forced colors and reduced motion preserve severity and geometry', async ({ page }) => {
    await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 400, height: 800 })
    await openTopbarSession(page, {
      sessionKey: `${TOPBAR_SESSION_KEY}-forced-colors`,
      locale: 'en',
      approvalCount: 1,
      update: { status: 'error', errorCode: 'download_failed' },
    })

    const trigger = page.getByTestId('chat-system-status-trigger')
    await expect(trigger).toHaveAttribute('data-state', 'danger')
    const stateStyle = await trigger.evaluate(element => {
      const style = getComputedStyle(element)
      return {
        animationName: style.animationName,
        forcedColorAdjust: style.forcedColorAdjust,
        borderWidth: style.borderTopWidth,
      }
    })
    expect(stateStyle).toEqual({
      animationName: 'none',
      forcedColorAdjust: 'none',
      borderWidth: '2px',
    })
    await expectTopbarGeometry(page, { minimumTargetSize: 44 })
  })
})

const THEME_PAIRWISE_CASES: Array<{
  theme: string
  width: number
  locale: 'zh-Hans' | 'de'
  state: Pick<TopbarScenario, 'approvalCount' | 'bgm' | 'update'>
}> = [
  { theme: 'ember', width: 400, locale: 'zh-Hans', state: { approvalCount: 1 } },
  {
    theme: 'miami',
    width: 480,
    locale: 'de',
    state: { bgm: { enabled: true, playing: true } },
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
    state: { approvalCount: 2, bgm: { enabled: true, playing: false } },
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
