import { test, expect } from '@playwright/test'
import { installSidebarFixture, SIDEBAR_SESSIONS } from './support/sidebar-fixture'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Chat Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(CONTROL_URL)
    // Wait for WebSocket connection
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
  })

  test('page loads with correct title', async ({ page }) => {
    await expect(page).toHaveTitle(/OpenSquilla/)
  })

  test('sidebar core shows the flat primary navigation', async ({ page }) => {
    const core = page.locator('.sidebar-core')

    // Chat stays the dedicated New-chat action. Long-lived Agent management and
    // the old Build disclosure are intentionally absent from the primary rail.
    await expect(core.getByText('Chat', { exact: true })).toHaveCount(0)
    // Sessions is routed but off the nav; New task leads the index as an action
    // row (its own class) rather than a destination.
    await expect(core.locator('> .sidebar-new-session')).toHaveText(/New task/)
    await expect(core.locator('> .sidebar-fn-item .sidebar-fn-label')).toHaveText(
      ['Skills & Channels', 'Cron', 'View usage'],
    )
    await expect(core.getByText('Sessions', { exact: true })).toHaveCount(0)
    await expect(core.getByText('Agents', { exact: true })).toHaveCount(0)
    await expect(core.locator('.sidebar-nav-group-toggle')).toHaveCount(0)
  })

  test('command palette opens on recent tasks, not on a list of destinations', async ({ page }) => {
    const recent = SIDEBAR_SESSIONS[0]!
    await installSidebarFixture(page, {
      'sessions.list': { sessions: [recent], count: 1, ts: 1_800_000_000, has_more: false },
    })
    await page.goto(CONTROL_URL)
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page.locator(`.sidebar-history-row[data-session-key="${recent.key}"]`)).toBeVisible()

    await page.locator('.sidebar-cmd-btn').click()
    const palette = page.getByRole('dialog', { name: 'Search and go to' })
    await expect(palette).toBeVisible()

    // Untyped state answers "which task?" — the button promises task search, so
    // destinations must not be the resting content.
    await expect(palette.locator('.cmdp-group-label')).toHaveText(['Recent tasks'])
    await expect(palette.locator('.cmdp-option__label')).toHaveText([recent.title])
    for (const name of ['View usage', 'Skills & Channels', 'Cron']) {
      await expect(palette.getByRole('option', { name, exact: true })).toHaveCount(0)
    }
  })

  test('command palette keeps the Skills & Channels hub together in Work', async ({ page }) => {
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    await expect(page.locator('.chat-textarea')).toBeFocused()
    await page.locator('.sidebar-cmd-btn').click()
    const palette = page.getByRole('dialog', { name: 'Search and go to' })
    await expect(palette).toBeVisible()
    const search = palette.getByRole('combobox')
    await expect(search).toBeFocused()

    // Destinations surface by name rather than by default, so the grouping
    // contract is asserted against a query that matches the whole hub.
    await search.fill('channels')
    await expect(search).toHaveValue('channels')
    await expect(search).toBeFocused()
    for (const name of ['Skills & Channels', 'Channels']) {
      await expect(palette.getByRole('option', { name, exact: true })).toBeVisible()
    }
    const labels = await palette.locator('.cmdp-option__label').allTextContents()
    expect(labels.indexOf('Channels')).toBe(labels.indexOf('Skills & Channels') + 1)
    await expect(palette.getByRole('option', { name: 'Agents', exact: true })).toHaveCount(0)
    await expect(palette.locator('.cmdp-group-label', { hasText: /^Build$/ })).toHaveCount(0)

    // Usage remains a Work destination after the diagnostic pages retire.
    await search.fill('usage')
    await expect(search).toHaveValue('usage')
    await expect(palette.locator('.cmdp-group-label', { hasText: /^Work$/ })).toBeVisible()
    await expect(palette.getByRole('option', { name: 'View usage', exact: true })).toBeVisible()

    for (const name of ['Overview', 'Logs']) {
      await search.fill(name)
      await expect(search).toHaveValue(name)
      await expect(palette.getByRole('option', { name, exact: true })).toHaveCount(0)
    }
  })

  test('Usage and Skills & Channels own disjoint route families', async ({ page }) => {
    const usage = page.locator('.sidebar-core').getByRole('link', { name: 'View usage' })
    const skillsChannels = page.locator('.sidebar-core').getByRole('link', { name: 'Skills & Channels' })
    for (const path of ['overview', 'health', 'usage']) {
      await page.goto(CONTROL_URL + path)
      await expect(page).toHaveURL(/\/usage$/)
      await expect(usage).toHaveClass(/is-active/)
      await expect(usage).toHaveAttribute('aria-current', 'page')
      await expect(skillsChannels).not.toHaveClass(/is-active/)
    }
    for (const path of ['skills', 'channels']) {
      await page.goto(CONTROL_URL + path)
      await expect(skillsChannels).toHaveClass(/is-active/)
      await expect(skillsChannels).toHaveAttribute('aria-current', 'page')
      await expect(usage).not.toHaveClass(/is-active/)
    }
  })

  test('Skills and Channels use canonical route links through history and reload', async ({ page }) => {
    await page.goto(CONTROL_URL + 'skills')
    let hub = page.getByRole('navigation', { name: 'Skills & Channels' })
    const skills = hub.getByRole('link', { name: 'Skills', exact: true })
    const channels = hub.getByRole('link', { name: 'Channels', exact: true })

    await expect(skills).toHaveAttribute('aria-current', 'page')
    await expect(channels).not.toHaveAttribute('aria-current', 'page')
    await channels.click()
    await expect(page).toHaveURL(/\/channels$/)
    await expect(channels).toHaveAttribute('aria-current', 'page')

    await page.goBack()
    await expect(page).toHaveURL(/\/skills$/)
    await expect(skills).toHaveAttribute('aria-current', 'page')

    await page.reload()
    hub = page.getByRole('navigation', { name: 'Skills & Channels' })
    await expect(hub.getByRole('link', { name: 'Skills', exact: true }))
      .toHaveAttribute('aria-current', 'page')
  })

  test('Usage is a standalone destination through reload and history', async ({ page }) => {
    await page.goto(CONTROL_URL + 'usage')
    await expect(page).toHaveURL(/\/usage$/)
    await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible()
    await expect(page.locator('.route-hub__tabs')).toHaveCount(0)
    await expect(page.locator('.ov-stage, .lg-stage')).toHaveCount(0)

    await page.locator('.sidebar-core').getByRole('link', { name: 'Skills & Channels' }).click()
    await expect(page).toHaveURL(/\/skills$/)
    await page.goBack()
    await expect(page).toHaveURL(/\/usage$/)
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible()
    await expect(page.locator('.route-hub__tabs')).toHaveCount(0)
  })

  test('can navigate between views', async ({ page }) => {
    const core = page.locator('.sidebar-core')

    await core.getByText('View usage', { exact: true }).click()
    await expect(page).toHaveURL(/\/usage/)

    await core.getByText('Skills & Channels', { exact: true }).click()
    await expect(page).toHaveURL(/\/skills/)

    await core.getByText('Cron', { exact: true }).click()
    await expect(page).toHaveURL(/\/cron/)

    // New task is instant (no modal): the primary row drops straight to a
    // draft. `exact` matches the New-task button precisely.
    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(page.getByRole('dialog', { name: 'New chat' })).toHaveCount(0)
    await expect(page).toHaveURL(/\/chat\/new\?agent=[a-z0-9_-]+$/i)
  })

  test('sidebar conversation list is free of raw identifiers', async ({ page }) => {
    // The family filter chips were retired with the Recents-only sidebar.
    await expect(page.locator('.sidebar-filter-chip')).toHaveCount(0)

    // Let the session list settle before inspecting sidebar text.
    await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
    await page.waitForTimeout(800)
    await expect(page.locator('.sidebar-history-list, .sidebar-onboarding, .sidebar-history-empty').first()).toBeVisible()

    const sidebarText = await page.locator('.sidebar').innerText()
    expect(sidebarText).not.toMatch(/agent:[a-z0-9_-]+:[a-z0-9_-]+:/i)
    expect(sidebarText).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i)

    // Contract-gap chips stay hidden unless the debug feature flag is on.
    await expect(page.locator('.sidebar-history-gap')).toHaveCount(0)
  })

  test('theme menu picks a mode directly', async ({ page }) => {
    const html = page.locator('html')

    await page.evaluate(() => localStorage.setItem('opensquilla-theme', 'light'))
    await page.reload()
    await page.waitForSelector('.topbar .conn-pill', { timeout: 10000 })
    await expect(html).toHaveAttribute('data-theme', 'light')

    const themeButton = page.getByRole('button', { name: 'Theme', exact: true })
    await themeButton.click()
    const menu = page.getByRole('menu', { name: 'Theme' })
    await expect(menu).toBeVisible()
    await expect(menu.getByRole('menuitemradio', { name: 'Light' })).toHaveAttribute('aria-checked', 'true')

    await menu.getByRole('menuitemradio', { name: 'Dark' }).click()
    await expect(html).toHaveAttribute('data-theme', 'dark')
    await expect(menu).toHaveCount(0)

    // Escape closes without changing the mode.
    await themeButton.click()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('menu', { name: 'Theme' })).toHaveCount(0)
    await expect(html).toHaveAttribute('data-theme', 'dark')
  })

  test('chat input area is visible', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat')

    await expect(page.locator('.chat-textarea')).toBeVisible()
    await expect(page.locator('.chat-composer')).toBeVisible()
  })

  test('connection status shows connected', async ({ page }) => {
    const connPill = page.locator('.topbar .conn-pill')
    await expect(connPill).toBeVisible()

    const text = await connPill.textContent()
    expect(text?.toLowerCase()).toMatch(/connected|connecting/)
  })

  test('no console errors on load', async ({ page }) => {
    const errors: string[] = []
    page.on('console', msg => {
      if (msg.type() === 'error') {
        errors.push(msg.text())
      }
    })

    await page.goto(CONTROL_URL)
    await page.waitForLoadState('networkidle')

    // Filter out non-critical errors
    const criticalErrors = errors.filter(e =>
      !e.includes('Source map') &&
      !e.includes('favicon') &&
      !e.includes('net::ERR_BLOCKED_BY_CLIENT')
    )

    expect(criticalErrors).toHaveLength(0)
  })
})

