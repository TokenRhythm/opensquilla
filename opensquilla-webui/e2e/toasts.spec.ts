import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2etoasts'

// Seed an assistant message carrying an artifact through the real WS pipeline:
// the page talks to the real gateway, but chat.history responses are rewritten
// in flight so the artifact chip renders without needing a live agent run.
async function seedHistoryWithArtifact(page: Page) {
  await page.routeWebSocket(/\/ws$/, ws => {
    const server = ws.connectToServer()
    const historyIds = new Set<string>()
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'req' && frame.method === 'chat.history') {
          historyIds.add(String(frame.id))
        }
      } catch {}
      server.send(message)
    })
    server.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type === 'res' && frame.id !== undefined && historyIds.has(String(frame.id))) {
          historyIds.delete(String(frame.id))
          frame.ok = true
          delete frame.error
          frame.payload = {
            messages: [
              {
                role: 'assistant',
                text: 'Saved the export.',
                id: 'msg-e2e-artifact',
                timestamp: Math.floor(Date.now() / 1000),
                artifacts: [
                  { id: 'art-e2e-1', name: 'report.csv', mime: 'text/csv', size: 2048 },
                ],
              },
            ],
            has_more: false,
          }
          ws.send(JSON.stringify(frame))
          return
        }
      } catch {}
      ws.send(message)
    })
  })
}

async function openDraftChat(page: Page) {
  await page.goto(CONTROL_URL + 'chat/new')
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
}

test.describe('Failure toasts and composer copy', () => {
  test('draft landing composer placeholder is English', async ({ page }) => {
    await openDraftChat(page)

    const textarea = page.locator('.chat-textarea')
    await expect(textarea).toHaveAttribute('placeholder', 'Assign a task or ask anything')
    const placeholder = await textarea.getAttribute('placeholder')
    expect(placeholder).not.toMatch(/[一-鿿]/)
  })

  test('unsupported voice input raises a danger toast that auto-dismisses', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'MediaRecorder', { value: undefined, configurable: true })
    })
    await openDraftChat(page)

    await page.locator('button[aria-label="Record voice input"]').click()

    const toast = page.getByTestId('toast')
    await expect(toast).toHaveCount(1)
    await expect(toast).toContainText('Voice input is not supported in this browser')
    await expect(toast).toHaveClass(/toast--danger/)

    // The host is a polite live region and does not steal focus.
    await expect(page.getByTestId('toast-host')).toHaveAttribute('aria-live', 'polite')

    // Auto-dismisses without user action.
    await expect(toast).toHaveCount(0, { timeout: 8000 })
  })

  test('repeated failures stack as separate toasts and dismiss on demand', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'MediaRecorder', { value: undefined, configurable: true })
    })
    await openDraftChat(page)

    const voiceButton = page.locator('button[aria-label="Record voice input"]')
    await voiceButton.click()
    await voiceButton.click()

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
    await expect(chip.locator('.msg-artifact-action')).toHaveText('Download')

    await chip.click()

    const toast = page.getByTestId('toast')
    await expect(toast).toBeVisible()
    await expect(toast).toContainText('Download failed — HTTP 500')
    await expect(toast).toHaveClass(/toast--danger/)
  })
})
