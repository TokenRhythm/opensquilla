import { writeFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'
import { installSidebarFixture, SIDEBAR_ACTIVE_SESSION_KEY } from './support/sidebar-fixture'

test.describe.configure({ mode: 'default', timeout: 120_000 })
const key = (index: number) => `agent:main:webchat:e2e-sidebar-${index}`
const rowsSelector = '.sidebar-history-row[data-session-key]'
const listSelector = '.sidebar-history-list'

async function openSidebar(page: Page, count: number, current = 3) {
  const renames: unknown[] = []
  const pages: number[] = []
  const sessions = Array.from({ length: count }, (_, index) => ({
    key: key(index + 1), title: `Synthetic task ${String(index + 1).padStart(4, '0')}`,
    sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main',
    updatedAt: 1_800_000_000 - index, messageCount: 2, status: 'ok', runStatus: 'idle',
  }))
  await page.setViewportSize({ width: 1440, height: 900 })
  await installSidebarFixture(page, {
    'sessions.list': (params: Record<string, unknown>) => {
      expect(params.limit).toBe(200)
      const offset = Number(params.cursor || 0)
      pages.push(offset)
      return {
        sessions: sessions.slice(offset, offset + 200), count, ts: 1_800_000_000,
        has_more: offset + 200 < count, next_cursor: offset + 200 < count ? String(offset + 200) : null,
      }
    },
    'sessions.rename': (params: Record<string, unknown>) => {
      renames.push(params)
      const session = sessions.find(session => session.key === params.key)
      if (session) session.title = String(params.displayName)
      return { key: params.key, updated: ['displayName'] }
    },
  })
  await page.goto(`/control/chat?session=${encodeURIComponent(current === 3 ? SIDEBAR_ACTIVE_SESSION_KEY : key(current))}`)
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  const list = page.locator(listSelector)
  await expect(list).toHaveAttribute('data-sidebar-loaded-count', String(Math.min(count, 200)))
  return { list, pages, renames }
}

for (const count of [200, 1000, 3000]) {
  test(`${count} paginated tasks keep a bounded DOM and responsive rename`, async ({ page }, info) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => {
      const metrics = { inputPaint: [] as number[], longTasks: [] as number[] }
      Object.defineProperty(window, '__sidebarMetrics', { value: metrics })
      new PerformanceObserver(list => metrics.longTasks.push(...list.getEntries().map(entry => entry.duration)))
        .observe({ type: 'longtask', buffered: true })
      document.addEventListener('input', () => {
        const start = performance.now()
        requestAnimationFrame(() => metrics.inputPaint.push(performance.now() - start))
      }, true)
    })
    const { list, pages } = await openSidebar(page, count)
    await expect(list).toHaveAttribute('data-sidebar-virtualized', 'true')
    for (let loaded = 200; loaded < count; loaded += 200) {
      await list.evaluate(element => { element.scrollTop = element.scrollHeight })
      await expect(list).toHaveAttribute('data-sidebar-loaded-count', String(Math.min(count, loaded + 200)))
    }
    await page.waitForTimeout(500)
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('HeapProfiler.collectGarbage')
    const heap = await cdp.send('Runtime.getHeapUsage')
    const dom = await list.evaluate(element => ({
      sidebarNodes: element.querySelectorAll('*').length,
      totalNodes: document.querySelectorAll('*').length,
      renderedRows: element.querySelectorAll('[data-session-key]').length,
    }))
    await page.evaluate(() => {
      const metrics = (window as unknown as { __sidebarMetrics: { longTasks: number[] } }).__sidebarMetrics
      metrics.longTasks.length = 0
    })
    await list.hover()
    for (let index = 0; index < 12; index++) {
      await page.mouse.wheel(0, -500)
      await page.waitForTimeout(50)
    }
    await list.evaluate(element => { element.scrollTop = 0 })
    const first = page.locator(`[data-session-key="${key(1)}"]`)
    await first.locator('.sidebar-row-menu-btn').click()
    await page.getByRole('menuitem', { name: /Rename/ }).click()
    const input = first.locator('input')
    await expect(input).toBeFocused()
    await input.pressSequentially(' sidebar responsiveness', { delay: 15 })
    await input.press('Escape')
    await expect(first.locator('.sidebar-history-item')).toBeFocused()
    const metrics = await page.evaluate(() => (window as unknown as {
      __sidebarMetrics: { inputPaint: number[]; longTasks: number[] }
    }).__sidebarMetrics)
    const sorted = metrics.inputPaint.toSorted((a, b) => a - b)
    const report = {
      tasks: count, pages, ...dom, heapAfterGcMiB: heap.usedSize / 2 ** 20,
      inputSamples: sorted.length, inputPaintP95Ms: sorted[Math.ceil(sorted.length * .95) - 1]!,
      longestInteractionTaskMs: Math.max(0, ...metrics.longTasks), errors,
    }
    await writeFile(info.outputPath('sidebar-performance.json'), JSON.stringify(report, null, 2))
    await info.attach('sidebar-performance', { path: info.outputPath('sidebar-performance.json'), contentType: 'application/json' })
    await page.screenshot({ path: info.outputPath('sidebar.png') })
    expect(pages).toEqual(Array.from({ length: Math.ceil(count / 200) }, (_, index) => index * 200))
    expect(report.renderedRows).toBeLessThan(45)
    expect(report.sidebarNodes).toBeLessThan(600)
    expect(report.inputSamples).toBeGreaterThanOrEqual(20)
    expect(report.inputPaintP95Ms).toBeLessThanOrEqual(100)
    expect(report.longestInteractionTaskMs).toBeLessThanOrEqual(200)
    expect(errors).toEqual([])
    await cdp.detach()
  })
}

