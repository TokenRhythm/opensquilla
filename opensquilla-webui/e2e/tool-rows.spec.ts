import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Tool rows and activity ribbon', () => {
  test('idle chat renders no live activity, elapsed labels, or result sheet', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.work-card')).toHaveCount(0)
    await expect(page.locator('.assistant-activity--live')).toHaveCount(0)
    await expect(page.locator('.tool-row__elapsed')).toHaveCount(0)
    await expect(page.locator('.tool-sheet')).toHaveCount(0)
  })

  test('live search run shows flat activity and checklist rows, then collapses', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Use your web search tool to find one recent headline about space exploration, then answer in one sentence.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const liveActivity = page.locator('.assistant-activity--live')
    await expect(liveActivity).toBeVisible({ timeout: 30000 })
    await expect(page.locator('.work-card')).toHaveCount(0)
    await expect(liveActivity.locator('.tool-row__bullet')).toHaveCount(0)

    // Observe the card head until the run completes. Fast runs finish every
    // phase in under a second — the elapsed chip never leaves 0s — so the
    // tick assertion only applies when a phase lasted long enough to tick.
    // Structure and lifecycle are asserted unconditionally.
    const observed = await page.evaluate(async () => {
      const t0 = Date.now()
      const samples: Array<{ phase: string | null; elapsed: string | null; checklistRows: number }> = []
      while (Date.now() - t0 < 180000) {
        const activity = document.querySelector('.assistant-activity--live')
        const phaseEl = document.querySelector('.assistant-activity__live-label')
        const elapsedEl = document.querySelector('.assistant-activity__live-elapsed')
        // Rows rendered inside the checklist variant of the timeline.
        const checklistRows = document.querySelectorAll('.assistant-activity--live .tool-timeline--checklist .tool-row').length
        samples.push({
          phase: phaseEl ? phaseEl.textContent : null,
          elapsed: elapsedEl ? elapsedEl.textContent : null,
          checklistRows,
        })
        if (activity === null && samples.length > 3) break
        await new Promise((resolve) => setTimeout(resolve, 150))
      }
      return samples
    })

    const phaseTexts = observed.map((s) => s.phase).filter((t): t is string => t !== null)
    expect(phaseTexts.length).toBeGreaterThan(0)
    // The checklist rows render inside the flat activity while it owns focus.
    expect(observed.some((s) => s.checklistRows > 0)).toBe(true)
    // Tick proof: ~2.4s of one continuous phase (16 samples at 150ms) must
    // show at least two distinct elapsed second values.
    const elapsedSeen = new Set<string>()
    let phaseLen = 0
    let prevPhase = ''
    let longestPhase = 0
    for (const s of observed) {
      if (s.phase === null || s.elapsed === null) continue
      elapsedSeen.add(s.elapsed)
      phaseLen = s.phase === prevPhase ? phaseLen + 1 : 1
      prevPhase = s.phase
      longestPhase = Math.max(longestPhase, phaseLen)
    }
    if (longestPhase >= 16) {
      expect(elapsedSeen.size).toBeGreaterThanOrEqual(2)
    }

    // Run completes: live activity settles to one summary row.
    await expect(liveActivity).toHaveCount(0, { timeout: 180000 })
    const activity = page.locator('.msg-ai .assistant-activity').first()
    await expect(activity).toBeVisible()
    const summary = activity.locator('.assistant-activity__summary')
    await expect(summary).toHaveAttribute('aria-expanded', 'false')
    await summary.press('Enter')
    await expect(summary).toHaveAttribute('aria-expanded', 'true')
    let searchRow = page.locator('.msg-ai .tool-row[data-op="web.search"]').first()
    await expect(searchRow).toBeVisible()

    // Search rows keep their semantic details collapsed after completion.
    await expect(searchRow).toHaveAttribute('aria-expanded', 'false')

    // Multiple search calls collapse under a group header; expand it and
    // assert against a member row, which follows the same pill contract.
    if (await searchRow.evaluate((el) => el.classList.contains('tool-row--group'))) {
      await searchRow.click()
      await expect(searchRow).toHaveAttribute('aria-expanded', 'true')
      searchRow = page.locator('.msg-ai .tool-row--member[data-op="web.search"]').first()
      await expect(searchRow).toBeVisible()
      await expect(searchRow).toHaveAttribute('aria-expanded', 'false')
    }

    // Replayed rows show no elapsed badges (no fake timings).
    await expect(page.locator('.tool-row__elapsed')).toHaveCount(0)

    // Expanding a row reveals compact activity details, not diagnostic cards.
    await searchRow.click()
    await expect(searchRow).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('.activity-tool-details').first()).toBeVisible()
    await expect(page.locator('.tool-row-section')).toHaveCount(0)
  })

  test('live failed tool call auto-expands its error row', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Fetch the exact URL http://127.0.0.1:9/missing with your web fetch tool and report what error you get. Do not try any other URL.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const errorRow = page.locator('.tool-row--error').first()
    await expect(errorRow).toBeVisible({ timeout: 180000 })
    await expect(errorRow).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('.activity-tool-details__line--error').first()).toBeVisible()
  })
})
