import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const MOBILE_VIEWPORT = { width: 390, height: 844 }

async function openMobileChat(page: Page) {
  await page.setViewportSize(MOBILE_VIEWPORT)
  await page.goto(CONTROL_URL + 'chat')
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
}

test.describe('Mobile bottom tab bar', () => {
  test('tabs are visible and navigate between Chat, Sessions, Overview, and More', async ({ page }) => {
    await openMobileChat(page)

    const tabbar = page.locator('.mobile-tabbar')
    await expect(tabbar).toBeVisible()
    await expect(tabbar.locator('.mobile-tab')).toHaveCount(4)
    await expect(tabbar.getByRole('link', { name: 'Agents' })).toHaveCount(0)

    // Chat is the active tab on the chat route.
    const chatTab = tabbar.getByRole('link', { name: 'Chat' })
    await expect(chatTab).toHaveClass(/is-active/)

    await tabbar.getByRole('link', { name: 'Sessions' }).click()
    await expect(page).toHaveURL(/\/sessions$/)
    await expect(tabbar.getByRole('link', { name: 'Sessions' })).toHaveClass(/is-active/)
    await expect(chatTab).not.toHaveClass(/is-active/)

    // Overview fronts Status/Usage and remains active for diagnostic Logs.
    await tabbar.getByRole('link', { name: 'Overview' }).click()
    await expect(page).toHaveURL(/\/overview$/)
    await expect(tabbar.getByRole('link', { name: 'Overview' })).toHaveClass(/is-active/)

    await chatTab.click()
    await expect(page).toHaveURL(/\/chat/)
    await expect(chatTab).toHaveClass(/is-active/)
  })

  test('every tab target meets the 44px minimum', async ({ page }) => {
    await openMobileChat(page)

    for (const tab of await page.locator('.mobile-tabbar .mobile-tab').all()) {
      const box = await tab.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.height).toBeGreaterThanOrEqual(44)
      expect(box!.width).toBeGreaterThanOrEqual(44)
    }
  })

  test('More opens the sidebar drawer; the scrim closes it', async ({ page }) => {
    await openMobileChat(page)

    const more = page.locator('.mobile-tabbar').getByRole('button', { name: 'More' })
    await more.click()
    await expect(page.locator('.sidebar.docked')).toBeVisible()
    await expect(page.locator('.sidebar-scrim')).toBeVisible()
    await expect(page.locator('.sidebar-core .sidebar-fn-label')).toHaveText([
      'Sessions', 'Overview', 'Skills & Channels', 'Cron',
    ])

    // Skills & Channels and Cron live in this same flat drawer instead of a
    // nested disclosure. Selecting a row closes the drawer and navigates normally.
    await page.locator('.sidebar-core').getByText('Skills & Channels', { exact: true }).click()
    await expect(page).toHaveURL(/\/skills$/)
    await expect(page.locator('.sidebar-scrim')).toHaveCount(0)
    await expect(more).toHaveClass(/is-active/)

    // Tap outside the drawer to dismiss it.
    await more.click()
    await expect(page.locator('.sidebar-scrim')).toBeVisible()
    await page.locator('.sidebar-scrim').click({ position: { x: 350, y: 400 } })
    await expect(page.locator('.sidebar-scrim')).toHaveCount(0)
  })

  test('More stays active on Skills, Channels, and Cron routes', async ({ page }) => {
    await openMobileChat(page)
    const more = page.locator('.mobile-tabbar').getByRole('button', { name: 'More' })
    const overview = page.locator('.mobile-tabbar').getByRole('link', { name: 'Overview' })

    for (const path of ['skills', 'channels', 'cron']) {
      await page.goto(CONTROL_URL + path)
      await expect(more).toHaveClass(/is-active/)
      await expect(overview).not.toHaveClass(/is-active/)
    }
  })

  test('Skills and Channels hub links are equal 44px targets on mobile', async ({ page }) => {
    await openMobileChat(page)
    await page.goto(CONTROL_URL + 'skills')

    const hub = page.getByRole('navigation', { name: 'Skills & Channels' })
    const links = hub.getByRole('link')
    await expect(links).toHaveCount(2)
    const skillsBox = await links.nth(0).boundingBox()
    const channelsBox = await links.nth(1).boundingBox()
    expect(skillsBox).not.toBeNull()
    expect(channelsBox).not.toBeNull()
    expect(skillsBox!.height).toBeGreaterThanOrEqual(44)
    expect(channelsBox!.height).toBeGreaterThanOrEqual(44)
    expect(Math.abs(skillsBox!.width - channelsBox!.width)).toBeLessThanOrEqual(1)

    await links.nth(1).click()
    await expect(page).toHaveURL(/\/channels$/)
    await expect(links.nth(1)).toHaveAttribute('aria-current', 'page')
  })

  test('the chat composer sits above the tab bar, not behind it', async ({ page }) => {
    await page.setViewportSize(MOBILE_VIEWPORT)
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const composer = page.locator('.chat-composer')
    await expect(composer).toBeVisible()

    const composerBox = await composer.boundingBox()
    const tabbarBox = await page.locator('.mobile-tabbar').boundingBox()
    expect(composerBox).not.toBeNull()
    expect(tabbarBox).not.toBeNull()
    expect(composerBox!.y + composerBox!.height).toBeLessThanOrEqual(tabbarBox!.y + 1)
  })

  test('desktop shows no tab bar', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(CONTROL_URL + 'chat')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.mobile-tabbar')).toBeHidden()
  })
})
