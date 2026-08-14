import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const RESEARCH_SESSION_KEY = 'agent:main:webchat:e2eresearchsources'

async function openFirstAssistantActivity(page: Page) {
  const activity = page.locator('.msg-ai .assistant-activity').first()
  await expect(activity).toBeVisible({ timeout: 10000 })
  await expect(activity).not.toHaveAttribute('open', '')
  await activity.locator('summary').press('Enter')
  await expect(activity).toHaveAttribute('open', '')
  return activity
}

async function seedSearchHistory(page: Page, toolName: string) {
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame?.type !== 'req' || frame.id === undefined) return
      if (frame.method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: {} }))
        return
      }
      if (frame?.type === 'req' && frame.method === 'chat.history') {
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: {
            messages: [
              {
                role: 'assistant',
                text: 'The answer cites the research result.',
                id: 'msg-e2e-research-source',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                tool_calls: [
                  {
                    tool_use_id: 'tool-e2e-research-source',
                    name: toolName,
                    input: { query: 'OpenSquilla web search' },
                    result: JSON.stringify({
                      ok: true,
                      results: [
                        {
                          title: 'OpenSquilla Search Notes',
                          url: 'https://example.com/opensquilla-search',
                          domain: 'example.com',
                          provider: 'tavily',
                          excerpt: 'Compact citation-ready excerpt.',
                          fetched: true,
                        },
                      ],
                    }),
                    status: 'success',
                  },
                ],
              },
            ],
            has_more: false,
          },
        }))
        return
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: {} }))
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
}

async function seedPersistedSearchSourcesHistory(page: Page) {
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.onMessage(message => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame?.type !== 'req' || frame.id === undefined) return
      if (frame.method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: {} }))
        return
      }
      if (frame?.type === 'req' && frame.method === 'chat.history') {
        ws.send(JSON.stringify({
          type: 'res',
          id: frame.id,
          ok: true,
          payload: {
            messages: [
              {
                role: 'assistant',
                text: 'The answer cites the persisted source.',
                id: 'msg-e2e-persisted-source',
                timestamp: Math.floor(Date.now() / 1000) - 60,
                tool_calls: [
                  {
                    tool_use_id: 'tool-e2e-persisted-source',
                    name: 'web_search',
                    input: { query: 'OpenSquilla persisted source' },
                    sources: [
                      {
                        title: 'OpenSquilla Persisted Source',
                        url: 'https://example.com/opensquilla-persisted-source',
                        domain: 'example.com',
                        provider: 'duckduckgo',
                        fetched: true,
                      },
                    ],
                    result: JSON.stringify({
                      ok: true,
                      results: [
                        {
                          title: 'Truncated fallback source',
                          url: 'https://example.com/opensquilla-persisted-sour…',
                          domain: 'example.com',
                          provider: 'duckduckgo',
                          excerpt: 'Compacted persisted result with a dead-link URL.',
                          fetched: true,
                        },
                      ],
                    }),
                    status: 'success',
                  },
                ],
              },
            ],
            has_more: false,
          },
        }))
        return
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: {} }))
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
}

