import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Compaction maintenance card', () => {
  test('idle chat renders no maintenance card', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.chat-compact-status')).toHaveCount(0)
  })

  test('live /compact shows the card with elapsed ticking, then settles and dismisses', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(300000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    // Seed the session with one short exchange so compaction has messages.
    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Reply with the single word: ok')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect(page.locator('.msg-ai').first()).toBeVisible({ timeout: 120000 })
    await expect(page.locator('.work-card')).toHaveCount(0, { timeout: 120000 })

    // Trigger manual compaction through the slash command path.
    await textarea.fill('/compact')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    const card = page.locator('.chat-compact-status')
    await expect(card).toBeVisible({ timeout: 10000 })

    // Observe the card until it settles. Fast compactions finish in under a
    // second — the elapsed chip never leaves 0s and the busy state may never
    // be sampled — so tick/busy assertions are conditional on duration while
    // lifecycle assertions stay unconditional.
    const observed = await page.evaluate(async () => {
      const t0 = Date.now()
      const samples: Array<{
        title: string | null
        elapsed: string | null
        busyIndicator: boolean
        gaugeIndeterminate: boolean
        gaugeDone: boolean
        gaugeAriaValueNow: string | null
        gaugeFillRatio: number | null
      }> = []
      while (Date.now() - t0 < 120000) {
        const cardEl = document.querySelector('.chat-compact-status')
        const titleEl = document.querySelector('.chat-compact-status__title')
        const elapsedEl = document.querySelector('.chat-compact-status__elapsed')
        const gaugeEl = document.querySelector<HTMLElement>('.chat-compact-status__gauge')
        const gaugeFillEl = document.querySelector<HTMLElement>('.chat-compact-status__gauge-fill')
        const gaugeWidth = gaugeEl?.getBoundingClientRect().width ?? 0
        samples.push({
          title: titleEl ? titleEl.textContent : null,
          elapsed: elapsedEl ? elapsedEl.textContent : null,
          busyIndicator: !!document.querySelector(
            '.chat-compact-status__indicator--busy',
          ),
          gaugeIndeterminate: !!document.querySelector(
            '.chat-compact-status__gauge-fill--indeterminate',
          ),
          gaugeDone: !!document.querySelector('.chat-compact-status__gauge-fill--done'),
          gaugeAriaValueNow: gaugeEl?.getAttribute('aria-valuenow') ?? null,
          gaugeFillRatio: gaugeFillEl && gaugeWidth > 0
            ? gaugeFillEl.getBoundingClientRect().width / gaugeWidth
            : null,
        })
        const settled = cardEl !== null
          && !document.querySelector('.chat-compact-status__indicator--busy')
        if ((settled || cardEl === null) && samples.length > 2) break
        await new Promise((resolve) => setTimeout(resolve, 150))
      }
      return samples
    })

    // Lifecycle: the card rendered a title and reached a terminal state.
    const titles = observed.map((s) => s.title).filter((t): t is string => t !== null)
    expect(titles.length).toBeGreaterThan(0)
    expect(
      titles.some(
        (t) => /^(Compacting context|Context compacted|Already within context budget|Context within budget\.?)/.test(t),
      ),
    ).toBe(true)
    const last = observed[observed.length - 1]
    expect(last.busyIndicator).toBe(false)
    expect(
      last.title === null
      || /^(Context compacted|Already within context budget|Context within budget\.?)/.test(last.title),
    ).toBe(true)

    const busySamples = observed.filter((s) => s.busyIndicator)
    if (busySamples.length > 0) {
      // While busy the operation exposes no fake percentage: a short
      // indeterminate segment moves across the track and omits aria-valuenow.
      expect(busySamples.some((s) => s.gaugeIndeterminate)).toBe(true)
      expect(busySamples.every((s) => s.gaugeAriaValueNow === null)).toBe(true)
      expect(
        busySamples.some(
          (s) => s.gaugeFillRatio !== null
            && s.gaugeFillRatio >= 0.38
            && s.gaugeFillRatio <= 0.46,
        ),
      ).toBe(true)
    }
    // Tick proof: ~2.4s of busy time (16 samples at 150ms) must show at
    // least two distinct elapsed second values.
    if (busySamples.length >= 16) {
      const elapsedSeen = new Set(busySamples.map((s) => s.elapsed).filter((t): t is string => t !== null))
      expect(elapsedSeen.size).toBeGreaterThanOrEqual(2)
    }

    // Completed settle: gauge turns into the static done fill and the frozen
    // elapsed stops ticking, observable inside the 5s auto-dismiss window.
    if (last.title !== null && /^Context compacted/.test(last.title)) {
      expect(last.gaugeDone).toBe(true)
      expect(last.gaugeIndeterminate).toBe(false)
      expect(last.gaugeAriaValueNow).toBe('100')
      expect(last.gaugeFillRatio).not.toBeNull()
      expect(last.gaugeFillRatio ?? 0).toBeGreaterThanOrEqual(0.99)
      if (last.elapsed !== null && (await card.count()) > 0) {
        const frozen = await card.locator('.chat-compact-status__elapsed').textContent().catch(() => null)
        if (frozen !== null) {
          await page.waitForTimeout(1200)
          if ((await card.count()) > 0) {
            await expect(card.locator('.chat-compact-status__elapsed')).toHaveText(frozen)
          }
        }
      }
    }

    // Terminal states keep the existing auto-dismiss timing.
    await expect(card).toHaveCount(0, { timeout: 15000 })
  })
})
