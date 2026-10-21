import { expect, test } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'

test('the landing note follows navigation without dismissing it when typing', async ({ page }) => {
  await installSidebarFixture(page)
  await page.goto('/control/chat/new')
  const note = page.locator('.chat-ai-disclaimer')
  await expect(note).toHaveText('AI can make mistakes. Verify important information.')
  await page.locator('.chat-textarea').fill('An unsent draft')
  await expect(note).toBeVisible()

  const historyItems = page.locator('.sidebar-history-item')
  await historyItems.filter({ hasText: 'Synthetic task 01' }).click()
  await expect(page).toHaveURL(/chat\?session=/)
  await expect(note).toHaveCount(0)
  await historyItems.filter({ hasText: 'Synthetic task 02' }).click()
  await expect(page).toHaveURL(/e2e-sidebar-2/)
  await expect(note).toHaveCount(0)

  await page.locator('.sidebar-new-session').click()
  await expect(page).toHaveURL(/chat\/new/)
  await expect(note).toBeVisible()
})

for (const sample of [
  { theme: 'light', locale: 'zh-Hans', width: 1440, copy: 'AI 可能出错，请核实重要信息。' },
  { theme: 'dark', locale: 'en', width: 1440, copy: 'AI can make mistakes. Verify important information.' },
  { theme: 'miami', locale: 'de', width: 360, copy: 'KI kann Fehler machen. Prüfen Sie wichtige Informationen.' },
]) {
  test(`landing note is plain and fits ${sample.theme} ${sample.locale} at ${sample.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: sample.width, height: 900 })
    await installSidebarFixture(page)
    await page.addInitScript(({ theme, locale }) => {
      localStorage.setItem('opensquilla-theme', theme)
      localStorage.setItem('opensquilla-locale', locale)
    }, sample)
    await page.goto('/control/chat/new')
    const note = page.locator('.chat-ai-disclaimer')
    await expect(note).toHaveText(sample.copy)
    await expect(note).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('data-theme', sample.theme)
    const geometry = await note.evaluate(el => {
      const rect = el.getBoundingClientRect()
      const style = getComputedStyle(el)
      return {
        left: rect.left, right: rect.right, bottom: rect.bottom,
        overflow: el.scrollWidth > el.clientWidth,
        background: style.backgroundColor,
        border: style.borderTopWidth,
        fontSize: style.fontSize,
        transition: style.transitionDuration,
        animation: style.animationName,
        tabbarTop: document.querySelector('.mobile-tabbar')?.getBoundingClientRect().top,
      }
    })
    expect(geometry.left).toBeGreaterThanOrEqual(0)
    expect(geometry.right).toBeLessThanOrEqual(sample.width)
    expect(geometry.overflow).toBe(false)
    expect(geometry.background).toBe('rgba(0, 0, 0, 0)')
    expect(geometry.border).toBe('0px')
    expect(geometry.fontSize).toBe('13px')
    expect(geometry.transition).toBe('0s')
    expect(geometry.animation).toBe('none')
    if (sample.width < 768 && geometry.tabbarTop) {
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.tabbarTop)
    }
    await page.screenshot({ path: testInfo.outputPath(`disclaimer-${sample.theme}-${sample.locale}.png`), fullPage: true, animations: 'disabled' })
  })
}
