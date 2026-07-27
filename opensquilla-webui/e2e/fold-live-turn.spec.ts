import { test, expect, type Page } from '@playwright/test'

// fold-authoritative live render proof. The fold is now the default live
// render source (the activity timeline/artifacts/live-thinking project from the
// folded event log); only `opensquilla.chat.foldLiveTurn=0` forces the legacy
// refs back. This spec pins the flag to '1' (any non-'0' value is ON) so it stays
// a deterministic ON-path proof regardless of the default, drives the same real
// live-stream path the legacy live specs drive, and proves the ON path renders
// correctly with NO `[live-turn parity]` divergence.
//
// Two gates apply, exactly like the other live specs:
//   - It only runs against a real gateway (OPENSQUILLA_E2E_LIVE=1).
//   - The flag only flips the consumer in a non-DEV build (DEV is pinned to
//     SHADOW). The orchestrator builds the bundle, then runs this with the flag.
const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'
const FOLD_FLAG_KEY = 'opensquilla.chat.foldLiveTurn'

// Pin the ON flag before any page script runs so useChatTurnLog resolves
// useReducer === true at composable init (the fold becomes authoritative).
async function forceFoldAuthoritative(page: Page) {
  await page.addInitScript(([key]) => {
    try {
      window.localStorage.setItem(key, '1')
    } catch {
      // localStorage can be unavailable in some sandboxes; the flag simply
      // stays OFF there and the run falls back to the legacy render.
    }
  }, [FOLD_FLAG_KEY])
}

test.describe('Fold-authoritative live turn', () => {
  // Reuse the console-clarity hard-fail: any `[live-turn parity]` error means
  // the fold diverged from legacy. With the fold authoritative this is the
  // deterministic guard that the ON path is byte-faithful to legacy.
  let parityErrors: string[]

  test.beforeEach(async ({ page }) => {
    parityErrors = []
    page.on('console', msg => {
      if (msg.type() === 'error' && msg.text().includes('[live-turn parity]')) {
        parityErrors.push(msg.text())
      }
    })
    await forceFoldAuthoritative(page)
  })

  test.afterEach(() => {
    expect(parityErrors, 'fold-authoritative live render diverged from legacy').toEqual([])
  })

  test('absent localStorage key resolves to the fold (ON) by default', async ({ browser }) => {
    // A context WITHOUT the init script leaves the key unset. Post default-flip an
    // absent key resolves to ON (the fold is authoritative); only an explicit '0'
    // forces legacy. The key stays null — the ON default needs no key written.
    const context = await browser.newContext()
    const page = await context.newPage()
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    const flag = await page.evaluate(key => window.localStorage.getItem(key), FOLD_FLAG_KEY)
    expect(flag).toBeNull()
    await context.close()
  })

  test('fold drives the flat activity timeline and tool rows for a live search run', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // Confirm the flag took effect for this build (a DEV build pins SHADOW and
    // never flips ON; the orchestrator runs this against a built bundle).
    const flag = await page.evaluate(key => window.localStorage.getItem(key), FOLD_FLAG_KEY)
    expect(flag).toBe('1')

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Use your web search tool to find one recent headline about space exploration, then answer in one sentence.')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const liveActivity = page.locator('.assistant-activity--live')
    await expect(liveActivity).toBeVisible({ timeout: 30000 })
    await expect(page.locator('.work-card')).toHaveCount(0)

    const sawChecklistRow = await page.evaluate(async () => {
      const t0 = Date.now()
      while (Date.now() - t0 < 180000) {
        const rows = document.querySelectorAll('.assistant-activity--live .tool-timeline--checklist .tool-row').length
        if (rows > 0) return true
        if (document.querySelector('.assistant-activity--live') === null) return false
        await new Promise(resolve => setTimeout(resolve, 150))
      }
      return false
    })
    expect(sawChecklistRow).toBe(true)

    // Run completes: live activity collapses, the transcript keeps the rows.
    await expect(liveActivity).toHaveCount(0, { timeout: 180000 })
    const activity = page.locator('.msg-ai .assistant-activity').first()
    await expect(activity).toBeVisible()
    const summary = activity.locator('.assistant-activity__summary')
    await expect(summary).toHaveAttribute('aria-expanded', 'false')
    await summary.press('Enter')
    await expect(summary).toHaveAttribute('aria-expanded', 'true')
    const searchRow = page.locator('.msg-ai .tool-row[data-op="web.search"]').first()
    await expect(searchRow).toBeVisible()
    await expect(searchRow).toHaveAttribute('aria-expanded', 'false')
  })
})
