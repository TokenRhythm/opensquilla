import { test, expect, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const RAW_KEY_PATTERN = /agent:[a-z0-9_-]+:[a-z0-9_-]+:/i
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

async function openHub(page: Page) {
  await page.goto(CONTROL_URL + 'sessions')
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.waitForSelector('.conn-pill.connected', { timeout: 10000 }).catch(() => {})
  // Let the session ledger settle before inspecting it.
  await page.waitForTimeout(800)
  await expect(page.locator('.hub-ledger, .hub-state').first()).toBeVisible()
}

test.describe('Sessions Hub', () => {
  test('desktop / redirects to the Sessions Hub', async ({ page }) => {
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page).toHaveURL(/\/sessions$/)
    await expect(page.locator('.hub-task__input')).toBeVisible()
  })

  test('mobile / still lands on chat', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 667 })
    await page.goto(CONTROL_URL)
    await page.waitForSelector('.conn-pill', { timeout: 10000 })
    await expect(page).toHaveURL(/\/chat/)
  })

  test('ledger rows show human titles, never raw keys or UUIDs', async ({ page }) => {
    await openHub(page)

    const rows = page.locator('.hub-row')
    test.skip((await rows.count()) === 0, 'No sessions on this gateway; seed sessions to exercise the ledger')

    const ledgerText = await page.locator('.hub-ledger').innerText()
    expect(ledgerText).not.toMatch(RAW_KEY_PATTERN)
    expect(ledgerText).not.toMatch(UUID_PATTERN)

    // Every row carries a non-empty title, an agent chip, and a time-ago cell.
    const firstRow = rows.first()
    expect((await firstRow.locator('.hub-row__title').innerText()).trim().length).toBeGreaterThan(0)
    await expect(firstRow.locator('.hub-row__agent')).toBeAttached()
    expect((await firstRow.locator('.hub-row__time').innerText()).trim().length).toBeGreaterThan(0)
  })

  test('filter chips narrow the ledger by session kind', async ({ page }) => {
    await openHub(page)

    const rows = page.locator('.hub-row')
    const total = await rows.count()
    test.skip(total === 0, 'No sessions on this gateway; seed sessions to exercise filters')

    // The sidebar has its own filter chips; scope to the hub's group.
    const chips = page.locator('.hub-filters')
    const chatsChip = chips.getByRole('button', { name: 'Tasks', exact: true })
    await chatsChip.click()
    await expect(chatsChip).toHaveAttribute('aria-pressed', 'true')

    const filteredCount = await rows.count()
    if (filteredCount === 0) {
      // Honest filter-empty state with a way back.
      await expect(page.locator('.hub-state')).toContainText('No matches')
      await page.getByRole('button', { name: 'Clear filters' }).click()
    } else {
      // Task/subagent rows intentionally follow their chat parent through the
      // Chats filter; cron and channel sessions must be filtered out.
      for (const kind of await rows.evaluateAll(nodes => nodes.map(node => node.getAttribute('data-kind')))) {
        expect(['chat', 'task']).toContain(kind)
      }
      await chips.getByRole('button', { name: 'All', exact: true }).click()
    }
    await expect(page.locator('.hub-row')).toHaveCount(total)
  })

  test('task input starts the task in one step', async ({ page }) => {
    await openHub(page)

    const taskText = 'Summarize the latest gateway logs'
    await page.locator('.hub-task__input').fill(taskText)
    await page.getByRole('button', { name: 'Start task' }).click()

    // One step: the hand-off lands in chat and fires the send itself, so the
    // operator never has to press Enter a second time.
    await expect(page).toHaveURL(/\/chat/)
    await expect(
      page.locator('.msg-user').filter({ hasText: taskText }),
    ).toHaveCount(1, { timeout: 15000 })
    // The composer clears once the draft sends.
    await expect(page.locator('.chat-textarea')).toHaveValue('')
  })

  test('row click opens the inspect drawer instead of navigating', async ({ page }) => {
    await openHub(page)

    const rows = page.locator('.hub-row')
    test.skip((await rows.count()) === 0, 'No sessions on this gateway; seed sessions to exercise row inspection')

    await rows.first().locator('.hub-row__main').click()
    await expect(page.locator('.inspect-drawer')).toBeVisible()
    await expect(page).toHaveURL(/\/sessions$/)
  })

  test('attention strip collapses to one quiet line when fully idle', async ({ page }) => {
    await openHub(page)

    // Exactly one of the attention surfaces renders: the collapsed clear line
    // (idle) or the expanded tile grid (something needs attention).
    const clear = page.locator('.hub-attention-clear')
    const expanded = page.locator('.hub-attention')
    const isIdle = (await clear.count()) > 0
    test.skip(!isIdle, 'Gateway has pending approvals or active runs; idle collapse not exercised')

    await expect(clear).toBeVisible()
    await expect(expanded).toHaveCount(0)
    await expect(clear).toContainText('All clear')
    // The cost figure stays visible in the idle state.
    const text = (await clear.innerText()).trim()
    if (/\$/.test(text)) expect(text).toMatch(/\$\d+\.\d{2} today/)
  })

  test('ledger times read as relative ago strings', async ({ page }) => {
    await openHub(page)

    const rows = page.locator('.hub-row')
    test.skip((await rows.count()) === 0, 'No sessions on this gateway; seed sessions to exercise the ledger')

    const time = (await rows.first().locator('.hub-row__time').innerText()).trim()
    // Relative form ("just now" / "Ns/Nm/Nh/Nd ago") or an absolute fallback
    // beyond ~7 days — never a raw timestamp or ISO string.
    expect(time).toMatch(/^(just now|\d+[smhd] ago|\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}|—)$/)
  })

  test('subagent rows indent and read as lineage under their parent', async ({ page }) => {
    await openHub(page)

    const children = page.locator('.hub-row--child')
    test.skip((await children.count()) === 0, 'No subagent sessions on this gateway; seed a spawned run to exercise lineage')

    const first = children.first()
    // Indented one level: child main padding exceeds a root row's.
    const childPad = await first.locator('.hub-row__main').evaluate(
      el => parseFloat(getComputedStyle(el as HTMLElement).paddingLeft))
    const rootPad = await page.locator('.hub-row:not(.hub-row--child) .hub-row__main').first().evaluate(
      el => parseFloat(getComputedStyle(el as HTMLElement).paddingLeft))
    expect(childPad).toBeGreaterThan(rootPad)

    // Lineage title, never a flat "Spawned from" label or a raw key. Forked
    // conversations carry their own copied title behind the arrow; subagent
    // rows read as "↳ Subagent".
    const isFork = (await first.locator('.hub-row__fork-badge').count()) > 0
    const title = (await first.locator('.hub-row__title').innerText()).trim()
    expect(title.startsWith(isFork ? '↳ ' : '↳ Subagent')).toBe(true)
    expect(title).not.toMatch(RAW_KEY_PATTERN)
    expect(title).not.toMatch(UUID_PATTERN)
  })
})
