import { expect, test } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'
import { CONTINUOUS_MESSAGE, CONTINUOUS_SESSION, continuousHistory, continuousPayloads } from './support/continuous-reading-fixture'

test('desktop to narrow Settings keeps the Interface detail and reading position with an explicit category return', async ({ page }, testInfo) => {
  await installSidebarFixture(page)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.goto('/control/settings/interface')
  const dialog = page.getByRole('dialog', { name: 'Settings' })
  const rail = dialog.locator('.settings-rail')
  const selected = rail.locator('#settings-rail-interface')
  const search = dialog.getByRole('searchbox')
  const panel = dialog.locator('.settings-panel')
  await expect(selected).toHaveAttribute('aria-selected', 'true')
  await expect(rail).toHaveAttribute('aria-orientation', 'vertical')
  await search.focus()
  const scrollBefore = await panel.evaluate(element => {
    element.scrollTop = 160
    return element.scrollTop
  })
  expect(scrollBefore).toBeGreaterThan(0)

  await page.setViewportSize({ width: 390, height: 720 })
  await expect(rail).toBeHidden()
  await expect(panel).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Back to settings categories' })).toBeVisible()
  await expect(search).toBeFocused()
  // Native scroll anchoring can adjust the offset as theme cards reflow; the
  // breakpoint must not reset the reading position to the start of the panel.
  expect(await panel.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  await expect(page).toHaveURL(/\/settings\/interface$/)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath('settings-interface-narrow.png'), fullPage: true })

  await page.setViewportSize({ width: 1280, height: 720 })
  await expect(rail).toHaveAttribute('aria-orientation', 'vertical')
  await expect(selected).toHaveAttribute('aria-selected', 'true')
  await expect(search).toBeFocused()
  expect(await panel.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
})

for (const theme of ['miami', 'light', 'dark']) {
  test(`new-task font inherits the navigation font and incomplete activity uses the readable muted token in ${theme}`, async ({ page }, testInfo) => {
    const history = structuredClone(continuousHistory)
    const outcomes = history.turn_outcomes as Array<{ activity_snapshot: { complete: boolean } }>
    outcomes[0]!.activity_snapshot.complete = false
    await installSidebarFixture(page, { ...continuousPayloads, 'chat.history': history })
    await page.addInitScript(theme => {
      localStorage.setItem('opensquilla-theme', theme)
      localStorage.removeItem('opensquilla.appearance.toolDetails.v1')
    }, theme)
    await page.setViewportSize({ width: 1280, height: 850 })
    await page.goto(`/control/chat?session=${encodeURIComponent(CONTINUOUS_SESSION)}`)
    const newTask = page.locator('.sidebar-new-session')
    await expect(newTask).toBeVisible()
    const fonts = await newTask.evaluate(element => {
      const current = getComputedStyle(element)
      const adjacent = getComputedStyle(document.querySelector('.sidebar-core .sidebar-fn-item')!)
      return { family: current.fontFamily, adjacentFamily: adjacent.fontFamily, size: current.fontSize, adjacentSize: adjacent.fontSize }
    })
    expect(fonts.family).toBe(fonts.adjacentFamily)
    expect(fonts.size).toBe(fonts.adjacentSize)
    const answer = page.locator(`[data-message-id="${CONTINUOUS_MESSAGE}"]`)
    const disclosure = answer.locator('.assistant-activity__summary')
    await expect(disclosure).toHaveAttribute('aria-expanded', 'false')
    await disclosure.click()
    const incomplete = answer.locator('.assistant-activity-incomplete')
    await expect(incomplete).toBeVisible()
    const styles = await incomplete.evaluate(element => {
      const style = getComputedStyle(element)
      const token = style.getPropertyValue('--text-muted').trim()
      const expected = token.startsWith('#') && token.length === 7
        ? `rgb(${[1, 3, 5].map(start => parseInt(token.slice(start, start + 2), 16)).join(', ')})`
        : token
      return { color: style.color, expected, fontSize: style.fontSize, opacity: style.opacity }
    })
    expect(styles.color).toBe(styles.expected)
    expect(styles.fontSize).toBe('12px')
    expect(styles.opacity).toBe('1')
    for (const call of await answer.locator('.tool-row[aria-expanded]').all()) {
      await expect(call).toHaveAttribute('aria-expanded', 'false')
    }
    if (theme === 'miami') {
      await incomplete.scrollIntoViewIfNeeded()
      await page.screenshot({ path: testInfo.outputPath('miami-typography-incomplete.png'), fullPage: true })
    }
  })
}
