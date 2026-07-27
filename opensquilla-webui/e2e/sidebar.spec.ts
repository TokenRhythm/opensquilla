import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const RAW_KEY_PATTERN = /agent:[a-z0-9_-]+:[a-z0-9_-]+:/i
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

async function openControl(page: Page, path = '') {
  await page.goto(CONTROL_URL + path)
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  // Let the session list settle before inspecting the sidebar.
  await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
  await page.waitForTimeout(800)
  await expect(
    page.locator('.sidebar-history-list, .sidebar-history-empty, .sidebar-onboarding').first(),
  ).toBeVisible()
}

test.describe('Sidebar', () => {
  test('desktop separator resizes, persists, and collapses only on pointer release', async ({ page }) => {
    await page.addInitScript(() => {
      if (sessionStorage.getItem('sidebar-resize-e2e-seeded')) return
      localStorage.removeItem('opensquilla.sidebar.width.v1')
      sessionStorage.setItem('sidebar-resize-e2e-seeded', '1')
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openControl(page)

    const sidebar = page.locator('.sidebar')
    const main = page.locator('#app-main')
    const resizer = page.getByTestId('sidebar-resizer')
    await expect(resizer).toBeVisible()
    await expect(resizer).toHaveAttribute('role', 'separator')
    await expect(resizer).toHaveAttribute('aria-controls', 'sidebar-nav app-main')

    const assertDockGeometry = async (expectedWidth: number) => {
      await expect.poll(async () => {
        const [sidebarBox, mainBox] = await Promise.all([sidebar.boundingBox(), main.boundingBox()])
        if (!sidebarBox || !mainBox) return null
        return {
          width: Math.round(sidebarBox.width),
          edgeDelta: Math.round(Math.abs(sidebarBox.x + sidebarBox.width - mainBox.x)),
        }
      }).toEqual({ width: expectedWidth, edgeDelta: 0 })
    }

    await assertDockGeometry(260)
    const initialHandle = await resizer.boundingBox()
    expect(initialHandle).not.toBeNull()
    await page.mouse.move(initialHandle!.x + 1, 420)
    await page.mouse.down()
    await page.mouse.move(initialHandle!.x + 101, 420, { steps: 8 })
    await page.mouse.up()
    await assertDockGeometry(360)
    await expect.poll(() => page.evaluate(() => JSON.parse(
      localStorage.getItem('opensquilla.sidebar.width.v1') || '{}',
    ))).toMatchObject({ version: 1, width: 360, source: 'custom' })

    // Crossing the raw 200px threshold only arms collapse. The expanded layout
    // remains at its 240px floor until the mouse is released.
    const resizedHandle = await resizer.boundingBox()
    await page.mouse.move(resizedHandle!.x + 1, 420)
    await page.mouse.down()
    await page.mouse.move(196, 420, { steps: 10 })
    await expect(page.locator('.sidebar-resizer__collapse-cue')).toBeVisible()
    await assertDockGeometry(240)
    await expect(sidebar).toHaveClass(/docked/)
    await page.mouse.up()

    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()
    await expect.poll(() => page.evaluate(() => JSON.parse(
      localStorage.getItem('opensquilla.sidebar.width.v1') || '{}',
    ))).toMatchObject({ width: 360, source: 'custom' })

    await page.getByTestId('sidebar-toggle-collapsed').click()
    await assertDockGeometry(360)
    await page.reload()
    await expect(resizer).toBeVisible()
    await assertDockGeometry(360)

    await resizer.dblclick()
    await assertDockGeometry(260)
    await expect(resizer).toHaveAttribute('aria-valuetext', /Default/)
    await expect.poll(() => page.evaluate(() => (
      localStorage.getItem('opensquilla.sidebar.width.v1')
    ))).toBeNull()
  })

  test('compact and resizable breakpoints preserve the desktop preference without a 959px jump', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem(
      'opensquilla.sidebar.width.v1',
      JSON.stringify({ version: 1, width: 360, source: 'wide' }),
    ))
    await page.setViewportSize({ width: 959, height: 900 })
    await openControl(page)

    const sidebar = page.locator('.sidebar')
    const width = async () => Math.round((await sidebar.boundingBox())?.width || 0)
    await expect(page.getByTestId('sidebar-resizer')).toHaveCount(0)
    await expect.poll(width).toBe(260)

    await page.setViewportSize({ width: 960, height: 900 })
    await expect(page.getByTestId('sidebar-resizer')).toBeVisible()
    await expect.poll(width).toBe(260)

    await page.setViewportSize({ width: 1024, height: 768 })
    await expect.poll(width).toBe(324)
    await expect.poll(() => page.evaluate(() => JSON.parse(
      localStorage.getItem('opensquilla.sidebar.width.v1') || '{}',
    ).width)).toBe(360)

    const constrainedHandle = await page.getByTestId('sidebar-resizer').boundingBox()
    const startX = constrainedHandle!.x + 1
    await page.mouse.move(startX, 420)
    await page.mouse.down()
    await page.mouse.move(startX - 40, 420, { steps: 5 })
    await page.mouse.move(startX, 420, { steps: 5 })
    await page.mouse.up()
    await expect.poll(width).toBe(324)
    await expect.poll(() => page.evaluate(() => JSON.parse(
      localStorage.getItem('opensquilla.sidebar.width.v1') || '{}',
    ))).toMatchObject({ width: 360, source: 'wide' })

    await page.getByTestId('sidebar-resizer').focus()
    await page.setViewportSize({ width: 959, height: 900 })
    await expect(page.getByTestId('sidebar-resizer')).toHaveCount(0)
    await expect(page.getByTestId('sidebar-toggle-expanded')).toBeFocused()

    await page.setViewportSize({ width: 768, height: 900 })
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()
    await page.setViewportSize({ width: 1024, height: 768 })
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-resizer')).toHaveCount(0)
  })

  test('touch phone landscape uses a fixed drawer with no resize handle', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: process.env.OPENSQUILLA_WEBUI_BASE_URL || 'http://127.0.0.1:18791',
      viewport: { width: 844, height: 390 },
      hasTouch: true,
      isMobile: true,
    })
    const page = await context.newPage()
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const sidebar = page.locator('.sidebar')
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-resizer')).toHaveCount(0)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toHaveCSS('width', '44px')
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toHaveCSS('height', '44px')
    await page.getByTestId('sidebar-toggle-collapsed').click()
    await expect(sidebar).toHaveClass(/sidebar--drawer/)
    await expect(page.locator('.sidebar-scrim')).toBeVisible()
    await expect(page.locator('#app-main')).toHaveAttribute('inert', '')
    await expect.poll(async () => Math.round((await sidebar.boundingBox())?.width || 0)).toBe(280)
    await expect.poll(async () => {
      const box = await page.locator('#app-main').boundingBox()
      return box ? Math.round(box.x) : -1
    }).toBe(0)
    await expect(page.getByTestId('sidebar-toggle-expanded')).toHaveCSS('width', '44px')

    await page.keyboard.press('Escape')
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()

    await page.getByTestId('sidebar-toggle-collapsed').click()
    await page.locator('.sidebar-scrim').click({ position: { x: 300, y: 200 } })
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()

    await page.getByTestId('sidebar-toggle-collapsed').click()
    await page.locator('.sidebar-brand-link').click()
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()

    const drawerShortcut = await page.getByTestId('sidebar-toggle-collapsed').getAttribute('aria-keyshortcuts')
    const drawerPrimary = drawerShortcut?.startsWith('Meta') ? 'Meta' : 'Control'
    await page.keyboard.press(`${drawerPrimary}+b`)
    await expect(sidebar).toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-expanded')).toBeFocused()
    await context.close()
  })

  test('phone portrait drawers fit 320px and 390px viewports without page overflow', async ({ browser }) => {
    for (const viewport of [
      { width: 320, height: 568 },
      { width: 390, height: 844 },
    ]) {
      const context = await browser.newContext({
        baseURL: process.env.OPENSQUILLA_WEBUI_BASE_URL || 'http://127.0.0.1:18791',
        viewport,
        hasTouch: true,
        isMobile: true,
      })
      const page = await context.newPage()
      await page.goto(CONTROL_URL)
      await page.waitForSelector('.conn-pill', { timeout: 10000 })
      await page.getByTestId('sidebar-toggle-collapsed').click()

      await expect(page.getByTestId('sidebar-resizer')).toHaveCount(0)
      await expect.poll(async () => Math.round((await page.locator('.sidebar').boundingBox())?.width || 0)).toBe(280)
      await expect.poll(() => page.evaluate(() => ({
        viewportWidth: window.innerWidth,
        pageWidth: document.documentElement.scrollWidth,
      }))).toEqual({ viewportWidth: viewport.width, pageWidth: viewport.width })
      await context.close()
    }
  })

  test('separator keeps a real focus outline and removes motion in accessibility media modes', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce', forcedColors: 'active' })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openControl(page)

    const resizer = page.getByTestId('sidebar-resizer')
    await resizer.focus()
    await expect(resizer).toBeFocused()
    await expect.poll(() => resizer.evaluate(element => getComputedStyle(element).outlineStyle)).toBe('solid')
    await expect.poll(() => resizer.evaluate((element) => {
      const line = getComputedStyle(element, '::before')
      return Math.max(
        ...line.transitionDuration.split(',').map(value => Number.parseFloat(value) || 0),
      )
    })).toBeLessThanOrEqual(0.00001)

    await openControl(page, 'settings/appearance')
    const wide = page.getByTestId('settings-sidebar-width-wide')
    await wide.locator('..').click()
    await page.getByRole('dialog').getByRole('button', { name: 'Close' }).focus()
    await expect.poll(() => wide.locator('..').evaluate((element) => {
      const style = getComputedStyle(element)
      return { style: style.outlineStyle, width: style.outlineWidth }
    })).toEqual({ style: 'solid', width: '2px' })
  })

  test('Appearance provides a click-only alternative for preset, custom width, and collapse', async ({ page }) => {
    await page.addInitScript(() => localStorage.removeItem('opensquilla.sidebar.width.v1'))
    await page.setViewportSize({ width: 1440, height: 900 })
    await openControl(page, 'settings/appearance')

    const sidebar = page.locator('.sidebar')
    const sidebarWidth = async () => Math.round((await sidebar.boundingBox())?.width || 0)
    await expect(page.getByTestId('settings-sidebar-width-group')).toBeVisible()

    await page.getByTestId('settings-sidebar-width-wide').locator('..').click()
    await expect.poll(sidebarWidth).toBe(360)

    await page.getByTestId('settings-sidebar-width-custom').locator('..').click()
    const increase = page.getByTestId('settings-sidebar-width-increase')
    for (let index = 0; index < 52; index += 1) await increase.click()
    await expect(page.getByTestId('settings-sidebar-width-value')).toHaveValue('312')
    await page.getByTestId('settings-sidebar-width-apply').click()
    await expect.poll(sidebarWidth).toBe(312)

    await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await page.getByTestId('sidebar-toggle-expanded').click()
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()
  })

  test('desktop sidebar uses explicit toggles and the primary+B shortcut', async ({ page }) => {
    await openControl(page)

    const sidebar = page.locator('.sidebar')
    const expandedToggle = page.getByTestId('sidebar-toggle-expanded')
    await expect(sidebar).toHaveClass(/docked/)
    await expect(expandedToggle).toHaveAttribute('aria-expanded', 'true')
    await expect(expandedToggle).toHaveAttribute('aria-keyshortcuts', /^(Meta|Control)\+B$/)
    await expect(expandedToggle.locator('path[d="M9.5 4.5v15"]')).toHaveCount(1)

    await expandedToggle.click()
    await expect(sidebar).not.toHaveClass(/docked/)
    const collapsedToggle = page.getByTestId('sidebar-toggle-collapsed')
    await expect(collapsedToggle).toBeFocused()
    await expect(collapsedToggle).toHaveAttribute('aria-expanded', 'false')
    await expect(collapsedToggle.locator('path[d="M8.5 9v6"]')).toHaveCount(1)

    // The former invisible edge hot zone is gone: approaching the history rail
    // can no longer reveal a 260px overlay above it.
    await expect(page.locator('.sidebar-hover-trigger, .sidebar-shadow')).toHaveCount(0)
    await page.mouse.move(1, 400)
    await page.waitForTimeout(350)
    await expect(sidebar).not.toHaveClass(/docked|hovered/)

    // Follow the app's rendered platform-relative chord. navigator.platform is
    // intentionally not used by the app and can disagree with the emulated UA.
    const primary = (await expandedToggle.getAttribute('aria-keyshortcuts'))?.startsWith('Meta')
      ? 'Meta'
      : 'Control'
    await page.keyboard.press(`${primary}+b`)
    await expect(sidebar).toHaveClass(/docked/)
    await page.keyboard.press(`${primary}+b`)
    await expect(sidebar).not.toHaveClass(/docked/)
  })

  test('Recents renders collapsible family groups (or onboarding when empty)', async ({ page }) => {
    await openControl(page)

    await expect(page.locator('.sidebar-recents-eyebrow')).toHaveText('Recents')

    const sidebarText = await page.locator('.sidebar').innerText()
    expect(sidebarText).not.toMatch(RAW_KEY_PATTERN)
    expect(sidebarText).not.toMatch(UUID_PATTERN)

    const titles = page.locator('.sidebar-history-title')
    if ((await titles.count()) === 0) {
      // First-run empty state: the onboarding panel replaces the list.
      await expect(page.locator('.sidebar-onboarding')).toBeVisible()
      await expect(
        page.locator('.sidebar-onboarding').getByRole('button', { name: 'Start a chat' }),
      ).toBeVisible()
      return
    }

    // Conversations exist: Recents renders collapsible family groups, each with
    // an expandable header and a per-section count.
    const groups = page.locator('.sidebar-group')
    expect(await groups.count()).toBeGreaterThan(0)
    await expect(groups.first().locator('.sidebar-group__count')).toBeVisible()

    // The header actually toggles: aria-expanded flips and the body visibility
    // follows. Assert the behavior, not just the attribute's presence.
    const header = groups.first().locator('.sidebar-group__header')
    const body = groups.first().locator('.sidebar-group__body')
    const startedExpanded = (await header.getAttribute('aria-expanded')) === 'true'
    await header.click()
    await expect(header).toHaveAttribute('aria-expanded', String(!startedExpanded))
    if (startedExpanded) await expect(body).toBeHidden()
    else await expect(body).toBeVisible()
    await header.click() // restore
    await expect(header).toHaveAttribute('aria-expanded', String(startedExpanded))

    for (const title of await titles.allInnerTexts()) {
      expect(title.trim().length).toBeGreaterThan(0)
      expect(title).not.toMatch(RAW_KEY_PATTERN)
      expect(title).not.toMatch(UUID_PATTERN)
    }
  })

  test('cron runs are grouped under Automations in Recents', async ({ page }) => {
    // The Sessions Hub ledger is ground truth for which kinds exist.
    await openControl(page, 'sessions')
    const cronInHub = await page.locator('.hub-row[data-kind="cron"]').count()
    test.skip(cronInHub === 0, 'No cron sessions on this gateway; seed a cron run to exercise the group')

    // Recents keeps automations in their own collapsible family group, distinct
    // from chats and channels.
    // Cron exists in the hub, so it MUST surface in the Automations group.
    // Collapsed groups keep their rows in the DOM (v-show), so a zero count is a
    // real grouping regression, not a collapse artifact — assert, don't skip.
    const cronRows = page.locator('.sidebar-history-row[data-family="automations"]')
    expect(await cronRows.count()).toBeGreaterThan(0)
    for (const title of await cronRows.locator('.sidebar-history-title').allInnerTexts()) {
      expect(title.trim().length).toBeGreaterThan(0)
      expect(title).not.toMatch(UUID_PATTERN)
    }
  })

  test('agent badge filters the flat list and clears via the agent chip', async ({ page }) => {
    await openControl(page)

    const badges = page.locator('.sidebar-agent-badge')
    test.skip((await badges.count()) === 0, 'No conversations on this gateway; seed sessions to exercise badge filtering')

    const label = await badges.first().getAttribute('aria-label')
    expect(label).toMatch(/^Filter by /)

    await badges.first().click()
    await expect(page.locator('.sidebar-agent-chip')).toBeVisible()
    await expect(badges.first()).toHaveAttribute('aria-pressed', 'true')

    // Every remaining row belongs to the filtered agent.
    for (const rowLabel of await page.locator('.sidebar-agent-badge').evaluateAll(
      nodes => nodes.map(node => node.getAttribute('aria-label')),
    )) {
      expect(rowLabel).toBe(label)
    }

    await page.locator('.sidebar-agent-chip').click()
    await expect(page.locator('.sidebar-agent-chip')).toHaveCount(0)
  })

  test('chat rows expose a rename/delete menu; non-chat rows do not', async ({ page }) => {
    await openControl(page)

    // Top-level chat rows (subagents are indented at depth > 0) carry the ⋯ menu.
    const chatRow = page
      .locator('.sidebar-history-row[data-family="chats"][data-depth="0"]')
      .first()
    test.skip((await chatRow.count()) === 0, 'No chat rows on this gateway; seed a chat to exercise the row menu')

    const menuBtn = chatRow.locator('.sidebar-row-menu-btn')
    await expect(menuBtn).toHaveAttribute('aria-haspopup', 'menu')
    await menuBtn.click()

    const menu = page.getByRole('menu')
    await expect(menu).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Rename' })).toBeVisible()
    await expect(menu.getByRole('menuitem', { name: 'Delete' })).toBeVisible()

    // Automations rows are not chats, so they never render the row menu.
    const automationRow = page.locator('.sidebar-history-row[data-family="automations"]').first()
    if ((await automationRow.count()) > 0) {
      await expect(automationRow.locator('.sidebar-row-menu-btn')).toHaveCount(0)
    }
  })

  test('Agent administration deep links without reappearing in the primary rail', async ({ page }) => {
    await openControl(page, 'agents')
    await expect(page).toHaveURL(/\/agents$/)
    await expect(page.locator('.sidebar-core').getByText('Agents', { exact: true })).toHaveCount(0)
    await expect(page.locator('.sidebar-nav-group-toggle')).toHaveCount(0)
  })

  test('only the Recents list scrolls at a 900px viewport', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await openControl(page)

    const metrics = await page.evaluate(() => {
      const pick = (selector: string) => {
        const el = document.querySelector(selector)
        if (!(el instanceof HTMLElement)) return null
        return {
          scrollHeight: el.scrollHeight,
          clientHeight: el.clientHeight,
          overflowY: getComputedStyle(el).overflowY,
        }
      }
      return {
        sidebar: pick('.sidebar'),
        core: pick('.sidebar-core'),
        list: pick('.sidebar-history-list'),
      }
    })

    // Nav chrome never overflows (1px slack for subpixel rounding).
    expect(metrics.sidebar).not.toBeNull()
    expect(metrics.sidebar!.scrollHeight).toBeLessThanOrEqual(metrics.sidebar!.clientHeight + 1)
    expect(metrics.core).not.toBeNull()
    expect(metrics.core!.scrollHeight).toBeLessThanOrEqual(metrics.core!.clientHeight + 1)

    // The Recents list is the single scroll region.
    if (metrics.list) {
      expect(metrics.list.overflowY).toBe('auto')
    }
  })

  test('Ctrl+K starts a new chat instantly', async ({ page }) => {
    await openControl(page)

    // Ctrl+K is the keyboard twin of the primary "New chat" button: it lands on
    // the draft route immediately against the preferred agent, with no picker.
    await page.keyboard.press('Control+k')

    await expect(page).toHaveURL(/\/chat\/new\?agent=[a-z0-9_-]+$/i)
    await expect(page.getByRole('dialog', { name: 'New chat' })).toHaveCount(0)
  })

  test('footer pins Settings; connection state shows in the topbar', async ({ page }) => {
    await openControl(page)

    const foot = page.locator('.sidebar-foot')
    await expect(foot.getByText('Settings', { exact: true })).toBeVisible()
    // Connection state is shown once, in the global topbar pill — not duplicated
    // in the sidebar footer.
    await expect(foot.locator('.sidebar-conn')).toHaveCount(0)
    const conn = (await page.locator('.topbar .conn-pill').innerText()).toLowerCase()
    expect(conn).toMatch(/connected|connecting/)
  })

  test('mobile drawer shows a scrim and tapping it closes the drawer', async ({ page }) => {
    await openControl(page)
    await page.setViewportSize({ width: 375, height: 667 })

    const sidebar = page.locator('.sidebar')
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.locator('.sidebar-scrim')).toBeHidden()

    await page.click('.topbar-toggle')
    await expect(sidebar).toHaveClass(/docked/)
    await expect(page.locator('.sidebar-scrim')).toBeVisible()

    // Tap outside the 280px drawer.
    await page.locator('.sidebar-scrim').click({ position: { x: 340, y: 400 } })
    await expect(sidebar).not.toHaveClass(/docked/)
    await expect(page.locator('.sidebar-scrim')).toBeHidden()
  })
})
