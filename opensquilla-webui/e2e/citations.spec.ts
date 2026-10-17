import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Inline citation pills', () => {
  test('idle chat renders no citation pills or sources row', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    await expect(page.locator('.citation-pill')).toHaveCount(0)
    await expect(page.locator('.sources-row')).toHaveCount(0)
  })

  test('rendered citation pills only ever target real sources', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Use your web search tool to find one recent headline about renewable energy, then answer in one sentence and cite the source inline as [1].')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    // The turn runs and completes.
    const ribbon = page.locator('.stream-activity')
    await expect(ribbon).toBeVisible({ timeout: 30000 })
    await expect(ribbon).toHaveCount(0, { timeout: 180000 })

    await assertCitationInvariant(page)

    // The invariant survives a reload through the history render path. Absence of
    // pills is acceptable both times — the model may not cite.
    await page.reload()
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
    await page.waitForTimeout(500)
    await assertCitationInvariant(page)
  })
})

// Invariant: every rendered pill points at a real numbered source, and
// activating one opens the paired row and brings the matching item into view.
// Pills are optional — the model may emit none — so we assert only over pills
// that exist.
async function assertCitationInvariant(page: import('@playwright/test').Page) {
  const message = page.locator('.msg-ai').filter({ has: page.locator('.sources-row') }).first()
  const pills = message.locator('.citation-pill')
  const pillCount = await pills.count()
  if (pillCount === 0) return

  const sourceCount = await message.locator('.sources-row__item, [data-source-id]').count()
  for (let i = 0; i < pillCount; i++) {
    const raw = await pills.nth(i).getAttribute('data-citation')
    expect(raw).toMatch(/^[0-9]+$/)
    const n = Number(raw)
    expect(n).toBeGreaterThanOrEqual(1)
    expect(n).toBeLessThanOrEqual(sourceCount)
  }

  // Activating the first pill opens the row and reveals its target source.
  const first = pills.first()
  const target = Number(await first.getAttribute('data-citation'))
  await first.click()
  const toggle = message.locator('.sources-row__toggle')
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  const item = message.locator(`[data-source-id="${target}"]`)
  await expect(item).toBeVisible()
}
