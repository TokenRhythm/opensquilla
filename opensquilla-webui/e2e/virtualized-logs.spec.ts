import { fileURLToPath } from 'node:url'
import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'
import { validateResult as validateLogsStatus } from '../src/contracts/generated/v4/logsStatusValidators.mjs'
import { validateResult as validateLogsTail } from '../src/contracts/generated/v4/logsTailValidators.mjs'

test.use({ viewport: { width: 1440, height: 900 } })
test.describe.configure({ timeout: 60_000 })

function lines(start: number, count: number) {
  return Array.from({ length: count }, (_, index) => ({
    level: index % 3 === 0 ? 'WARN' : 'INFO',
    timestamp: '2026-01-01T12:00:00.000Z',
    message: `Synthetic log #${String(start + index).padStart(6, '0')} • `
      + 'A synthetic diagnostic line that wraps naturally with the available width. '.repeat(1 + index % 6),
  }))
}

async function openLogs(page: Page) {
  const state = { delivered: 0, pending: [lines(0, 500)] }
  const status = {
    gateway_file_log: { enabled: true, path: 'synthetic-debug.log' },
    raw_turn_call_log: { enabled: false, source: 'off', directory: { path: 'synthetic-logs' } },
    diagnostics_enabled: {},
  }
  expect(validateLogsStatus(status)).toBe(true)
  await installSidebarFixture(page, {
    'logs.status': status,
    get 'logs.tail'() {
      const next = state.pending.shift() ?? []
      state.delivered += next.length
      const payload = { lines: next, cursor: state.delivered, has_more: false }
      expect(validateLogsTail(payload)).toBe(true)
      return payload
    },
  })
  await page.addInitScript(() => localStorage.setItem('opensquilla.logs.runTrace', '1'))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public/opensquilla-mark.png', import.meta.url)), contentType: 'image/png',
  }))
  await page.goto('/control/logs')
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(page.locator('.lg-line').last()).toContainText('Synthetic log #000499')
  await settle(page)
  return state
}

async function geometry(page: Page) {
  return page.locator('.lg-display').evaluate(display => {
    const bounds = display.getBoundingClientRect()
    const rows = [...display.querySelectorAll<HTMLElement>('.lg-line')].map(row => {
      const rect = row.getBoundingClientRect()
      return { key: row.textContent?.match(/Synthetic log #\d+/)?.[0] ?? '',
        top: rect.top - bounds.top, bottom: rect.bottom - bounds.top, height: rect.height }
    })
    return { offset: display.scrollTop, height: display.scrollHeight, viewport: display.clientHeight,
      gap: display.scrollHeight - display.scrollTop - display.clientHeight, rows }
  })
}

async function settle(page: Page) {
  let previous = '', stable = 0
  await expect.poll(async () => {
    const current = JSON.stringify(await geometry(page))
    stable = current === previous ? stable + 1 : 0
    previous = current
    return stable
  }, { intervals: [100] }).toBeGreaterThanOrEqual(3)
}

async function readingAnchor(page: Page, key?: string) {
  const state = await geometry(page)
  const row = key ? state.rows.find(row => row.key === key)
    : state.rows.find(row => row.bottom > 1 && row.top < state.viewport)
  expect(row, 'The original reading row must remain mounted').toBeDefined()
  return row!
}

async function readOlder(page: Page) {
  await page.locator('.lg-toggle input').uncheck()
  await page.locator('.lg-display').hover({ position: { x: 80, y: 120 } })
  await page.mouse.wheel(0, -5_000)
  await settle(page)
  expect((await geometry(page)).gap).toBeGreaterThan(500)
}

async function evidence(page: Page, info: TestInfo, label: string) {
  const screenshot = info.outputPath(`${label}.png`)
  await page.screenshot({ path: screenshot, fullPage: true })
  await info.attach(label, { path: screenshot, contentType: 'image/png' })
  await info.attach(`${label}-geometry`, {
    body: JSON.stringify(await geometry(page), null, 2), contentType: 'application/json',
  })
}

test.beforeEach(async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  Object.assign(page, { virtualLogsErrors: errors })
})

