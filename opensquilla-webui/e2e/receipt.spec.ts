import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Assistant receipt meta', () => {
  test('draft chat renders no receipt meta or usage popover', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    await expect(page.locator('.msg-ai-meta')).toHaveCount(0)
    await expect(page.locator('.msg-meta-popover')).toHaveCount(0)
  })

  test('live run keeps cost and savings inline and tucks token details into the popover', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    await page.locator('.chat-textarea').fill('Reply with the single word ok.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const meta = page.locator('.msg-ai .msg-ai-meta').last()
    await expect(meta).toBeVisible({ timeout: 180000 })

    // Cost and savings stay inline on the meta line — no interaction needed.
    await expect(meta.locator('.msg-meta__cost')).toContainText('$', { timeout: 60000 })
    await expect(meta.locator('.savings-indicator')).toBeVisible()
    await expect(meta.locator('.savings-indicator')).toContainText(/Saved ~\d+%/)

    // Token/cache/think details are no longer inline on the meta line.
    await expect(meta).not.toContainText('cache:')
    await expect(meta).not.toContainText('think:')
    await expect(meta).not.toContainText('↑')

    // The usage popover opens from a focusable trigger and lists token details.
    const trigger = meta.getByRole('button', { name: 'Usage details' })
    await expect(trigger).toBeVisible()
    await trigger.click()
    const popover = meta.locator('.msg-meta-popover')
    await expect(popover).toBeVisible()
    await expect(popover.locator('.msg-meta-popover__label').first()).toHaveText('tokens')
    await expect(popover).toContainText('↑')
    const labels = await popover.locator('.msg-meta-popover__label').allTextContents()
    for (const label of labels) {
      expect(['tokens', 'cache', 'think']).toContain(label)
    }

    // Escape closes the popover and returns focus to the trigger.
    await page.keyboard.press('Escape')
    await expect(popover).toHaveCount(0)
    await expect(trigger).toBeFocused()

    // At 390px the receipt stays a single compact row.
    await page.setViewportSize({ width: 390, height: 844 })
    const box = await meta.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.height).toBeLessThanOrEqual(30)
  })
})
