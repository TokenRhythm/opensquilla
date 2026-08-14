import { expect, test } from '@playwright/test'

const CONTROL_URL = '/control/chat/new'

test('clipboard paste restores send readiness after stale IME composition', async ({ context, page }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto(CONTROL_URL)
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })

  const textarea = page.locator('.chat-textarea')
  const sendButton = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(textarea).toBeVisible()
  await textarea.focus()

  const pastedText = 'pasted from the Chromium clipboard'
  await page.evaluate(async text => navigator.clipboard.writeText(text), pastedText)

  // Leave Vue's element-level composing flag set, matching the Windows/IME
  // state from #1017, then use the browser's real clipboard shortcut. The DOM
  // receives the text even though vModelText ignores the input event.
  await textarea.dispatchEvent('compositionstart')
  await page.keyboard.press('ControlOrMeta+V')

  await expect(textarea).toHaveValue(pastedText)
  await expect(sendButton).toHaveClass(/is-ready/)
})