test('short lists retain native reading anchoring when an earlier row grows', async ({ page }) => {
  const { list } = await openSidebar(page, 90)
  await expect(list).toHaveAttribute('data-sidebar-virtualized', 'false')
  await expect(page.locator(rowsSelector)).toHaveCount(90)
  await expect(list).toHaveCSS('overflow-anchor', 'auto')
  await list.evaluate(element => { element.scrollTop = 900 })
  await list.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  const drift = await list.evaluate(async element => {
    const top = element.getBoundingClientRect().top
    const rows = [...element.querySelectorAll<HTMLElement>('[data-session-key]')]
    const anchor = rows.find(row => row.getBoundingClientRect().bottom > top)!
    const before = anchor.getBoundingClientRect().top
    rows[0]!.style.paddingTop = '80px'
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
    return Math.abs(anchor.getBoundingClientRect().top - before)
  })
  expect(drift).toBeLessThanOrEqual(2)
})

test('keeps offscreen rename drafts and menu focus without duplicate saves', async ({ page }) => {
  const { list, renames } = await openSidebar(page, 200)
  const first = page.locator(`[data-session-key="${key(1)}"]`)
  await first.locator('.sidebar-row-menu-btn').click()
  await list.evaluate(element => { element.scrollTop = 4500 })
  await expect(page.getByRole('menu')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(first.locator('.sidebar-row-menu-btn')).toBeFocused()
  await first.locator('.sidebar-row-menu-btn').click()
  await page.getByRole('menuitem', { name: /Rename/ }).click()
  const input = first.locator('input')
  await input.fill('Unsaved synthetic title')
  await list.evaluate(element => { element.scrollTop = 4500 })
  await expect(input).toBeFocused()
  await expect(input).toHaveValue('Unsaved synthetic title')
  expect(renames).toHaveLength(0)
  await input.press('Escape')
  await expect(first.locator('.sidebar-history-item')).toBeFocused()
  expect(renames).toHaveLength(0)
  await list.evaluate(element => { element.scrollTop = 0 })
  await first.locator('.sidebar-row-menu-btn').click()
  await page.getByRole('menuitem', { name: /Rename/ }).click()
  await first.locator('input').fill('Committed synthetic title')
  await first.locator('input').press('Enter')
  await expect.poll(() => renames.length).toBe(1)
  await expect(first.locator('.sidebar-history-item')).toBeFocused()
})

test('native Tab crosses virtual windows and bulk selection includes unmounted rows', async ({ page }) => {
  await openSidebar(page, 200)
  await page.locator(`[data-session-key="${key(1)}"] .sidebar-history-item`).focus()
  for (let index = 0; index < 60; index++) await page.keyboard.press('Tab')
  await expect(page.locator(`[data-session-key="${key(31)}"] .sidebar-history-item`)).toBeFocused()
  for (let index = 0; index < 60; index++) await page.keyboard.press('Shift+Tab')
  await expect(page.locator(`[data-session-key="${key(1)}"] .sidebar-history-item`)).toBeFocused()
  await expect.poll(() => page.locator(rowsSelector).count()).toBeLessThan(45)
  await page.locator('.sidebar-bulk-mode-btn').click()
  await page.locator('.sidebar-select-all-btn').click()
  await expect(page.locator('.sidebar-bulk-delete-btn')).toHaveAttribute('aria-label', /200/)
  await page.keyboard.press('Escape')
  await expect(page.locator('.sidebar-select-all-btn')).toHaveCount(0)
})

test('retains pointer capture while dragging beyond the initial virtual window', async ({ page }) => {
  const { list } = await openSidebar(page, 200)
  const source = page.locator(`[data-session-key="${key(1)}"]`)
  await source.hover({ position: { x: 60, y: 20 } })
  const start = (await source.boundingBox())!
  await page.mouse.down()
  await page.mouse.move(start.x + 65, start.y + 70, { steps: 5 })
  await expect(page.locator('.sidebar-session-drag-preview')).toBeVisible()
  await list.evaluate(element => { element.scrollTop = 4500 })
  await expect(source).toHaveClass(/is-dragging/)
  await expect(page.locator(`[data-session-key="${key(105)}"]`)).toBeInViewport()
  const target = await list.evaluate(element => {
    const viewport = element.getBoundingClientRect()
    const candidates = [...element.querySelectorAll<HTMLElement>('[data-session-key]')]
    const row = candidates.find(candidate => {
      const rect = candidate.getBoundingClientRect()
      return rect.top > viewport.top + 150 && rect.bottom < viewport.bottom - 80
    })!
    const rect = row.getBoundingClientRect()
    return { key: row.dataset.sessionKey!, x: rect.x + 65, y: rect.bottom - 6 }
  })
  await page.mouse.move(target.x, target.y, { steps: 8 })
  await expect(page.locator('.is-drop-after')).toHaveAttribute('data-session-key', target.key)
  await page.mouse.up()
  await expect(page.locator('.sidebar-session-drag-preview')).toHaveCount(0)
  await expect.poll(() => page.evaluate(({ source, target }) => {
    const order = JSON.parse(localStorage.getItem('opensquilla-sidebar-session-order-v1') || '[]') as string[]
    return order.indexOf(source) === order.indexOf(target) + 1
  }, { source: key(1), target: target.key })).toBe(true)
  await expect(page).toHaveURL(url => url.searchParams.get('session') === key(3))
})

test('reveals an initially offscreen selected task and remains usable after a drawer resize', async ({ page }) => {
  const { list } = await openSidebar(page, 200, 175)
  const selected = page.locator(`[data-session-key="${key(175)}"] .sidebar-history-item`)
  await expect(selected).toBeInViewport()
  const anchor = await list.evaluate(element => {
    const top = element.getBoundingClientRect().top
    const row = [...element.querySelectorAll<HTMLElement>('[data-session-key]')]
      .find(row => row.getBoundingClientRect().bottom > top)!
    return { key: row.dataset.sessionKey!, offset: row.getBoundingClientRect().top - top }
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByTestId('sidebar-toggle-collapsed').click()
  // The shorter drawer need not keep the old bottom row visible, but must
  // preserve the reader's top row instead of snapping to the selected task.
  await expect.poll(() => list.evaluate((element, anchor) => {
    const row = [...element.querySelectorAll<HTMLElement>('[data-session-key]')]
      .find(row => row.dataset.sessionKey === anchor.key)
    return row ? Math.abs(row.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset) : Infinity
  }, anchor)).toBeLessThanOrEqual(2)
  await list.hover()
  await page.mouse.wheel(0, -600)
  await expect.poll(() => page.locator(rowsSelector).count()).toBeLessThan(45)
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeFocused()
})

test('mixed project trees, pinned tasks and family folds retain correct geometry', async ({ page }) => {
  const sessions = Array.from({ length: 200 }, (_, index) => ({
    key: key(index + 1), title: `Mixed task ${index + 1}`, effectiveAgentId: 'main',
    sessionKind: index === 2 ? 'task' : index >= 180 ? 'cron' : 'chat',
    surface: index === 2 ? 'subagent' : index >= 180 ? 'cron' : 'webchat',
    conversationKind: 'direct', updatedAt: 1_800_000_000 - index,
    messageCount: 2, status: 'ok', runStatus: 'idle',
    workspaceId: index < 80 ? `synthetic-${Math.floor(index / 40) + 1}` : undefined,
    parent: index === 2 ? { key: key(2), spawnDepth: 1 } : undefined,
  }))
  await page.addInitScript(pinned => {
    localStorage.setItem('opensquilla-sidebar-pinned-sessions-v1', JSON.stringify([pinned]))
  }, key(1))
  await installSidebarFixture(page, {
    'sessions.list': { sessions, count: 200, ts: 1_800_000_000, has_more: false },
    'workspaces.list': { workspaces: [1, 2].map(index => ({
      id: `synthetic-${index}`, name: `Synthetic project ${index}`,
      path: `/synthetic/project-${index}`, taskCount: 40, pinned: false, available: true, removed: false,
    })) },
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/control/chat?session=${encodeURIComponent(key(2))}`)
  const list = page.locator(listSelector)
  await expect(list).toHaveAttribute('data-sidebar-loaded-count', '200')
  await expect(list).toHaveAttribute('data-sidebar-virtualized', 'true')
  await expect(page.locator(`[data-session-key="${key(1)}"]`)).toHaveAttribute('data-sidebar-zone', 'pinned')
  const project = page.locator('[data-sidebar-item-key="row:workspace:synthetic-1"]')
  const parent = list.locator(`[data-session-key="${key(2)}"]`)
  const child = list.locator(`[data-session-key="${key(3)}"]`)
  await expect(project).toBeVisible()
  await expect(child).toHaveCount(0)
  await parent.locator('.sidebar-task-disclosure').click()
  await expect(child).toBeInViewport()
  await expect(child).toHaveAttribute('data-sidebar-zone', 'projects')
  await project.locator('.sidebar-project-disclosure').click()
  await expect(parent).toHaveCount(0)
  await expect(child).toHaveCount(0)
  await expect(page.locator('[data-sidebar-item-key="row:workspace:synthetic-2"]')).toBeInViewport()
  await project.locator('.sidebar-project-disclosure').click()
  await expect(child).toBeInViewport()
  // Offscreen headings remain keyboard-addressable. Collapsing a distant
  // family removes its virtual extent, not just its mounted rows.
  await list.evaluate(element => { element.scrollTop = element.scrollHeight })
  const family = page.locator('[data-family="automations"] .sidebar-group__header')
  await expect(family).toBeVisible()
  const before = await list.evaluate(element => element.scrollHeight)
  await family.click()
  await expect(family).toHaveAttribute('aria-expanded', 'false')
  await expect.poll(() => list.evaluate(element => element.scrollHeight)).toBeLessThan(before - 600)
  await family.click()
  await expect(family).toHaveAttribute('aria-expanded', 'true')
  await list.evaluate(element => { element.scrollTop = element.scrollHeight })
  await expect(page.locator(`[data-session-key="${key(200)}"]`)).toBeInViewport()
  await expect.poll(() => page.locator(rowsSelector).count()).toBeLessThan(45)
})
