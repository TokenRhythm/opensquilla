import { expect, test, type Page } from '@playwright/test'
import { chatHistoryPayload } from './support/session-read-fixtures'
import { installSidebarFixture } from './support/sidebar-fixture'

const SESSION_KEY = 'agent:main:webchat:e2emessagecopy'
const COPY_SHORTCUT = process.platform === 'darwin' ? 'Meta+C' : 'Control+C'
const PASTE_SHORTCUT = process.platform === 'darwin' ? 'Meta+V' : 'Control+V'

interface ClipboardPaste {
  text: string
  html: string
}

async function openUserMessages(page: Page, texts: string[]) {
  await installSidebarFixture(page, {
    'chat.history': chatHistoryPayload(texts.map((text, index) => ({
      role: 'user', id: `synthetic-copy-message-${index}`, text, timestamp: 1_800_000_000 + index,
    }))),
  })
  if (process.env.OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI === 'preview') {
    await page.route('**/control/chat?*', async route => {
      if (!route.request().isNavigationRequest()) return route.fallback()
      const response = await route.fetch()
      const body = (await response.text()).replace(/(src|href)="\.\//g, '$1="/control/')
      await route.fulfill({ response, body })
    })
  }
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)
  const messages = page.locator('.msg-user')
  await expect(messages.locator('.msg-user-bubble')).toHaveText(texts)
  await page.evaluate(() => navigator.clipboard.writeText('Synthetic clipboard sentinel.'))
  return messages
}

// Inspect native paste after this test has copied synthetic content. Keeping
// both clipboard formats catches icons that are absent from plain text.
async function pasteSyntheticClipboard(page: Page): Promise<ClipboardPaste> {
  await page.evaluate(() => {
    const target = document.createElement('div')
    target.contentEditable = 'true'
    target.dataset.testid = 'message-copy-paste-probe'
    target.style.cssText = 'position:fixed;inset:0 auto auto 0;width:80px;height:40px;z-index:2147483647'
    target.addEventListener('paste', event => {
      event.preventDefault()
      event.stopImmediatePropagation()
      target.dataset.paste = JSON.stringify({
        text: event.clipboardData?.getData('text/plain') ?? '',
        html: event.clipboardData?.getData('text/html') ?? '',
      })
    })
    document.body.append(target)
    target.focus()
  })
  await page.keyboard.press(PASTE_SHORTCUT)
  const probe = page.getByTestId('message-copy-paste-probe')
  await expect(probe).toHaveAttribute('data-paste')
  const result = JSON.parse((await probe.getAttribute('data-paste'))!) as ClipboardPaste
  await probe.evaluate(element => element.remove())
  // Windows exposes native line endings as CRLF.
  return { text: result.text.replace(/\r\n/g, '\n'), html: result.html }
}

async function expectNoCopiedControls(page: Page, html: string) {
  const controls = await page.evaluate(html => {
    const document = new DOMParser().parseFromString(html, 'text/html')
    return document.querySelectorAll('svg, button, .msg-user-actions').length
  }, html)
  expect(controls).toBe(0)
}

test('triple-click copy retains the paragraph without hidden toolbar icons', async ({ page }) => {
  const text = 'Synthetic paragraph for selection copy.'
  const message = await openUserMessages(page, [text])
  await message.locator('.msg-user-bubble').click({ clickCount: 3 })
  await page.keyboard.press(COPY_SHORTCUT)
  const pasted = await pasteSyntheticClipboard(page)
  expect(pasted.text.trimEnd()).toBe(text)
  expect(pasted.html).toContain(text)
  await expectNoCopiedControls(page, pasted.html)
})

test('dragging across messages retains authored line breaks without toolbar content', async ({ page }) => {
  const texts = ['Synthetic first line.\nSynthetic continuation.', 'Synthetic next message.']
  const messages = await openUserMessages(page, texts)
  const endpoints = await messages.locator('.msg-user-bubble').evaluateAll(elements => elements.map(element => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const rects = Array.from(range.getClientRects())
    const first = rects[0]!
    const last = rects[rects.length - 1]!
    return { start: { x: first.left, y: first.top + first.height / 2 },
      end: { x: last.right + 1, y: last.top + last.height / 2 } }
  }))
  await page.mouse.move(endpoints[0]!.start.x, endpoints[0]!.start.y)
  await page.mouse.down()
  await page.mouse.move(endpoints[1]!.end.x, endpoints[1]!.end.y, { steps: 12 })
  await page.mouse.up()
  await page.keyboard.press(COPY_SHORTCUT)
  const pasted = await pasteSyntheticClipboard(page)
  expect(pasted.text).toContain(texts[0])
  expect(pasted.text).toContain(texts[1])
  expect(pasted.text.replace(/\s+/g, ' ').trim()).toBe(texts.join(' ').replace(/\s+/g, ' '))
  await expectNoCopiedControls(page, pasted.html)
})

test('copy button preserves multiline message text', async ({ page }) => {
  const text = 'Synthetic first line.\nSynthetic second line.\n\nSynthetic final paragraph.'
  const message = await openUserMessages(page, [text])
  await message.hover()
  await message.locator('.msg-user-actions button').first().click()
  await expect(message.locator('.msg-action--ok')).toHaveCount(1)
  const pasted = await pasteSyntheticClipboard(page)
  expect(pasted.text).toBe(text)
  expect(pasted.html).toBe('')
})
