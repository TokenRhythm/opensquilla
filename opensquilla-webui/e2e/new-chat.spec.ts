import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('New chat draft state', () => {
  test('New chat lands on a clean draft with no session key', async ({ page }) => {
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // The primary "New chat" button opens the draft instantly against the
    // preferred agent with no picker dialog.
    await page.locator('.sidebar-new-session').click()

    await expect(page.getByRole('dialog', { name: 'New chat' })).toHaveCount(0)
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    expect(new URL(page.url()).searchParams.get('session')).toBeNull()

    // Empty transcript: landing brand, no rendered messages.
    await expect(page.locator('.chat-landing-brand')).toBeVisible()
    await expect(page.locator('.msg-user, .msg-ai')).toHaveCount(0)

    // Composer is focused and ready.
    await expect(page.locator('.chat-textarea')).toBeFocused()
  })

  test('bare /chat opens the draft instead of restoring the last session', async ({ page }) => {
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    // Seed a stored session the way a previous visit would.
    await page.evaluate(() => {
      localStorage.setItem('opensquilla_active_session', 'agent:main:webchat:seededprior')
    })

    await page.goto(CONTROL_URL + 'chat')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page).toHaveURL(/\/chat\/new$/)
    expect(page.url()).not.toContain('session=')
    await expect(page.locator('.chat-landing-brand')).toBeVisible()

    // The draft does not overwrite the stored session of the prior visit.
    const stored = await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))
    expect(stored).toBe('agent:main:webchat:seededprior')
  })

  test('legacy ?newChat=1 and ?new=1 redirect to the draft route', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat?newChat=1')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page).toHaveURL(/\/chat\/new$/)
    expect(page.url()).not.toContain('newChat=')

    await page.goto(CONTROL_URL + 'chat?new=1&agent=main')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    expect(page.url()).not.toContain('new=1')
  })

  test('new task hides Agent selection while preserving Agent deep links', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new?agent=research')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page).toHaveURL(/\/chat\/new\?agent=research$/)
    await expect(page.locator('.chat-landing-agent')).toHaveCount(0)
    await expect(page.getByRole('menu', { name: 'Choose agent' })).toHaveCount(0)
    await expect(page.locator('.chat-landing-brand')).toBeVisible()
    await expect(page.locator('.empty-state__identity')).toContainText('research')
    await expect(page.locator('.chat-textarea')).toBeFocused()
  })

  test('first send materializes the session key in the URL once', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page).toHaveURL(/\/chat\/new$/)

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Reply with the single word: ok')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    // vue-router leaves colons in query values unencoded; tolerate both forms.
    await expect(page).toHaveURL(/\/chat\?session=agent(?::|%3A)main(?::|%3A)webchat(?::|%3A)/, { timeout: 15000 })
    await expect(page.locator('.msg-user').first()).toBeVisible()
  })
})
