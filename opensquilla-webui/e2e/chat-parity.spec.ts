import { test, expect } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'

const CONTROL_CHAT_URL = '/control/chat'

test.describe('Chat parity controls', () => {
  test('composer keeps low-frequency actions in one compact menu', async ({ page }) => {
    await installSidebarFixture(page, { 'sessions.promptCacheKeepalive.status': {} })
    await page.goto(CONTROL_CHAT_URL)
    await expect(page.locator('.chat-composer')).toBeVisible()

    await page.locator('.chat-composer').getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByRole('menuitem', { name: 'Composer settings' })).toHaveCount(0)
    await expect(page.getByRole('group', { name: 'More', exact: true }).getByRole('menuitem')).toHaveCount(3)
    await expect(page.getByRole('menuitem', { name: 'Export as Markdown' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Prompt cache keepalive' })).toBeDisabled()
  })
})
