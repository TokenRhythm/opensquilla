import { expect, test } from '@playwright/test'

test('Web Settings opens the Sandbox overview and file-safety details', async ({ page }) => {
  await page.goto('/control/')
  await page.waitForSelector('.conn-pill', { timeout: 10_000 })

  await page.locator('.sidebar-fn-item[data-icon="settings"]').click()
  const settings = page.getByRole('dialog', { name: 'Settings' })
  await expect(settings).toBeVisible()

  const sandboxTab = settings.getByRole('tab', { name: 'Sandbox', exact: true })
  await expect(sandboxTab).toBeVisible()
  await sandboxTab.click()
  await expect(page).toHaveURL(/\/settings\/sandbox$/)

  await expect(page.getByTestId('sandbox-overview')).toBeVisible()
  await expect(page.getByTestId('sandbox-safe-mode')).toBeVisible()
  await expect(page.getByTestId('sandbox-full-mode')).toBeVisible()
  await expect(page.locator('.sandbox-settings__status')).toBeVisible()

  await page.getByTestId('sandbox-open-files').click()
  await expect(page.getByTestId('sandbox-detail')).toBeVisible()
  await expect(page.getByTestId('builtin-file-rules')).toBeVisible()
  await expect(page.getByTestId('sandbox-backup-quota')).toBeVisible()

  await page.getByTestId('sandbox-detail-back').click()
  await expect(page.getByTestId('sandbox-overview')).toBeVisible()
})
