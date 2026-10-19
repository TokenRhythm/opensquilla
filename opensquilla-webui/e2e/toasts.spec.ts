import { test, expect, type Page } from '@playwright/test'
import { chatHistoryPayload } from './support/session-read-fixtures'
import { installSidebarFixture } from './support/sidebar-fixture'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2etoasts'

// Use a complete synthetic conversation; this key is not owned by a real Gateway.
async function seedHistoryWithArtifact(page: Page) {
  await installSidebarFixture(page, {
    'chat.history': chatHistoryPayload([
      {
        role: 'assistant',
        text: 'Saved the export.',
        id: 'msg-e2e-artifact',
        timestamp: Math.floor(Date.now() / 1000),
        artifacts: [
          { id: 'art-e2e-1', name: 'report.csv', mime: 'text/csv', size: 2048 },
        ],
      },
    ]),
  })
}

async function openDraftChat(page: Page) {
  await installSidebarFixture(page, { 'onboarding.status': { audioConfigured: true } })
  await page.goto(CONTROL_URL + 'chat/new')
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
}

test.describe('Failure toasts and composer copy', () => {
  test('draft landing composer placeholder is English', async ({ page }) => {
    await openDraftChat(page)

    const textarea = page.locator('.chat-textarea')
    await expect(textarea).toHaveAttribute('data-placeholder', 'Assign a task or ask anything')
    const placeholder = await textarea.getAttribute('data-placeholder')
    expect(placeholder).not.toMatch(/[一-鿿]/)
  })

  for (const mobile of [false, true]) test(`unsupported voice input stays at the bottom and auto-dismisses (${mobile ? 'mobile' : 'desktop'})`, async ({ page }) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 })
    await page.addInitScript(() => {
      Object.defineProperty(window, 'MediaRecorder', { value: undefined, configurable: true })
    })
    await openDraftChat(page)

    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await page.getByRole('menuitem', { name: 'Record voice input', exact: true }).click()

    const toast = page.getByTestId('toast')
    await expect(toast).toHaveCount(1)
    await expect(toast).toContainText('Voice input is not supported in this browser')
    await expect(toast).toHaveClass(/toast--danger/)

    // The host is a polite live region and does not steal focus.
    await expect(page.getByTestId('toast-host')).toHaveAttribute('aria-live', 'polite')
    const position = await page.getByTestId('toast-host').evaluate(host => {
      const rect = host.getBoundingClientRect()
      const tabbar = document.querySelector('.mobile-tabbar')?.getBoundingClientRect()
      return { rightGap: innerWidth - rect.right, bottomGap: innerHeight - rect.bottom,
        top: rect.top, bottom: rect.bottom, tabbarTop: tabbar?.top ?? innerHeight }
    })
    expect(position.top).toBeGreaterThan(400)
    expect(position.rightGap).toBeGreaterThan(0)
    expect(position.rightGap).toBeLessThan(30)
    if (mobile) {
      expect(position.bottom).toBeLessThan(position.tabbarTop)
    } else {
      expect(position.bottomGap).toBeGreaterThan(0)
      expect(position.bottomGap).toBeLessThan(30)
    }

    // Auto-dismisses without user action.
    await expect(toast).toHaveCount(0, { timeout: 8000 })
  })

  test('repeated failures stack as separate toasts and dismiss on demand', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'MediaRecorder', { value: undefined, configurable: true })
    })
    await openDraftChat(page)

    for (let attempt = 0; attempt < 2; attempt += 1) {
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      await page.getByRole('menuitem', { name: 'Record voice input', exact: true }).click()
    }

    const toasts = page.getByTestId('toast')
    await expect(toasts).toHaveCount(2)

    await toasts.first().getByRole('button', { name: 'Dismiss notification' }).click()
    await expect(toasts).toHaveCount(1)
  })

  test('artifact download failure raises a toast when the API returns 500', async ({ page }) => {
    await seedHistoryWithArtifact(page)
    await page.route('**/api/v1/artifacts/**', route =>
      route.fulfill({ status: 500, body: 'internal error' }))

    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    // The seeded history renders a real artifact chip with the English action label.
    const chip = page.locator('.msg-artifact-chip').first()
    await expect(chip).toBeVisible({ timeout: 10000 })
    await chip.getByRole('button', { name: 'Download report.csv', exact: true }).click()

    const toast = page.getByTestId('toast')
    await expect(toast).toBeVisible()
    await expect(toast).toContainText('Download failed — HTTP 500')
    await expect(toast).toHaveClass(/toast--danger/)
  })
})
