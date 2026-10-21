import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Code block syntax highlighting', () => {
  test('idle chat renders no highlighted code blocks', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    await expect(page.locator('code.hljs')).toHaveCount(0)
    await expect(page.locator('.code-lang')).toHaveCount(0)
  })

  test('live python snippet renders distinct token colors and a language label', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    await page.locator('.chat-textarea').fill(
      'Reply with exactly one fenced python code block (```python) defining def greet(name): return f"hello {name}" — keep the def keyword and the string literal. No prose outside the block.',
    )
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const block = page.locator('.msg-ai .msg-ai-text pre code.hljs').first()
    await expect(block).toBeVisible({ timeout: 180000 })
    await expect(page.locator('.stream-activity')).toHaveCount(0, { timeout: 180000 })

    // Highlighter token spans survive sanitization.
    const keyword = block.locator('.hljs-keyword').first()
    const literal = block.locator('.hljs-string').first()
    await expect(keyword).toBeVisible()
    await expect(literal).toBeVisible()

    // Two token kinds resolve to different computed colors, and both differ
    // from the block's base text color — the --syntax-* tokens applied.
    const keywordColor = await keyword.evaluate(node => getComputedStyle(node).color)
    const stringColor = await literal.evaluate(node => getComputedStyle(node).color)
    const baseColor = await block.evaluate(node => getComputedStyle(node).color)
    expect(keywordColor).not.toBe(stringColor)
    expect(keywordColor).not.toBe(baseColor)
    expect(stringColor).not.toBe(baseColor)

    // The language label chip renders on the block.
    const label = page.locator('.msg-ai .msg-ai-text pre .code-lang').first()
    await expect(label).toBeVisible()
    await expect(label).toHaveText(/python/i)

    // Highlighting survives a reload: history replays through the same renderer.
    await page.reload()
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
    const replayed = page.locator('.msg-ai .msg-ai-text pre code.hljs .hljs-keyword').first()
    await expect(replayed).toBeVisible({ timeout: 30000 })
  })
})