test.describe('Sources row and thinking disclosure', () => {
  test('idle chat renders no sources row or thinking disclosure', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.sources-row')).toHaveCount(0)
    await expect(page.locator('.thinking-fold')).toHaveCount(0)
  })

  test('replayed web_search results render a sources row with links', async ({ page }) => {
    await seedSearchHistory(page, 'web_search')
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(RESEARCH_SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await openFirstAssistantActivity(page)
    await expect(page.locator('.msg-ai .tool-row[data-op="web.search"]').first()).toBeVisible({ timeout: 10000 })

    const sourcesRow = page.locator('.msg-ai .sources-row').first()
    await expect(sourcesRow).toBeVisible({ timeout: 10000 })
    await expect(sourcesRow.locator('.sources-row__count')).toHaveText('1')

    await sourcesRow.locator('.sources-row__toggle').click()
    const link = sourcesRow.locator('.sources-row__link')
    await expect(link).toHaveAttribute('href', 'https://example.com/opensquilla-search')
    await expect(sourcesRow.locator('.sources-row__title')).toHaveText('OpenSquilla Search Notes')
  })

  test('replayed web_search prefers persisted sources over truncated result URLs', async ({ page }) => {
    await seedPersistedSearchSourcesHistory(page)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(RESEARCH_SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await openFirstAssistantActivity(page)
    await expect(page.locator('.msg-ai .tool-row[data-op="web.search"]').first()).toBeVisible({ timeout: 10000 })

    const sourcesRow = page.locator('.msg-ai .sources-row').first()
    await expect(sourcesRow).toBeVisible({ timeout: 10000 })
    await expect(sourcesRow.locator('.sources-row__count')).toHaveText('1')

    await sourcesRow.locator('.sources-row__toggle').click()
    const link = sourcesRow.locator('.sources-row__link')
    await expect(link).toHaveAttribute('href', 'https://example.com/opensquilla-persisted-source')
    await expect(sourcesRow.locator('.sources-row__title')).toHaveText('OpenSquilla Persisted Source')
  })

  test('replayed web_discover results render as discovery without sources row', async ({ page }) => {
    await seedSearchHistory(page, 'web_discover')
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(RESEARCH_SESSION_KEY))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await openFirstAssistantActivity(page)
    await expect(page.locator('.msg-ai .tool-row[data-op="web.discover"]').first()).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.msg-ai .sources-row')).toHaveCount(0)
  })

  test('live search turn renders a sources row with real links', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Use your web search tool to find one recent headline about renewable energy, then answer in one sentence.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    // The turn runs and completes.
    const ribbon = page.locator('.stream-activity')
    await expect(ribbon).toBeVisible({ timeout: 30000 })
    await expect(ribbon).toHaveCount(0, { timeout: 180000 })

    // Sources row appears on the finished assistant turn, collapsed.
    const sourcesRow = page.locator('.msg-ai .sources-row').first()
    await expect(sourcesRow).toBeVisible({ timeout: 30000 })
    const toggle = sourcesRow.locator('.sources-row__toggle')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(toggle.locator('.sources-row__count')).toHaveText(/^[1-9]\d*$/)

    // Expanding reveals real external links.
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const links = sourcesRow.locator('.sources-row__link')
    expect(await links.count()).toBeGreaterThan(0)
    for (const href of await links.evaluateAll(nodes => nodes.map(node => node.getAttribute('href') || ''))) {
      expect(href).toMatch(/^https?:\/\//)
      // Compacted tool results truncate long strings with a '…' suffix; such
      // URLs are dead links and must never be rendered as hrefs.
      expect(href).not.toMatch(/…|%E2%80%A6/i)
    }
    const resolvedHrefs = await links.evaluateAll(nodes =>
      nodes.map(node => (node as HTMLAnchorElement).href),
    )
    for (const href of resolvedHrefs) {
      expect(href).not.toMatch(/…|%E2%80%A6/i)
    }
    const rels = await links.evaluateAll(nodes => nodes.map(node => node.getAttribute('rel') || ''))
    for (const rel of rels) {
      expect(rel).toContain('noopener')
      expect(rel).toContain('noreferrer')
    }

    // Completed execution activity is compact by default. Reasoning, when
    // present, lives inside this one disclosure instead of a nested accordion.
    const activities = page.locator('.msg-ai .assistant-activity')
    if (await activities.count() > 0) {
      for (const isOpen of await activities.evaluateAll(nodes => nodes.map(node => node.hasAttribute('open')))) {
        expect(isOpen).toBe(false)
      }
    }

    // The row survives a reload: tool results replay through chat.history.
    await page.reload()
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    const replayedRow = page.locator('.msg-ai .sources-row').first()
    await expect(replayedRow).toBeVisible({ timeout: 30000 })
    await expect(replayedRow.locator('.sources-row__toggle')).toHaveAttribute('aria-expanded', 'false')
  })

  test('measured reasoning time survives the post-turn history sync', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    // Force the incremental merge ON regardless of the rollout default, before
    // any page script reads the flag. The merge keeps a finished turn's measured
    // reasoning.seconds when the next history snapshot lands; without it the
    // snapshot maps seconds back to 0 and the disclosure loses its elapsed.
    await page.addInitScript(() => {
      try { window.localStorage.setItem('opensquilla.chat.historyMerge', '1') } catch { /* storage blocked */ }
    })

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // A multi-step reasoning prompt so the measured think time is comfortably
    // over a second; a trivial one-word answer can resolve in under 1s, which
    // renders as "Thought process" (seconds < 1) and cannot exercise the
    // seconds-preservation contract this test guards.
    const textarea = page.locator('.chat-textarea')
    await textarea.fill(
      'Reason thoroughly and at length, step by step, before answering. '
      + 'Logic puzzle: Alice, Bob, and Carol each own a different pet '
      + '(cat, dog, fish) and like a different color (red, green, blue). '
      + 'Clue 1: Alice does not own the cat. Clue 2: the dog owner likes blue. '
      + 'Clue 3: Carol likes red. Clue 4: Bob does not like green. Work through '
      + 'every deduction explicitly, then give the full solution in one sentence.',
    )
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    // The turn runs and completes.
    const ribbon = page.locator('.stream-activity')
    await expect(ribbon).toBeVisible({ timeout: 30000 })
    await expect(ribbon).toHaveCount(0, { timeout: 180000 })

    // The finished assistant turn's embedded reasoning label shows the measured
    // elapsed. ReasoningPart renders "Thought for Ns" when
    // seconds >= 1 and degrades to "Thought process" when seconds is 0 — the
    // latter is what a snapshot that drops the measured seconds would produce.
    const activity = page.locator('.msg-ai .assistant-activity').first()
    await expect(activity).toBeVisible({ timeout: 30000 })
    await activity.locator('summary').press('Enter')
    const reasoningLabel = activity.locator('.thinking-block__header').first()
    await expect(reasoningLabel).toBeVisible()
    await expect(reasoningLabel).toHaveText(/Thought for \d+/)
    await expect(reasoningLabel).not.toHaveText(/Thought process/)

    // Wait past the ~50ms history-sync debounce plus a generous margin, then
    // re-assert: a sync that clobbered the row would have reset the summary to
    // "Thought process" here; the merge keeps the measured seconds.
    await page.waitForTimeout(500)
    await expect(reasoningLabel).toHaveText(/Thought for \d+/)
    await expect(reasoningLabel).not.toHaveText(/Thought process/)
  })
})
