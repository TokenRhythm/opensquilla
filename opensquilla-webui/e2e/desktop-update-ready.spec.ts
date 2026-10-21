import { expect, test } from '@playwright/test'
import { expectTopbarConsoleClean, openTopbarSession } from './support/topbar-fixture'

test.afterEach(({ page }) => expectTopbarConsoleClean(page))

for (const width of [375, 1440]) {
  test(`${width}px ready entry opens existing settings and keeps release notes external`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const harness = await openTopbarSession(page, {
      locale: 'zh-Hans', theme: 'miami',
      update: { status: 'downloaded', latestVersion: '2.0.0', canInstall: true,
        canNativeInstall: false, installMode: 'manual', canAutoDownload: true,
        releaseNotesUrl: 'https://github.com/TokenRhythm/opensquilla/releases/tag/v2.0.0' },
    })
    if (width < 769) {
      await expect(page.getByTestId('sidebar-update-dot')).toBeVisible()
      await page.getByTestId('sidebar-toggle-collapsed').click()
    }
    const update = page.getByTestId('sidebar-desktop-update')
    await expect(update).toHaveAccessibleName('更新已就绪 2.0.0')
    const label = update.locator('.sidebar-desktop-update__label')
    expect(await label.evaluate(el => getComputedStyle(el).whiteSpace)).toBe('nowrap')
    await page.screenshot({ path: info.outputPath(`ready-sidebar-${width}.png`), fullPage: true })
    await update.click()
    await expect(page).toHaveURL(/settings\/gateway#updates$/)
    await expect(page.locator('#settings-gateway-updates')).toBeFocused()
    await expect(page.getByTestId('settings-update-relaunch')).toContainText('退出并安装')
    const notes = page.getByTestId('settings-update-release-notes')
    await expect(notes).toHaveAttribute('href', 'https://github.com/TokenRhythm/opensquilla/releases/tag/v2.0.0')
    await expect(notes).toHaveAttribute('target', '_blank')
    await expect(notes).toHaveAttribute('rel', 'noopener noreferrer')
    await page.locator('.settings-modal').evaluate(async element => {
      await Promise.all(element.getAnimations({ subtree: true })
        .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
        .map(animation => animation.finished.catch(() => undefined)))
    })
    await page.screenshot({ path: info.outputPath(`ready-settings-${width}.png`), fullPage: true })
    // Snoozing hides the proactive entry, not installation inside settings.
    await harness.pushUpdate({ snoozedUntil: '2024-01-16T08:00:00.000Z' })
    await expect(update).toHaveCount(0)
    await expect(page.getByTestId('settings-update-relaunch')).toBeEnabled()
  })
}

test('legacy manual preparation keeps its download entry and does not pretend it can install', async ({ page }) => {
  await openTopbarSession(page, {
    update: { status: 'available', latestVersion: '2.0.0', installMode: 'manual',
      canAutoDownload: false, canInstall: false, canNativeInstall: false },
  })
  await page.getByTestId('sidebar-desktop-update').click()
  await expect(page.getByTestId('settings-update-download')).toBeEnabled()
  await expect(page.getByTestId('settings-update-relaunch')).toHaveCount(0)
})
