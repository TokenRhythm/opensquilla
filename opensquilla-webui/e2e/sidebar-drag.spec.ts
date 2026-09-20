import { expect, test, type Page } from '@playwright/test'
import {
  installSidebarFixture,
  SIDEBAR_ACTIVE_SESSION_KEY,
  SIDEBAR_SESSION_KEYS,
} from './support/sidebar-fixture'

const ORDER_STORAGE_KEY = 'opensquilla-sidebar-session-order-v1'
const ROW_SELECTOR = '.sidebar-history-row[data-session-key]'

async function openSidebar(page: Page) {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installSidebarFixture(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SIDEBAR_ACTIVE_SESSION_KEY)}`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(page.locator(ROW_SELECTOR)).toHaveCount(SIDEBAR_SESSION_KEYS.length)
  await expect(page.getByText('The example tasks are ready.', { exact: false })).toBeVisible()
}

async function renderedOrder(page: Page) {
  return page.locator(ROW_SELECTOR).evaluateAll(rows => (
    rows.map(row => (row as HTMLElement).dataset.sessionKey)
  ))
}

async function beginDrag(page: Page) {
  const source = page.locator(ROW_SELECTOR).nth(0)
  const target = page.locator(ROW_SELECTOR).nth(3)
  // Hover waits for the row to settle after initial session metadata arrives.
  await source.hover({ position: { x: 65, y: 20 } })
  const targetBox = await target.boundingBox()
  expect(targetBox).not.toBeNull()
  await page.mouse.down()
  await page.mouse.move(targetBox!.x + 65, targetBox!.y + targetBox!.height - 6, { steps: 12 })
  await expect(page.locator('.sidebar-session-drag-preview')).toBeVisible()
  await expect(source).toHaveClass(/is-dragging/)
  await expect(target).toHaveClass(/is-drop-after/)
}

test('mouse drag previews, commits once, and preserves order after reload', async ({ page }) => {
  await openSidebar(page)
  const initialUrl = page.url()
  await beginDrag(page)
  await expect.poll(() => renderedOrder(page)).toEqual(SIDEBAR_SESSION_KEYS)
  await page.mouse.up()

  const expectedOrder = [...SIDEBAR_SESSION_KEYS]
  expectedOrder.splice(0, 1)
  expectedOrder.splice(3, 0, SIDEBAR_SESSION_KEYS[0]!)
  await expect.poll(() => renderedOrder(page)).toEqual(expectedOrder)
  await expect(page.locator('.sidebar-session-drag-preview')).toHaveCount(0)
  await expect(page).toHaveURL(initialUrl)
  await expect.poll(() => page.evaluate(key => JSON.parse(
    localStorage.getItem(key) || '[]',
  ), ORDER_STORAGE_KEY)).toEqual(expectedOrder)

  // The drag's synthetic click must not swallow the next keyboard activation.
  await page.locator(`[data-session-key="${SIDEBAR_SESSION_KEYS[0]}"] .sidebar-history-item`).focus()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(url => url.searchParams.get('session') === SIDEBAR_SESSION_KEYS[0])
  await expect.poll(() => renderedOrder(page)).toEqual(expectedOrder)

  await page.reload()
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect.poll(() => renderedOrder(page)).toEqual(expectedOrder)
})

test('Escape cancels a drag without reordering or selecting a task', async ({ page }) => {
  await openSidebar(page)
  const initialUrl = page.url()
  await beginDrag(page)
  await page.keyboard.press('Escape')
  await expect(page.locator('.sidebar-session-drag-preview')).toHaveCount(0)
  await page.mouse.up()

  await expect.poll(() => renderedOrder(page)).toEqual(SIDEBAR_SESSION_KEYS)
  await expect(page.locator('.is-dragging, .is-drop-before, .is-drop-after')).toHaveCount(0)
  await expect(page).toHaveURL(initialUrl)
  expect(await page.evaluate(key => localStorage.getItem(key), ORDER_STORAGE_KEY)).toBeNull()
})

test('wheel scrolling during a drag moves the insertion marker to the visible target', async ({ page }) => {
  await openSidebar(page)
  await beginDrag(page)
  const initialUrl = page.url()
  const originalTarget = page.locator(ROW_SELECTOR).nth(3)
  const box = (await originalTarget.boundingBox())!
  const point = { x: box.x + 65, y: box.y + box.height - 6 }
  await page.mouse.wheel(0, 132)
  await expect.poll(() => page.locator('.sidebar-history-list').evaluate(el => el.scrollTop))
    .toBeGreaterThan(100)

  const target = await page.evaluate(({ x, y }) => {
    const row = document.elementFromPoint(x, y)?.closest<HTMLElement>('[data-session-key]')
    if (!row) return null
    const rect = row.getBoundingClientRect()
    return { key: row.dataset.sessionKey!, after: y >= rect.top + rect.height / 2 }
  }, point)
  expect(target).not.toBeNull()
  expect(target!.key).not.toBe(SIDEBAR_SESSION_KEYS[3])
  await expect(page.locator('.is-drop-before, .is-drop-after'))
    .toHaveAttribute('data-session-key', target!.key)
  await page.mouse.up()

  const expected = SIDEBAR_SESSION_KEYS.slice(1)
  expected.splice(expected.indexOf(target!.key) + Number(target!.after), 0, SIDEBAR_SESSION_KEYS[0]!)
  await expect.poll(() => renderedOrder(page)).toEqual(expected)
  await expect(page).toHaveURL(initialUrl)
})

test('holding a drag at the history edge scrolls and Escape stops it', async ({ page }) => {
  await openSidebar(page)
  await beginDrag(page)
  const history = page.locator('.sidebar-history-list')
  const historyBox = await history.boundingBox()
  expect(historyBox).not.toBeNull()
  await page.mouse.move(historyBox!.x + 70, historyBox!.y + historyBox!.height - 4, { steps: 8 })
  await expect.poll(() => history.evaluate(element => element.scrollTop)).toBeGreaterThan(40)
  await page.keyboard.press('Escape')
  await page.mouse.up()
  await expect(page.locator('.sidebar-session-drag-preview')).toHaveCount(0)
  const stoppedAt = await history.evaluate(element => element.scrollTop)
  await page.waitForTimeout(100)
  expect(await history.evaluate(element => element.scrollTop)).toBe(stoppedAt)
  await expect.poll(() => renderedOrder(page)).toEqual(SIDEBAR_SESSION_KEYS)
})

test('desktop main canvas has square corners and meets the sidebar flush', async ({ page }) => {
  await openSidebar(page)
  const geometry = await page.evaluate(() => {
    const main = document.querySelector<HTMLElement>('#app-main')!
    const sidebar = document.querySelector<HTMLElement>('.sidebar')!
    const topbar = document.querySelector<HTMLElement>('.topbar--chat')!
    const chat = document.querySelector<HTMLElement>('.content--chat .chat')!
    const mainRect = main.getBoundingClientRect()
    const topbarRect = topbar.getBoundingClientRect()
    const chatRect = chat.getBoundingClientRect()
    return {
      surfaces: [main, topbar, chat].map(element => {
        const style = getComputedStyle(element)
        return {
          radii: [style.borderTopLeftRadius, style.borderTopRightRadius,
            style.borderBottomLeftRadius, style.borderBottomRightRadius],
          boxShadow: style.boxShadow,
        }
      }),
      gaps: [
        topbarRect.top,
        window.innerHeight - chatRect.bottom,
        window.innerWidth - topbarRect.right,
        window.innerWidth - chatRect.right,
        mainRect.left - sidebar.getBoundingClientRect().right,
        topbarRect.left - mainRect.left,
        chatRect.left - mainRect.left,
        chatRect.top - topbarRect.bottom,
      ],
    }
  })
  for (const surface of geometry.surfaces) {
    expect(surface.radii).toEqual(['0px', '0px', '0px', '0px'])
    expect(surface.boxShadow).toBe('none')
  }
  for (const edge of geometry.gaps) {
    expect(Math.abs(edge)).toBeLessThanOrEqual(1)
  }
})

test('narrow drawer keeps the session hover card out of the list interaction path', async ({ page }) => {
  await page.setViewportSize({ width: 548, height: 844 })
  await installSidebarFixture(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SIDEBAR_ACTIVE_SESSION_KEY)}`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()

  const openSidebarButton = page.getByTestId('sidebar-toggle-collapsed')
  if (await openSidebarButton.isVisible()) await openSidebarButton.click()
  await expect(page.locator('.sidebar--drawer')).toBeVisible()

  const firstRow = page.locator(ROW_SELECTOR).first()
  await firstRow.hover()
  await expect(page.locator('.sidebar-session-preview')).toHaveCount(0)
  await expect(firstRow).toBeVisible()
})

test('resizing closes the existing preview and keyboard focus respects the breakpoint', async ({ page }) => {
  await openSidebar(page)
  const firstButton = page.locator(`${ROW_SELECTOR} .sidebar-history-item`).first()
  await firstButton.focus()
  await expect(page.locator('.sidebar-session-preview')).toBeVisible()

  for (const width of [768, 548, 769]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.locator('.sidebar-session-preview')).toHaveCount(0)
    const toggle = page.getByTestId('sidebar-toggle-collapsed')
    if (await toggle.isVisible()) await toggle.click()
    // Wait for the drawer/compact layout to settle before the keyboard gesture.
    await firstButton.click({ trial: true })
    await page.locator(`${ROW_SELECTOR} .sidebar-history-item`).nth(1).focus()
    await firstButton.focus()
    await expect(page.locator('.sidebar-session-preview')).toHaveCount(width > 768 ? 1 : 0)
  }
})
