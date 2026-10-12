import { expect, test, type Page } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'

// Real App/ChatView/ChatComposer with an isolated, synthetic Gateway. These
// checks never send a task or read/write the installed desktop profile.
async function openComposer(page: Page, locale = 'en') {
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await installSidebarFixture(page, {
    'sessions.routing.get': { mode: 'router', revision: 0 },
    'sessions.routing.set': { mode: 'router', revision: 1 },
    'models.routing.get': { mode: 'router' },
    'workspaces.list': { workspaces: [] },
    'workspaces.open': {},
  })
  await page.addInitScript(locale => {
    localStorage.setItem('opensquilla-locale', locale)
    localStorage.setItem('opensquilla-theme', 'miami')
  }, locale)
  await page.goto('/control/chat/new')
  await expect(page.locator('.chat-textarea')).toBeVisible()
  await expect(page.locator('.chat-run-mode-btn')).toBeEnabled()
  await expect(page.locator('.chat-model-routing-btn')).toBeVisible()
  await expect(page.locator('.chat-model-routing-btn')).toHaveClass(/chat-model-routing-btn--squilla_router/)
}

test('ordinary Escape preserves the displayed and persisted draft across reload', async ({ page }) => {
  await openComposer(page)
  const textarea = page.locator('.chat-textarea')
  const draft = 'Keep this unfinished task. 保留 Miami 配色。'
  await textarea.fill(draft)
  await expect.poll(() => page.evaluate(() => {
    const key = localStorage.getItem('opensquilla.chat.recent-draft-session')
    return key ? localStorage.getItem(`opensquilla.chat.draft:${key}`) : null
  })).toBe(draft)

  await textarea.press('Escape')
  await expect(textarea).toHaveValue(draft)
  await page.reload()
  await expect(textarea).toHaveValue(draft)

  await page.locator('.chat-run-mode-btn').click()
  await expect(page.locator('.composer-run-mode')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('.composer-run-mode')).toHaveCount(0)
  await expect(textarea).toHaveValue(draft)
})

for (const { width, locale } of [
  { width: 360, locale: 'en' }, { width: 487, locale: 'en' },
  { width: 535, locale: 'en' }, { width: 1440, locale: 'en' },
  { width: 487, locale: 'zh-Hans' }, { width: 487, locale: 'de' },
]) {
  test(`ordinary composer keeps separate hit targets and usable popovers at ${width}px in ${locale}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 940 })
    await openComposer(page, locale)
    const controls = ['.chat-plus-btn', '.chat-project-choose', '.chat-run-mode-btn', '.chat-more-actions-btn', '.chat-model-routing-btn']
    const geometry = await page.locator('.chat-input-footer').evaluate((footer, selectors) => {
      const buttons = selectors.map(selector => footer.querySelector<HTMLElement>(selector)).filter((element): element is HTMLElement => Boolean(element))
      return buttons.map(button => {
        const rect = button.getBoundingClientRect()
        const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
        return {
          name: button.className, left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
          centerHit: Boolean(hit && button.contains(hit)),
        }
      })
    }, controls)
    expect(geometry).toHaveLength(5)
    for (const [index, box] of geometry.entries()) {
      expect(box.centerHit, JSON.stringify(box)).toBe(true)
      expect(box.left).toBeGreaterThanOrEqual(0)
      expect(box.right).toBeLessThanOrEqual(width)
      for (const other of geometry.slice(index + 1)) {
        const overlapX = Math.min(box.right, other.right) - Math.max(box.left, other.left)
        const overlapY = Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top)
        expect(overlapX > 1 && overlapY > 1, `${box.name} overlaps ${other.name}`).toBe(false)
      }
    }
    const permission = geometry.find(box => box.name.includes('chat-run-mode-btn'))!
    const model = geometry.find(box => box.name.includes('chat-model-routing-btn'))!
    if (width === 1440) expect(Math.abs(permission.top - model.top)).toBeLessThanOrEqual(5)
    else expect(model.top).toBeGreaterThanOrEqual(permission.bottom)

    await page.locator('.chat-run-mode-btn').click()
    await expect(page.locator('.composer-run-mode')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.composer-run-mode')).toHaveCount(0)
    await page.locator('.chat-more-actions-btn').click()
    const menu = page.locator('.chat-more-actions-menu')
    await expect(menu).toBeVisible()
    const menuBox = await menu.boundingBox()
    expect(menuBox!.x).toBeGreaterThanOrEqual(0)
    expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(width)
    await page.locator('.chat-more-actions-btn').click()
    await expect(menu).toHaveCount(0)
    await page.locator('.chat-model-routing-btn').click()
    await expect(page.locator('.composer-model-routing')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.composer-model-routing')).toHaveCount(0)
    await page.locator('.chat-input-panel').screenshot({ path: testInfo.outputPath(`composer-miami-${width}-${locale}.png`) })
  })
}
