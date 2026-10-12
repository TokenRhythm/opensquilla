import { test, expect, type Page } from '@playwright/test'

const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const CONTROL_URL = '/control/'

async function openControl(page: Page, path = '') {
  await page.goto(CONTROL_URL + path)
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
}

test.describe('Consolidated console tables', () => {
  test('Community skills registry renders the shared DataTable', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    await openControl(page, 'skills')
    await page.getByRole('tab', { name: 'Community' }).click()
    await page.getByRole('button', { name: 'Search' }).click()
    // Either results render in a .data-table, or the empty hint shows — both pass.
    const table = page.locator('.sk-registry-table .data-table')
    const hint = page.locator('.sk-registry__hint')
    await expect(table.or(hint).first()).toBeVisible()
  })

  test('Cron run history renders the shared DataTable when a job has runs', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    await openControl(page, 'cron')
    const firstName = page.locator('[data-cron-row] .cron-card__name, [data-cron-row] .cron-link').first()
    test.skip((await firstName.count()) === 0, 'No cron jobs on this gateway; seed one to exercise run history.')
    await firstName.click()
    const runsTable = page.locator('.cron-runs-table .data-table')
    const noRuns = page.getByText('No run history yet.')
    await expect(runsTable.or(noRuns).first()).toBeVisible()
  })
})
