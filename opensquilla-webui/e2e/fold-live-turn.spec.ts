import { test, expect } from '@playwright/test'

// End-to-end proof for the sole append-only live-turn projection. It drives the
// same real stream path as the other live specs and verifies that activity and
// tool rows survive the live-to-history transition.
const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Append-only live turn', () => {
  test('fold drives the flat activity timeline and tool rows for a live search run', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Use your web search tool to find one recent headline about space exploration, then answer in one sentence.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const liveActivity = page.locator('.assistant-activity--live')
    await expect(liveActivity).toBeVisible({ timeout: 30000 })
    await expect(page.locator('.work-card')).toHaveCount(0)

    const sawChecklistRow = await page.evaluate(async () => {
      const t0 = Date.now()
      while (Date.now() - t0 < 180000) {
        const rows = document.querySelectorAll('.assistant-activity--live .tool-timeline--checklist .tool-row').length
        if (rows > 0) return true
        if (document.querySelector('.assistant-activity--live') === null) return false
        await new Promise(resolve => setTimeout(resolve, 150))
      }
      return false
    })
    expect(sawChecklistRow).toBe(true)

    // Run completes: live activity collapses, the transcript keeps the rows.
    await expect(liveActivity).toHaveCount(0, { timeout: 180000 })
    const activity = page.locator('.msg-ai .assistant-activity').first()
    await expect(activity).toBeVisible()
    const summary = activity.locator('.assistant-activity__summary')
    await expect(summary).toHaveAttribute('aria-expanded', 'false')
    await summary.press('Enter')
    await expect(summary).toHaveAttribute('aria-expanded', 'true')
    const searchRow = page.locator('.msg-ai .tool-row[data-op="web.search"]').first()
    await expect(searchRow).toBeVisible()
    await expect(searchRow).toHaveAttribute('aria-expanded', 'false')
  })
})
