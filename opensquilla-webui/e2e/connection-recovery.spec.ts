import { expect, test } from '@playwright/test'

// Native Chromium rendering of the production notice/composer components.
// Transport transitions are fixture-controlled; real Gateway acceptance is separate.
test.skip(!process.env.OPENSQUILLA_DIAGRAM_ENGINE_DEV, 'Uses source fixtures through Vite')

test.beforeEach(async ({ page }) => {
  await page.goto('/e2e/fixtures/connection-recovery.html')
  await expect(page.getByTestId('evidence')).toContainText('实际连接：connected')
})

for (const seconds of [1, 3]) {
  test(`${seconds}-second reconnect stays silent and preserves the editable draft`, async ({ page }) => {
    const draft = page.locator('.chat-textarea')
    await draft.fill('Draft retained while reconnecting')
    await page.getByRole('button', { name: `短暂重连（${seconds} 秒）` }).click()
    await expect(page.getByTestId('evidence')).toContainText('实际连接：disconnected')
    await expect(draft).toBeEditable()
    await expect(page.locator('.chat-send-btn')).toBeDisabled()
    await expect(page.getByTestId('evidence')).toContainText('实际连接：connected', { timeout: 6_000 })
    await expect(page.getByTestId('evidence')).toContainText('顶栏提示出现：0 次；聊天提示出现：0 次')
    await expect(draft).toHaveValue('Draft retained while reconnecting')
    await expect(page.locator('.chat-send-btn')).toBeEnabled()
  })
}

test('persistent failure reveals recovery controls; recovery removes them without losing the draft', async ({ page }, testInfo) => {
  const draft = page.locator('.chat-textarea')
  const text = await draft.inputValue()
  await page.getByRole('button', { name: '持续断线', exact: true }).click()
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(page.getByTestId('connection-status')).toBeVisible({ timeout: 7_000 })
  await expect(page.getByTestId('chat-session-recovery-status')).toBeVisible()
  await page.getByTestId('connection-status').click()
  await expect(page.getByTestId('evidence')).toContainText('连接入口：已打开')
  await page.screenshot({ path: testInfo.outputPath('persistent-connection.png'), fullPage: true })
  await page.getByRole('button', { name: '恢复连接', exact: true }).click()
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  await expect(page.getByTestId('chat-session-recovery-status')).toHaveCount(0)
  await expect(draft).toHaveValue(text)
})

test('a rejected recovery or attempted blocked send gives immediate, local feedback', async ({ page }, testInfo) => {
  await page.getByRole('button', { name: '持续断线', exact: true }).click()
  await page.locator('.chat-send-control').focus()
  await expect(page.getByRole('tooltip')).toBeVisible()
  await expect(page.getByTestId('evidence')).toContainText('发送：0 次')
  await expect(page.getByTestId('connection-status')).toHaveCount(0)
  await page.getByRole('button', { name: '恢复被拒绝', exact: true }).click()
  await expect(page.getByTestId('connection-status')).toBeVisible({ timeout: 1_000 })
  await expect(page.getByTestId('chat-session-recovery-status')).toBeVisible({ timeout: 1_000 })
  await page.screenshot({ path: testInfo.outputPath('blocked-connection.png'), fullPage: true })
})
