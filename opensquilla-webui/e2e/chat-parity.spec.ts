import { test, expect } from '@playwright/test'

const CONTROL_CHAT_URL = '/control/chat'

test.describe('Chat parity controls', () => {
  test('composer keeps low-frequency actions in one compact menu', async ({ page }) => {
    await page.goto(CONTROL_CHAT_URL)
    await expect(page.locator('.chat-composer')).toBeVisible()

    await page.getByRole('button', { name: 'More' }).click()
    await expect(page.getByRole('menuitem', { name: 'Composer settings' })).toHaveCount(0)
    await expect(page.locator('.chat-more-actions-menu [role="menuitem"]')).toHaveCount(3)
    await expect(page.getByRole('menuitem', { name: 'Export as Markdown' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Prompt cache keepalive' })).toBeDisabled()
  })
})
