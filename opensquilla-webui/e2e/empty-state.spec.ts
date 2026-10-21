import { test, expect, type Page } from '@playwright/test'
import { expectComposerValue } from './utils'
import { installSidebarFixture } from './support/sidebar-fixture'

const CONTROL_URL = '/control/'

async function openDraft(page: Page) {
  await page.goto(CONTROL_URL + 'chat/new')
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
  await page.waitForSelector('.topbar[data-connection-state="connected"]', { timeout: 10000 }).catch(() => {})
}

async function captureSuggestedTask(page: Page) {
  const sends: Record<string, unknown>[] = []
  await installSidebarFixture(page, {
    'onboarding.status': { llmConfigured: true, hasConfig: true, searchConfigured: true },
    'chat.send': (params: Record<string, unknown>) => {
      sends.push(params)
      return { accepted: true, session: params.sessionKey, task_id: 'suggested-task' }
    },
  })
  return sends
}

test.describe('Empty draft state', () => {
  test('draft greets with at least 3 chips without redundant identity or brand marks', async ({ page }) => {
    await openDraft(page)

    await expect(page.locator('.empty-state__greeting')).toHaveText(/Good (morning|afternoon|evening)\./)
    await expect(page.locator('.empty-state__identity')).toHaveCount(0)

    // Brand lives in the sidebar chrome only: no wordmark, no mark in the content area.
    await expect(page.locator('.chat-landing-lockup')).toHaveCount(0)
    await expect(page.locator('.empty-state__mark')).toHaveCount(0)
    await expect(page.locator('.msg-ai-avatar')).toHaveCount(0)

    expect(await page.locator('.empty-state__chip').count()).toBeGreaterThanOrEqual(3)
  })

  test('chip click submits exactly one task through the composer', async ({ page }) => {
    const sends = await captureSuggestedTask(page)
    await openDraft(page)

    const chip = page.locator('.empty-state__chip').first()
    const text = (await chip.innerText()).trim()
    await chip.click()

    await expect.poll(() => sends.length).toBe(1)
    expect(sends[0]?.message).toBe(text)
    await expect(page.locator('.msg-user').filter({ hasText: text })).toHaveCount(1)
    await expectComposerValue(page.locator('.chat-textarea'), '')
    await expect(page.locator('.empty-state__greeting')).toHaveCount(0)
  })

  test('renders and submits once at 390px', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const sends = await captureSuggestedTask(page)
    await openDraft(page)

    await expect(page.locator('.empty-state__greeting')).toBeVisible()
    expect(await page.locator('.empty-state__chip').count()).toBeGreaterThanOrEqual(3)

    const chip = page.locator('.empty-state__chip').first()
    const text = (await chip.innerText()).trim()
    await chip.click()

    await expect.poll(() => sends.length).toBe(1)
    expect(sends[0]?.message).toBe(text)
    await expectComposerValue(page.locator('.chat-textarea'), '')
    await expect(page.locator('.msg-user').filter({ hasText: text })).toHaveCount(1)
  })

  test('legacy Sessions entry redirects to chat and sends once through the shared composer', async ({ page }) => {
    const sends = await captureSuggestedTask(page)
    await page.goto(CONTROL_URL + 'sessions')
    await expect(page).toHaveURL(/\/control\/chat\/new$/)
    await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'connected')

    const taskText = 'Summarize the latest gateway logs'
    await page.locator('.chat-textarea').fill(taskText)
    await page.getByRole('button', { name: 'Send', exact: true }).click()

    await expect(page).toHaveURL(/\/chat/)
    await expect.poll(() => sends.length).toBe(1)
    expect(sends[0]?.message).toBe(taskText)
    await expectComposerValue(page.locator('.chat-textarea'), '')
    await expect(
      page.locator('.msg-user').filter({ hasText: taskText }),
    ).toHaveCount(1, { timeout: 15000 })
    await expect(page.locator('.empty-state__greeting')).toHaveCount(0)
  })
})
