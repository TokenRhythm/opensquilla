import { expect, test } from '@playwright/test'
import { expectComposerValue } from './utils'

const CONTROL_URL = '/control/chat/new'

test('clipboard paste restores send readiness after stale IME composition', async ({ context, page }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.goto(CONTROL_URL)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 10_000 })

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

  await expectComposerValue(textarea, pastedText)
  await expect(sendButton).toHaveClass(/is-ready/)
})

test('long plain-text clipboard paste becomes a pasted-text attachment', async ({ context, page }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.goto(CONTROL_URL)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 10_000 })

  const textarea = page.locator('.chat-textarea')
  await textarea.focus()
  const pastedText = `${'line with pasted content\n'.repeat(900)}tail`
  await page.evaluate(async text => navigator.clipboard.writeText(text), pastedText)
  await page.keyboard.press('ControlOrMeta+V')

  const chip = page.locator('.attachment-chip')
  await expect(chip).toHaveCount(1)
  await expect(chip).toContainText('Pasted text')
  await expectComposerValue(textarea, '')
})

test('post-insertion paste fallback attaches text before send', async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.goto(CONTROL_URL)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible({ timeout: 10_000 })

  const textarea = page.locator('.chat-textarea')
  const instruction = 'Summarize the pasted text: '
  const pastedText = `${'fallback paste content\n'.repeat(900)}tail`
  // Fill the contenteditable first so its logical model and caret reflect the
  // browser's post-insertion state, then deliver the paste-shaped input event
  // that the long-paste fallback consumes.
  await textarea.fill(`${instruction}${pastedText}`)
  await textarea.evaluate((element, value) => {
    element.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      inputType: 'insertFromPaste',
      data: value,
    }))
  }, pastedText)

  const chip = page.locator('.attachment-chip')
  await expect(chip).toHaveCount(1)
  await expect(chip).toContainText('Pasted text')
  await expectComposerValue(textarea, instruction)
})