test.describe('Chat Interaction', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
  })

  test('can type in chat input', async ({ page }) => {
    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Hello, this is a test message')
    await expect(textarea).toHaveValue('Hello, this is a test message')
  })

  test('keeps slash selection visible and closes the menu on outside pointerdown', async ({ page }) => {
    const textarea = page.locator('.chat-textarea')
    const menu = page.locator('.chat-slash')
    const scrollList = menu.locator('.chat-slash-list')

    await textarea.fill('/')
    await expect(menu).toBeVisible()
    await expect(menu.locator('.chat-slash-name--command').first()).toBeVisible()
    await expect(menu.locator('.chat-slash-empty[role="status"]')).toHaveCount(0)

    const items = menu.locator('.chat-slash-item')
    const itemCount = await items.count()
    expect(itemCount).toBeGreaterThan(1)
    await expect.poll(() => scrollList.evaluate(element => element.scrollHeight > element.clientHeight))
      .toBe(true)

    for (let index = 1; index < itemCount; index += 1) {
      await textarea.press('ArrowDown')
    }

    const activeItem = menu.locator('.chat-slash-item--active')
    await expect(activeItem).toHaveCount(1)
    await expect(items.last()).toHaveClass(/chat-slash-item--active/)
    await expect.poll(() => scrollList.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
    await expect.poll(() => scrollList.evaluate(element => {
      const active = element.querySelector<HTMLElement>('.chat-slash-item--active')
      if (!active) return false
      const menuRect = element.getBoundingClientRect()
      const activeRect = active.getBoundingClientRect()
      // Chromium rounds scroll offsets while these rectangles retain subpixels.
      return activeRect.top >= menuRect.top - 1 && activeRect.bottom <= menuRect.bottom + 1
    })).toBe(true)

    await activeItem.dispatchEvent('pointerdown', { pointerType: 'mouse', button: 0 })
    await expect(menu).toBeVisible()

    await page.locator('.chat-thread').click({ position: { x: 32, y: 8 } })
    await expect(menu).toHaveCount(0)
    await expect(textarea).toHaveValue('/')
  })

  test('sidebar toggle works on mobile viewport', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })

    // Mobile collapses the sidebar to an overlay; the topbar toggle reopens it.
    const sidebar = page.locator('.sidebar')
    await expect(sidebar).not.toHaveClass(/docked/)

    await page.click('.topbar-toggle')
    await expect(sidebar).toHaveClass(/docked/)

    await page.click('.sidebar-brand .sidebar-dock-toggle')
    await expect(sidebar).not.toHaveClass(/docked/)
  })
})

test.describe('Visual Regression', () => {
  // Live runs seed real sessions into the sidebar, so the pixel baselines
  // only hold against a clean instance (the default, non-live suite).
  test.skip(LIVE, 'Visual baselines assume a clean sidebar; skipped in live runs.')

  // Live data and wall-clock content are masked so the baseline pins the
  // chrome: sidebar core/footer, composer, and chat surface.
  const dynamicRegions = (page: import('@playwright/test').Page) => [
    page.locator('.sidebar-history'),
    page.locator('.empty-state__greeting'),
  ]

  test('chat page screenshot matches baseline', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await page.waitForTimeout(500) // Let animations settle

    await expect(page).toHaveScreenshot('chat-page.png', {
      maxDiffPixels: 100,
      mask: dynamicRegions(page),
    })
  })

  test('dark mode screenshot', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // Force dark theme
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-theme', 'dark')
      localStorage.setItem('opensquilla-theme', 'dark')
    })
    await page.waitForTimeout(300)

    await expect(page).toHaveScreenshot('chat-page-dark.png', {
      maxDiffPixels: 100,
      mask: dynamicRegions(page),
    })
  })
})
