import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const SESSION_KEY = 'agent:main:webchat:e2edonecard'

// Seed a finished turn through the real WS pipeline: the page talks to the
// real gateway, but chat.history responses are rewritten in flight so a
// deliverable-bearing assistant turn renders without a live agent run.
async function seedHistoryWithDeliverable(page: Page) {
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
                role: 'user',
                text: 'Save a short report for me.',
                id: 'msg-e2e-done-user',
                timestamp: Math.floor(Date.now() / 1000) - 120,
              },
              {
                role: 'assistant',
                text: 'No deliverable on this turn.',
                id: 'msg-e2e-done-plain',
                timestamp: Math.floor(Date.now() / 1000) - 90,
                usage: { model: 'openai/gpt-test', input_tokens: 40, output_tokens: 12, cost_usd: 0.0004 },
              },
              {
                role: 'assistant',
                text: 'Saved the report.',
                id: 'msg-e2e-done-artifact',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                artifacts: [
                  { id: 'art-e2e-done-1', name: 'report.csv', mime: 'text/csv', size: 2048 },
                ],
                tool_calls: [
                  {
                    tool_use_id: 'tool-e2e-done-search',
                    name: 'web_search',
                    input: { query: 'renewable energy report' },
                    result: JSON.stringify({
                      results: [
                        { title: 'Renewable energy outlook', url: 'https://example.com/outlook' },
                      ],
                    }),
                  },
                ],
                usage: { model: 'openai/gpt-test', input_tokens: 120, output_tokens: 60, cost_usd: 0.0012 },
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

async function openSeededSession(page: Page) {
  await seedHistoryWithDeliverable(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
}

test.describe('Deliverable endings', () => {
  test('artifact turn keeps the message receipt outside the deliverable surface', async ({ page }) => {
    await openSeededSession(page)

    // Only the artifact-bearing turn gets the deliverable group.
    const block = page.getByTestId('done-block')
    await expect(block).toHaveCount(1)
    await expect(block).toBeVisible({ timeout: 10000 })
    // The retired follow-up row never renders; the composer continues the turn.
    await expect(page.locator('.done-card')).toHaveCount(0)

    // The deliverable group holds only the artifact and its sources.
    await expect(block.locator('.msg-artifact-chip')).toBeVisible()
    await expect(block.locator('.msg-artifact-name')).toHaveText('report.csv')
    await expect(block.locator('.sources-row')).toBeVisible()
    await expect(block.locator('.msg-ai-footer')).toHaveCount(0)

    const messageMain = block.locator('..')
    const receipt = messageMain.locator(':scope > .msg-ai-footer')
    await expect(receipt.locator('.msg-ai-meta .msg-meta__cost')).toContainText('$')

    // The old outer card is gone: the artifact chip owns the only visible frame.
    const surface = await block.evaluate(el => {
      const style = getComputedStyle(el)
      return {
        background: style.backgroundColor,
        borderTop: style.borderTopWidth,
        paddingTop: style.paddingTop,
      }
    })
    expect(surface).toEqual({
      background: 'rgba(0, 0, 0, 0)',
      borderTop: '0px',
      paddingTop: '0px',
    })
    await expect(block.locator('.msg-artifact-chip')).toHaveCSS('border-top-style', 'solid')

    // Group order is artifacts → sources; the receipt follows as a sibling.
    const groupOrder = await block.evaluate(el => {
      const children = Array.from(el.children)
      const indexOf = (selector: string) => children.findIndex(child => child.matches(selector))
      return {
        artifacts: indexOf('.msg-artifacts'),
        sources: indexOf('.sources-row'),
      }
    })
    expect(groupOrder.artifacts).toBeGreaterThanOrEqual(0)
    expect(groupOrder.sources).toBeGreaterThan(groupOrder.artifacts)

    const receiptFollowsBlock = await page.locator('.msg-ai-main').last().evaluate(el => {
      const done = el.querySelector('[data-testid="done-block"]')
      return !!done && done.nextElementSibling?.matches('.msg-ai-footer')
    })
    expect(receiptFollowsBlock).toBe(true)
  })

  test('completed turns render no follow-up actions; the composer is the continue affordance', async ({ page }) => {
    await openSeededSession(page)

    await expect(page.getByTestId('done-block')).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('done-continue')).toHaveCount(0)
    await expect(page.getByTestId('done-new-task')).toHaveCount(0)
    await expect(page.locator('.done-card')).toHaveCount(0)

    // Continuing the conversation goes straight through the composer.
    await expect(page.locator('.chat-textarea')).toBeVisible()
    await page.locator('.chat-textarea').click()
    await expect(page.locator('.chat-textarea')).toBeFocused()
  })

  test('live file-producing run ends with the deliverable block', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // Asking for a download nudges the agent to publish the file as an
    // artifact instead of stopping at a plain workspace write.
    await page.locator('.chat-textarea').fill('把这句话保存成一个文本文件，并提供下载。')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const block = page.getByTestId('done-block')
    await expect(block).toBeVisible({ timeout: 180000 })
    await expect(block.locator('.msg-artifact-chip').first()).toBeVisible()
    await expect(block.locator('.msg-ai-footer')).toHaveCount(0)
    await expect(block.locator('..').locator(':scope > .msg-ai-footer')).toBeVisible()
    // No follow-up action row after completion.
    await expect(page.getByTestId('done-continue')).toHaveCount(0)
    await expect(page.getByTestId('done-new-task')).toHaveCount(0)
    await expect(page.locator('.done-card')).toHaveCount(0)
  })
})
