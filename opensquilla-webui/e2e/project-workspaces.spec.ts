import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'

async function openControl(page: Page) {
  await page.goto(CONTROL_URL)
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
  await expect(
    page.locator('.sidebar-history-list, .sidebar-history-empty, .sidebar-onboarding').first(),
  ).toBeVisible()
}

test.describe('Project workspaces', () => {
  test('offers project selection from both the sidebar and an ordinary draft', async ({ page }) => {
    await openControl(page)

    await expect(
      page.locator('.sidebar-actions').getByRole('button', { name: 'Choose project' }),
    ).toBeVisible()
    await page.locator('.sidebar-new-session').click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main$/)
    await expect(page.getByRole('button', { name: 'Choose project' }).last()).toBeVisible()
  })

  test('project names only disclose tasks while the pencil opens a project draft', async ({ page }) => {
    await openControl(page)
    const project = page.locator('.sidebar-history-row--workspace').first()
    test.skip(await project.count() === 0, 'No persisted project on this gateway')

    const disclosure = project.getByTestId('project-workspace-disclosure')
    const info = project.getByTestId('project-workspace-info')
    const pencil = project.getByTestId('project-workspace-new-task')

    await expect(info).toBeVisible()
    await expect(disclosure).toHaveAttribute('aria-expanded', /true|false/)
    const startedExpanded = await disclosure.getAttribute('aria-expanded') === 'true'
    await disclosure.click()
    await expect(disclosure).toHaveAttribute('aria-expanded', String(!startedExpanded))
    await expect(page).not.toHaveURL(/\/chat\?session=/)

    await pencil.click()
    await expect(page).toHaveURL(/\/chat\/new\?agent=main&project=[^&]+$/)
    await expect(page.locator('.chat-project-chip')).toBeVisible()
  })
})
