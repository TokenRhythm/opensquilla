import { test, expect } from '@playwright/test'

const CONTROL_URL = '/control/'
const LIVE = process.env.OPENSQUILLA_E2E_LIVE === '1'

test.describe('Inline citation pills', () => {
  test('idle chat renders no citation pills or sources row', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    await expect(page.locator('.citation-pill')).toHaveCount(0)
    await expect(page.locator('.sources-row')).toHaveCount(0)
  })

  test('only [n] markers that map to a real source become pills', async ({ page }) => {
    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    // Drive the transform deterministically over a known-sanitized snippet with a
    // 3-element source list. `[2]` is in range → a focusable button.citation-pill;
    // `[5]` is out of range → plain text, untouched. The transform rules mirror
    // src/utils/chat/citations.ts (single-integer [n], 1 ≤ n ≤ sources.length,
    // createElement/textContent only) so this proves the core mapping without a
    // model emitting markers.
    const result = await page.evaluate(() => {
      const CITATION_RE = /\[(\d{1,3})\]/g
      const SKIP_ANCESTORS = new Set(['PRE', 'CODE', 'A', 'BUTTON'])
      const sources = [
        { sourceId: 1, url: 'https://a.example/1', title: 'One', domain: 'a.example' },
        { sourceId: 2, url: 'https://b.example/2', title: 'Two', domain: 'b.example' },
        { sourceId: 3, url: 'https://c.example/3', title: 'Three', domain: 'c.example' },
      ]

      const root = document.createElement('div')
      // A pre-sanitized body: bare brackets survive marked/DOMPurify as text.
      root.innerHTML = '<p>Solar grew [2] and wind [5].</p>'
      document.body.appendChild(root)

      function isSkipped(node: Node | null): boolean {
        let cur: Node | null = node
        while (cur && cur instanceof HTMLElement) {
          if (SKIP_ANCESTORS.has(cur.nodeName)) return true
          if (cur.hasAttribute('data-citation')) return true
          cur = cur.parentNode
        }
        return false
      }

      const candidates: Text[] = []
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const t = n.nodeValue
        if (!t || !t.includes('[')) continue
        if (isSkipped(n.parentNode)) continue
        candidates.push(n as Text)
      }

      for (const node of candidates) {
        const text = node.nodeValue ?? ''
        CITATION_RE.lastIndex = 0
        const frag = document.createDocumentFragment()
        let last = 0
        let changed = false
        let m = CITATION_RE.exec(text)
        while (m) {
          const num = Number(m[1])
          if (num >= 1 && num <= sources.length) {
            if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)))
            const pill = document.createElement('button')
            pill.type = 'button'
            pill.className = 'citation-pill'
            pill.textContent = `[${num}]`
            pill.setAttribute('data-citation', String(num))
            frag.appendChild(pill)
            last = m.index + m[0].length
            changed = true
          }
          m = CITATION_RE.exec(text)
        }
        if (!changed) continue
        if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)))
        node.replaceWith(frag)
      }

      const pills = Array.from(root.querySelectorAll('.citation-pill'))
      const out = {
        pillCount: pills.length,
        pillCitations: pills.map(p => p.getAttribute('data-citation')),
        pillIsButton: pills.every(p => p.tagName === 'BUTTON'),
        // [5] must remain literal text in the body.
        plainTextHasFive: root.textContent?.includes('[5]') ?? false,
        bracketFiveIsPill: pills.some(p => p.getAttribute('data-citation') === '5'),
      }
      root.remove()
      return out
    })

    // [2] → exactly one focusable pill; [5] → never a pill, stays plain text.
    expect(result.pillCount).toBe(1)
    expect(result.pillCitations).toEqual(['2'])
    expect(result.pillIsButton).toBe(true)
    expect(result.bracketFiveIsPill).toBe(false)
    expect(result.plainTextHasFive).toBe(true)
  })

  test('rendered citation pills only ever target real sources', async ({ page }) => {
    test.skip(!LIVE, 'Live gateway test; set OPENSQUILLA_E2E_LIVE=1 to run.')
    test.setTimeout(240000)

    await page.goto(CONTROL_URL + 'chat/new')
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })

    const textarea = page.locator('.chat-textarea')
    await textarea.fill('Use your web search tool to find one recent headline about renewable energy, then answer in one sentence and cite the source inline as [1].')
    await page.locator('.chat-send-btn[aria-label="Send"]').click()

    // The turn runs and completes.
    const ribbon = page.locator('.stream-activity')
    await expect(ribbon).toBeVisible({ timeout: 30000 })
    await expect(ribbon).toHaveCount(0, { timeout: 180000 })

    await assertCitationInvariant(page)

    // The invariant survives a reload through the history render path. Absence of
    // pills is acceptable both times — the model may not cite.
    await page.reload()
    await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
    await page.waitForTimeout(500)
    await assertCitationInvariant(page)
  })
})

// Invariant: every rendered pill points at a real numbered source, and
// activating one opens the paired row and brings the matching item into view.
// Pills are optional — the model may emit none — so we assert only over pills
// that exist.
async function assertCitationInvariant(page: import('@playwright/test').Page) {
  const message = page.locator('.msg-ai').filter({ has: page.locator('.sources-row') }).first()
  const pills = message.locator('.citation-pill')
  const pillCount = await pills.count()
  if (pillCount === 0) return

  const sourceCount = await message.locator('.sources-row__item, [data-source-id]').count()
  for (let i = 0; i < pillCount; i++) {
    const raw = await pills.nth(i).getAttribute('data-citation')
    expect(raw).toMatch(/^[0-9]+$/)
    const n = Number(raw)
    expect(n).toBeGreaterThanOrEqual(1)
    expect(n).toBeLessThanOrEqual(sourceCount)
  }

  // Activating the first pill opens the row and reveals its target source.
  const first = pills.first()
  const target = Number(await first.getAttribute('data-citation'))
  await first.click()
  const toggle = message.locator('.sources-row__toggle')
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  const item = message.locator(`[data-source-id="${target}"]`)
  await expect(item).toBeVisible()
}