test.afterEach(async ({ page }, info) => {
  const errors = (page as Page & { virtualLogsErrors: string[] }).virtualLogsErrors
  await info.attach('console-health', { body: JSON.stringify(errors), contentType: 'application/json' })
  if (await page.locator('.lg-display').count()) await evidence(page, info, 'final')
  await expect(page.locator('vite-error-overlay')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('500 mixed-height log rows stay bounded without overlap under reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await openLogs(page)
  const state = await geometry(page)
  expect(state.gap).toBeLessThanOrEqual(2)
  expect(state.rows.length).toBeLessThan(80)
  expect(new Set(state.rows.map(row => row.height)).size).toBeGreaterThan(1)
  for (let index = 1; index < state.rows.length; index++) {
    expect(state.rows[index]!.top - state.rows[index - 1]!.bottom).toBeGreaterThanOrEqual(-0.5)
  }
  await expect(page).toHaveURL(/\/control\/logs$/)
  expect(await page.title()).not.toBe('')
})

test('appending another 500 logs preserves reading position until Auto follow is re-enabled', async ({ page }) => {
  const fixture = await openLogs(page)
  await readOlder(page)
  const before = await readingAnchor(page)
  fixture.pending.push(lines(500, 500))
  await expect.poll(() => fixture.delivered, { timeout: 10_000 }).toBe(1_000)
  await expect(page.locator('.lg-stream__foot')).toContainText('1,000')
  await settle(page)
  expect(Math.abs((await readingAnchor(page, before.key)).top - before.top)).toBeLessThanOrEqual(2)
  expect((await geometry(page)).rows.length).toBeLessThan(80)
  await page.locator('.lg-toggle input').check()
  await expect(page.locator('.lg-line').last()).toContainText('Synthetic log #000999')
  await settle(page)
  expect((await geometry(page)).gap).toBeLessThanOrEqual(2)
})

test('desktop width changes preserve the first visible log row', async ({ page }, info) => {
  await openLogs(page)
  await readOlder(page)
  const before = await readingAnchor(page)
  await evidence(page, info, 'wide-before')
  await page.setViewportSize({ width: 1000, height: 900 })
  await settle(page)
  expect(Math.abs((await readingAnchor(page, before.key)).top - before.top)).toBeLessThanOrEqual(2)
  await evidence(page, info, 'narrow')
  await page.setViewportSize({ width: 1440, height: 900 })
  await settle(page)
  expect(Math.abs((await readingAnchor(page, before.key)).top - before.top)).toBeLessThanOrEqual(2)
})

test('narrow-screen wrapped logs remain virtualized and survive the desktop layout breakpoint', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await openLogs(page)
  expect((await geometry(page)).rows.length).toBeLessThan(80)
  await readOlder(page)
  const before = await readingAnchor(page)
  await evidence(page, info, 'narrow-before')
  await page.setViewportSize({ width: 800, height: 844 })
  await settle(page)
  expect(Math.abs((await readingAnchor(page, before.key)).top - before.top)).toBeLessThanOrEqual(2)
  await evidence(page, info, 'wide-after')
  await page.setViewportSize({ width: 390, height: 844 })
  await settle(page)
  expect(Math.abs((await readingAnchor(page, before.key)).top - before.top)).toBeLessThanOrEqual(2)
  expect((await geometry(page)).rows.length).toBeLessThan(80)
  await evidence(page, info, 'narrow-restored')
})

test('keyboard detail navigation restores focus to the same virtual log row', async ({ page }) => {
  await openLogs(page)
  await readOlder(page)
  const key = (await readingAnchor(page)).key
  const row = page.locator('.lg-line').filter({ hasText: key })
  await row.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.lg-detail')).toBeVisible()
  await page.keyboard.press('Tab')
  expect(await page.locator('.lg-detail').evaluate(drawer => drawer.contains(document.activeElement))).toBe(true)
  await page.keyboard.press('Escape')
  await expect(page.locator('.lg-detail')).toHaveCount(0)
  await expect(row).toBeFocused()
})
