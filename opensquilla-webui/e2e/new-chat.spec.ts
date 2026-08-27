import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const RECENT_DRAFT_SESSION_KEY = 'opensquilla.chat.recent-draft-session'

async function seedRecoverableDraft(
  page: import('@playwright/test').Page,
  key: string,
  text: string,
  active = false,
) {
  await page.goto(CONTROL_URL)
  await page.evaluate(({ key, text, active, pointerKey }) => {
    localStorage.setItem(`opensquilla.chat.draft:${key}`, text)
    localStorage.setItem(pointerKey, key)
    if (active) localStorage.setItem('opensquilla_active_session', key)
  }, { key, text, active, pointerKey: RECENT_DRAFT_SESSION_KEY })
}

test.describe('New chat draft state', () => {
  test('cold /chat/new restores the most recent provisional draft', async ({ page }) => {
    const key = 'agent:main:webchat:e2e-cold-draft'
    await seedRecoverableDraft(page, key, 'unfinished cold-start task')

    await page.goto(CONTROL_URL + 'chat/new')

    await expect(page).toHaveURL(/\/chat\/new$/)
    await expect(page.locator('.chat-textarea')).toHaveValue('unfinished cold-start task')
  })

  test('cold /chat/new restores an existing session draft and its route', async ({ page }) => {
    const key = 'agent:main:webchat:e2e-existing-draft'
    await seedRecoverableDraft(page, key, 'unfinished existing reply', true)

    await page.goto(CONTROL_URL + 'chat/new')

    await expect(page).toHaveURL(/\/chat\?session=agent(?::|%3A)main/)
    await expect(page.locator('.chat-textarea')).toHaveValue('unfinished existing reply')
  })

  test('explicit New task discards a recovered draft instead of reviving it', async ({ page }) => {
    const key = 'agent:main:webchat:e2e-discarded-draft'
    await seedRecoverableDraft(page, key, 'discard this task')
    await page.goto(CONTROL_URL + 'chat/new')
    await expect(page.locator('.chat-textarea')).toHaveValue('discard this task')

    await page.locator('.sidebar-new-session').click()

    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    await expect(page.locator('.chat-textarea')).toHaveValue('')
    await expect.poll(() => page.evaluate(({ key, pointerKey }) => ({
      draft: localStorage.getItem(`opensquilla.chat.draft:${key}`),
      pointer: localStorage.getItem(pointerKey),
    }), { key, pointerKey: RECENT_DRAFT_SESSION_KEY })).toEqual({
      draft: null,
      pointer: null,
    })
  })

  test('explicit New task preserves another session draft', async ({ page }) => {
    const otherKey = 'agent:main:webchat:e2e-other-draft'
    await seedRecoverableDraft(page, otherKey, 'keep the other draft')
    await page.goto(CONTROL_URL + 'chat?session=agent:main:webchat:e2e-current-session')
    await expect(page.locator('.chat-textarea')).toHaveValue('')

    await page.locator('.sidebar-new-session').click()

    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    expect(await page.evaluate(({ key, pointerKey }) => ({
      draft: localStorage.getItem(`opensquilla.chat.draft:${key}`),
      pointer: localStorage.getItem(pointerKey),
    }), { key: otherKey, pointerKey: RECENT_DRAFT_SESSION_KEY })).toEqual({
      draft: 'keep the other draft',
      pointer: otherKey,
    })
  })

  test('clearing a recovered composer prevents it from returning after reload', async ({ page }) => {
    const key = 'agent:main:webchat:e2e-cleared-draft'
    await seedRecoverableDraft(page, key, 'clear before reload')
    await page.goto(CONTROL_URL + 'chat/new')
    const textarea = page.locator('.chat-textarea')
    await expect(textarea).toHaveValue('clear before reload')

    await textarea.fill('')
    await expect.poll(() => page.evaluate(pointerKey => (
      localStorage.getItem(pointerKey)
    ), RECENT_DRAFT_SESSION_KEY)).toBeNull()
    await page.reload()

    await expect(textarea).toHaveValue('')
  })

  test('a corrupt recovery pointer is retired without blocking a clean draft', async ({ page }) => {
    await page.goto(CONTROL_URL)
    await page.evaluate(pointerKey => {
      localStorage.setItem(pointerKey, 'not-a-session')
    }, RECENT_DRAFT_SESSION_KEY)

    await page.goto(CONTROL_URL + 'chat/new')

    await expect(page.locator('.chat-textarea')).toHaveValue('')
    expect(await page.evaluate(pointerKey => (
      localStorage.getItem(pointerKey)
    ), RECENT_DRAFT_SESSION_KEY)).toBeNull()
  })

  test('an explicit prefill wins over a recoverable draft', async ({ page }) => {
    const key = 'agent:main:webchat:e2e-prefill-draft'
    await seedRecoverableDraft(page, key, 'stale draft text')
    await page.evaluate(() => {
      window.history.replaceState(
        { ...window.history.state, prefill: 'explicit prefill text' },
        '',
        '/control/chat/new?agent=main',
      )
    })

    await page.reload()

    await expect(page.locator('.chat-textarea')).toHaveValue('explicit prefill text')
    const storage = await page.evaluate(({ key, pointerKey }) => {
      const pointer = localStorage.getItem(pointerKey)
      return {
        oldDraft: localStorage.getItem(`opensquilla.chat.draft:${key}`),
        pointer,
        explicitDraft: pointer
          ? localStorage.getItem(`opensquilla.chat.draft:${pointer}`)
          : null,
      }
    }, { key, pointerKey: RECENT_DRAFT_SESSION_KEY })
    expect(storage.oldDraft).toBeNull()
    expect(storage.pointer).not.toBe(key)
    expect(storage.explicitDraft).toBe('explicit prefill text')
  })

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
