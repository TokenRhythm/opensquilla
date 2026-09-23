import { fileURLToPath } from 'node:url'
import { expect as baseExpect, test, type Page, type TestInfo, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload } from './support/session-read-fixtures'

const expect = baseExpect.configure({ timeout: 10_000 })
test.use({ viewport: { width: 1440, height: 900 } })
test.describe.configure({ timeout: 120_000 })

const SESSION = 'agent:main:webchat:e2e-virtual-history'
type Metrics = Record<string, unknown>

function transcript(total: number) {
  return Array.from({ length: total }, (_, index) => ({
    role: index % 2 === 0 ? 'user' : 'assistant',
    id: `history-${index}`, message_id: `history-${index}`,
    timestamp: 1_800_000_000 + index,
    text: index % 2 === 0
      ? `Synthetic question ${index}. ${'Readable wrapping sentence. '.repeat(1 + index % 8)}`
      : `Synthetic answer ${index}.\n\n` + Array.from({ length: 1 + index % 7 }, (_, p) =>
        `Paragraph ${p + 1} of answer ${index}. ${'Variable height content for browser layout. '.repeat(2 + index % 5)}`,
      ).join('\n\n') + (index % 5 === 0 ? '\n\n```ts\nconst synthetic = true\nconsole.log(synthetic)\n```' : ''),
  }))
}

async function gateway(page: Page, total: number) {
  const history = transcript(total)
  const state = { total, offset: total, pages: [] as unknown[], pauseBelow: 0, hold: false, pending: null as null | (() => void) }
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla.chat.virtualizeHistory', 'true')
    localStorage.removeItem('opensquilla.sidebar.width.v1')
  })
  // Vite preview has no gateway static proxy. Serve the same checked-in brand asset.
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  for (const endpoint of ['approvals', 'system/update', 'elevated-mode']) {
    await page.route(`**/api/${endpoint}`, route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ pending: [], enabled: false }),
    }))
  }
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const f = JSON.parse(String(raw))
      if (f.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (f.type !== 'req') return
      if (f.method === 'connect') {
        ws.send(helloOkResponse({ auth: { principal: { isOwner: true, authenticated: true } } }))
        return
      }
      const send = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: f.id, ok: true, payload }))
      if (f.method === 'chat.history') {
        const limit = Math.max(1, Math.min(200, Number(f.params?.limit) || 50))
        const before = String(f.params?.before || '')
        const end = before.startsWith('cursor-') ? Number(before.slice(7)) : total
        const start = Math.max(0, end - limit)
        const reply = () => {
          state.offset = Math.min(state.offset, start)
          state.pages.push({ before, limit, start, end, session: f.params?.key || f.params?.sessionKey })
          send(chatHistoryPayload(history.slice(start, end), {
            has_more: start > 0, oldest_cursor: start ? `cursor-${start}` : null,
            newest_cursor: `cursor-${end}`, page_size: limit,
            history_scope: start ? 'latest_window' : 'complete',
          }))
        }
        if (before && (state.hold || start < state.pauseBelow)) state.pending = reply
        else reply()
        return
      }
      const payload: Record<string, unknown> = {
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      send(payload[String(f.method)] ?? {})
    })
  })
  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION))
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect.poll(() => state.offset).toBeLessThan(total)
  return state
}

async function loadUntil(page: Page, state: Awaited<ReturnType<typeof gateway>>, targetOffset = 0) {
  state.pauseBelow = targetOffset
  const markers = page.getByTestId('conversation-minimap-marker')
  const targetTurns = (state.total - targetOffset) / 2
  await expect.poll(() => markers.count()).toBeGreaterThan(0)
  while (await markers.count() < targetTurns) {
    await expect.poll(async () => {
      const idle = await page.getByTestId('history-load-sentinel').evaluateAll(elements =>
        elements[0]?.classList.contains('history-load-sentinel--idle') ?? false)
      return await markers.count() >= targetTurns || idle
    }).toBe(true)
    // Another intersection can commit the final page while the idle check is pending.
    const previous = await markers.count()
    if (previous >= targetTurns) break
    await page.locator('.chat-thread').hover({ position: { x: 80, y: 120 } })
    await page.mouse.wheel(0, -1_000_000)
    await expect.poll(() => markers.count()).toBeGreaterThan(previous)
  }
  await expect(markers).toHaveCount(targetTurns)
  await expect(page.locator('.chat-message-list')).toHaveAttribute('data-virtualized', 'true')
  if (targetOffset === 0) {
    await expect(page.getByTestId('history-load-sentinel')).toHaveCount(0)
    await expect(page.getByTestId('conversation-minimap-marker')).toHaveCount(state.total / 2)
  }
  await expect(page.locator('.chat-jump-latest')).toBeVisible()
  await page.locator('.chat-jump-latest').click()
  await settleLayout(page, true)
  await expect(page.locator(`[data-chat-message-key="history-${state.total - 1}"]`))
    .toHaveAttribute('data-chat-message-index', String(state.total - targetOffset - 1))
}

async function settleLayout(page: Page, atLatest = false) {
  let previous = '', stable = 0
  await expect.poll(async () => {
    const measurement = await page.evaluate(() => {
      const thread = document.querySelector<HTMLElement>('.chat-thread')!
      const bounds = thread.getBoundingClientRect()
      const rows = [...thread.querySelectorAll<HTMLElement>('[data-testid="chat-message-row"]')]
      const rects = rows.map(row => row.getBoundingClientRect())
      const last = rects.at(-1)
      const animating = document.querySelector('.chat')?.getAnimations({ subtree: true }).some(a =>
        a.playState === 'running' && a.effect?.getComputedTiming().endTime !== Infinity)
      return { signature: JSON.stringify([thread.scrollTop, thread.scrollHeight, thread.clientHeight,
        ...rows.map((row, i) => [row.dataset.chatMessageKey, rects[i]!.top, rects[i]!.height])]),
        visible: rects.some(r => r.bottom > bounds.top && r.top < bounds.bottom),
        lastVisible: !!last && last.bottom > bounds.top && last.top < bounds.bottom,
        gap: thread.scrollHeight - thread.scrollTop - thread.clientHeight, animating }
    })
    stable = measurement.signature === previous ? stable + 1 : 0
    previous = measurement.signature
    return stable >= 4 && measurement.visible && !measurement.animating
      && (!atLatest || (measurement.lastVisible && measurement.gap <= 2))
  }, { intervals: [100] }).toBe(true)
}

async function snapshot(page: Page) {
  return page.evaluate(() => {
    const thread = document.querySelector<HTMLElement>('.chat-thread')!
    const rect = thread.getBoundingClientRect()
    const rows = [...document.querySelectorAll<HTMLElement>('[data-testid="chat-message-row"]')]
    return {
      rendered: rows.length, forced: rows.filter(e => e.dataset.chatMessageForced === 'true').length,
      ordinary: rows.filter(e => e.dataset.chatMessageForced !== 'true').length,
      scrollTop: thread.scrollTop, scrollHeight: thread.scrollHeight,
      gap: thread.scrollHeight - thread.scrollTop - thread.clientHeight,
      viewportHeight: thread.clientHeight, reading: thread.classList.contains('chat-thread--reading-history'),
      rows: rows.map(e => ({ key: e.dataset.chatMessageKey, height: e.getBoundingClientRect().height,
        top: e.getBoundingClientRect().top - rect.top, forced: e.dataset.chatMessageForced })),
    }
  })
}

async function rowAnchor(page: Page, key?: string) {
  return page.locator('.chat-thread').evaluate((thread, expected) => {
    const bounds = thread.getBoundingClientRect()
    const rows = [...thread.querySelectorAll<HTMLElement>('[data-testid="chat-message-row"]')]
    const row = expected ? rows.find(e => e.dataset.chatMessageKey === expected) : rows.find(e => {
      const r = e.getBoundingClientRect(); return r.bottom > bounds.top + 1 && r.top < bounds.bottom
    })
    if (!row) return null
    const r = row.getBoundingClientRect()
    return { key: row.dataset.chatMessageKey!, top: r.top - bounds.top, height: r.height,
      text: row.innerText.slice(0, 100), width: r.width, absoluteTop: r.top - bounds.top + thread.scrollTop }
  }, key)
}

async function screenshotEvidence(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`)
  await page.screenshot({ path, fullPage: true })
  await info.attach(`${name}.png`, { path, contentType: 'image/png' })
  return path
}

async function diagnose(page: Page, info: TestInfo, work: (metrics: Metrics) => Promise<void>) {
  const metrics: Metrics = { case: info.title, rowBudget: 'ordinary <= 30; forced rows are correctness leases' }
  const consoleErrors: string[] = [], pageErrors: string[] = []
  const httpErrors: { url: string; status: number; resourceType: string }[] = []
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('response', response => {
    if (response.status() >= 400) httpErrors.push({
      url: response.url(), status: response.status(), resourceType: response.request().resourceType(),
    })
  })
  try { await work(metrics) } finally {
    metrics.final = await snapshot(page).catch(error => ({ error: String(error) }))
    metrics.browserEvidence = await page.evaluate(() => ({
      url: location.href, title: document.title,
      meaningfulDom: document.querySelector('#app-main')?.textContent?.trim().slice(0, 1800) || '',
      frameworkErrorOverlay: !!document.querySelector('vite-error-overlay'),
      messageRows: document.querySelectorAll('[data-testid="chat-message-row"]').length,
      composer: !!document.querySelector('.chat-textarea'),
    })).catch(error => ({ error: String(error) }))
    metrics.consoleHealth = { consoleErrors, pageErrors, httpErrors, pageErrorFree: pageErrors.length === 0 }
    metrics.screenshot = await screenshotEvidence(page, info, 'browser-evidence')
      .catch(error => ({ error: String(error) }))
    await info.attach('metrics.json', { body: JSON.stringify(metrics, null, 2), contentType: 'application/json' })
  }
}

test('1000 mixed-height messages use a bounded real DOM after protocol-sized pagination', async ({ page }, info) => {
  await diagnose(page, info, async m => {
    const state = await gateway(page, 1000)
    m.historyPages = state.pages
    await loadUntil(page, state)
    const latest = await snapshot(page); m.latest = latest
    await page.locator('.chat-thread').hover({ position: { x: 80, y: 120 } })
    await page.mouse.wheel(0, -latest.scrollTop / 2)
    await expect(page.locator('.chat-thread')).toHaveClass(/chat-thread--reading-history/)
    await settleLayout(page)
    const reading = await snapshot(page); m.reading = reading
    expect(reading.scrollTop).toBeGreaterThan(latest.scrollTop * 0.2)
    expect(reading.scrollTop).toBeLessThan(latest.scrollTop * 0.8)
    expect(reading.gap).toBeGreaterThan(latest.scrollTop * 0.2)
    expect(reading.gap).toBeLessThan(latest.scrollTop * 0.8)
    expect(reading.ordinary).toBeLessThanOrEqual(30)
    expect(new Set(reading.rows.map(r => Math.round(r.height))).size).toBeGreaterThan(2)
    expect(state.offset).toBe(0)
  })
})

for (const reducedMotion of ['no-preference', 'reduce'] as const) {
  test(`far minimap navigation mounts and arrives at target (${reducedMotion})`, async ({ page }, info) => {
    await diagnose(page, info, async m => {
      await page.emulateMedia({ reducedMotion })
      const state = await gateway(page, 200); m.historyPages = state.pages; await loadUntil(page, state)
      m.before = await snapshot(page)
      await expect(page.locator('[data-chat-turn-key="history-40"]')).toHaveCount(0)
      const samplesPromise = page.evaluate(async () => {
        const samples: unknown[] = [], mountEvents: unknown[] = [], started = performance.now()
        const observer = new MutationObserver(records => {
          const row = document.querySelector<HTMLElement>('[data-chat-message-key="history-40"]')
          if (row) mountEvents.push({ ms: performance.now() - started, mounted: true,
            forced: row.dataset.chatMessageForced,
            arrived: !!row.querySelector('.is-history-target'), recordTypes: records.map(r => r.type) })
        })
        observer.observe(document.querySelector('.chat-message-list')!, {
          childList: true, subtree: true, attributes: true,
          attributeFilter: ['data-chat-message-forced', 'class'],
        })
        while (performance.now() - started < 2600) {
          const t = document.querySelector<HTMLElement>('.chat-thread')!
          const target = document.querySelector<HTMLElement>('[data-chat-turn-key="history-40"]')
          samples.push({ ms: performance.now() - started, scrollTop: t.scrollTop,
            mounted: !!target, forced: target?.closest<HTMLElement>('[data-chat-message-forced]')?.dataset.chatMessageForced,
            arrived: target?.classList.contains('is-history-target'),
            targetTop: target ? target.getBoundingClientRect().top - t.getBoundingClientRect().top : null })
          await new Promise(requestAnimationFrame)
        }
        observer.disconnect()
        return { samples: samples as { mounted: boolean; forced?: string; arrived?: boolean; targetTop: number | null }[],
          mountEvents: mountEvents as { mounted: boolean; forced?: string; arrived?: boolean }[] }
      })
      await page.getByTestId('conversation-minimap-marker').nth(20).click()
      const { samples, mountEvents } = await samplesPromise
      m.samples = samples; m.mountEvents = mountEvents; m.after = await snapshot(page)
      expect([...samples, ...mountEvents].some(s => s.mounted)).toBe(true)
      expect([...samples, ...mountEvents].some(s => s.forced === 'true')).toBe(true)
      expect([...samples, ...mountEvents].some(s => s.arrived)).toBe(true)
      const settled = samples.slice(-12)
      m.settledTargetErrorsPx = settled.map(s => s.targetTop === null ? null : s.targetTop - 16)
      expect(settled.length).toBe(12)
      expect(settled.every(s => s.targetTop !== null && Math.abs(s.targetTop - 16) <= 4)).toBe(true)
      expect((await snapshot(page)).ordinary).toBeLessThanOrEqual(30)
    })
  })
}

test('far minimap navigation keeps its stable-key destination through a live width change', async ({ page }, info) => {
  await diagnose(page, info, async m => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const state = await gateway(page, 200)
    m.historyPages = state.pages
    await loadUntil(page, state)
    await expect(page.locator('[data-chat-turn-key="history-40"]')).toHaveCount(0)
    const before = await page.evaluate(() => {
      const measure = () => {
        const thread = document.querySelector<HTMLElement>('.chat-thread')!
        const target = thread.querySelector<HTMLElement>('[data-chat-turn-key="history-40"]')
        return {
          shellWidth: document.querySelector<HTMLElement>('.chat-thread-shell')!.clientWidth,
          listWidth: document.querySelector<HTMLElement>('.chat-message-list')!.getBoundingClientRect().width,
          forced: target?.closest<HTMLElement>('[data-chat-message-forced]')?.dataset.chatMessageForced,
          arrived: !!target?.classList.contains('is-history-target'),
          targetTop: target ? target.getBoundingClientRect().top - thread.getBoundingClientRect().top : null,
        }
      }
      window.addEventListener('resize', () => {
        Object.assign(window, { __virtualNavigationResize: measure() })
      }, { once: true })
      return measure()
    })
    m.beforeResize = before
    expect(before.shellWidth).toBeGreaterThanOrEqual(1120)
    await page.getByTestId('conversation-minimap-marker').nth(20).click()
    await page.setViewportSize({ width: 1680, height: 900 })
    await expect.poll(() => page.evaluate(() => '__virtualNavigationResize' in window)).toBe(true)
    const resized = await page.evaluate(() => (
      window as unknown as { __virtualNavigationResize: {
        shellWidth: number; listWidth: number; forced?: string; arrived: boolean; targetTop: number | null
      } }
    ).__virtualNavigationResize)
    m.duringResize = resized
    // Prove real list geometry changed while the original navigation still
    // owned its destination, without hiding the rail or mocking measurements.
    expect(resized.shellWidth).toBeGreaterThanOrEqual(1120)
    expect(resized.listWidth - before.listWidth).toBeGreaterThan(200)
    expect(resized.forced).toBe('true')
    expect(resized.arrived).toBe(false)
    expect(resized.targetTop).not.toBeNull()
    expect(Math.abs(resized.targetTop! - 16)).toBeGreaterThan(4)
    await expect(page.getByTestId('conversation-minimap')).toBeVisible()
    const samples = await page.evaluate(async () => {
      const started = performance.now()
      const samples: { key?: string; targetTop: number | null; forced?: string; arrived: boolean }[] = []
      while (performance.now() - started < 2600) {
        const thread = document.querySelector<HTMLElement>('.chat-thread')!
        const target = thread.querySelector<HTMLElement>('[data-chat-turn-key="history-40"]')
        samples.push({
          key: target?.dataset.chatTurnKey,
          targetTop: target ? target.getBoundingClientRect().top - thread.getBoundingClientRect().top : null,
          forced: target?.closest<HTMLElement>('[data-chat-message-forced]')?.dataset.chatMessageForced,
          arrived: !!target?.classList.contains('is-history-target'),
        })
        await new Promise(requestAnimationFrame)
      }
      return samples
    })
    m.samples = samples
    expect(samples.some(sample => sample.arrived)).toBe(true)
    const settled = samples.slice(-12)
    expect(settled).toHaveLength(12)
    expect(settled.every(sample => sample.key === 'history-40'
      && sample.targetTop !== null && Math.abs(sample.targetTop - 16) <= 4)).toBe(true)
    expect(settled.every(sample => sample.forced === 'false')).toBe(true)
    expect((await snapshot(page)).ordinary).toBeLessThanOrEqual(30)
  })
})

test('a real wheel gesture cancels a pending far minimap navigation without resuming it', async ({ page }, info) => {
  await diagnose(page, info, async m => {
    const state = await gateway(page, 200)
    await loadUntil(page, state)
    await page.evaluate(() => {
      const thread = document.querySelector<HTMLElement>('.chat-thread')!
      thread.addEventListener('wheel', () => {
        const target = document.querySelector<HTMLElement>('[data-chat-message-key="history-40"]')
        Object.assign(window, { __virtualNavigationWheel: {
          forced: target?.dataset.chatMessageForced,
          scrollTop: thread.scrollTop,
          arrived: !!target?.querySelector('.is-history-target'),
        } })
      }, { once: true, capture: true })
    })
    await page.getByTestId('conversation-minimap-marker').nth(20).click()
    await page.locator('.chat-thread').hover({ position: { x: 80, y: 120 } })
    await page.mouse.wheel(0, -600)
    await expect.poll(() => page.evaluate(() => '__virtualNavigationWheel' in window)).toBe(true)
    const wheel = await page.evaluate(() => (
      window as unknown as { __virtualNavigationWheel?: { forced?: string; scrollTop: number; arrived: boolean } }
    ).__virtualNavigationWheel)
    m.wheel = wheel
    // Prove the gesture interrupted a leased destination, not an already
    // completed seek. The wheel is real browser input, not a scrollTop write.
    expect(wheel?.forced).toBe('true')
    expect(wheel?.arrived).toBe(false)
    await expect(page.locator('.chat-thread')).toHaveClass(/chat-thread--reading-history/)
    await settleLayout(page)
    const anchor = await rowAnchor(page)
    expect(anchor).not.toBeNull()
    m.cancelledAnchor = anchor
    const samples = await page.evaluate(async (key) => {
      const thread = document.querySelector<HTMLElement>('.chat-thread')!
      const started = performance.now(), positions: (number | null)[] = []
      while (performance.now() - started < 2600) {
        const row = thread.querySelector<HTMLElement>(`[data-chat-message-key="${key}"]`)
        positions.push(row ? row.getBoundingClientRect().top - thread.getBoundingClientRect().top : null)
        await new Promise(requestAnimationFrame)
      }
      return positions
    }, anchor!.key)
    m.cancelledAnchorSamples = samples
    expect(samples.length).toBeGreaterThan(12)
    expect(samples.every(top => top !== null && Math.abs(top - anchor!.top) <= 2)).toBe(true)
    await expect(page.locator('[data-chat-message-key="history-40"][data-chat-message-forced="true"]')).toHaveCount(0)
    await expect(page.locator('[data-chat-turn-key="history-40"].is-history-target')).toHaveCount(0)
  })
})

test('virtualized reading preserves a stable row boundary through real width reflow', async ({ page }, info) => {
  await diagnose(page, info, async m => {
    const state = await gateway(page, 200); m.historyPages = state.pages; await loadUntil(page, state)
    const latest = await snapshot(page); m.latest = latest
    await page.locator('.chat-thread').hover({ position: { x: 80, y: 120 } }); await page.mouse.wheel(0, -3000)
    await expect(page.locator('.chat-thread')).toHaveClass(/chat-thread--reading-history/)
    await settleLayout(page)
    const reader = await snapshot(page); m.reader = reader
    expect(reader.gap).toBeGreaterThan(1000)
    expect(reader.scrollTop).toBeLessThan(latest.scrollTop - 1000)
    const candidate = await rowAnchor(page); expect(candidate).not.toBeNull()
    // Place the first intersecting row boundary just above the viewport. Internal
    // reflow below this boundary cannot legitimately move the boundary itself.
    await page.mouse.wheel(0, candidate!.top + 4); await settleLayout(page)
    const before = await rowAnchor(page, candidate!.key); m.before = before; expect(before).not.toBeNull()
    expect(before!.top).toBeGreaterThanOrEqual(-8); expect(before!.top).toBeLessThanOrEqual(0)
    m.beforeScreenshot = await screenshotEvidence(page, info, 'width-before')
    await page.setViewportSize({ width: 1000, height: 900 }); await settleLayout(page)
    const narrow = await rowAnchor(page, before!.key); m.narrow = narrow
    m.narrowRowStartDriftPx = narrow ? narrow.top - before!.top : null
    m.narrowScreenshot = await screenshotEvidence(page, info, 'width-narrow')
    await page.setViewportSize({ width: 1440, height: 900 }); await settleLayout(page)
    const restored = await rowAnchor(page, before!.key); m.restored = restored
    m.restoredRowStartDriftPx = restored ? restored.top - before!.top : null
    m.restoredScreenshot = await screenshotEvidence(page, info, 'width-restored')
    m.interpretation = 'The first intersecting row starts within 8px above the viewport before reflow. This measures row-boundary retention, not arbitrary paragraph text anchoring; changed row height itself is expected.'
    expect(narrow).not.toBeNull(); expect(restored).not.toBeNull()
    expect(Math.abs(narrow!.width - before!.width)).toBeGreaterThan(50)
    expect(Math.abs(narrow!.top - before!.top)).toBeLessThanOrEqual(2)
    expect(Math.abs(restored!.top - before!.top)).toBeLessThanOrEqual(2)
    expect((await snapshot(page)).reading).toBe(true)
  })
})

test('successful real history prepend retains a visible stable-key reading anchor', async ({ page }, info) => {
  await diagnose(page, info, async m => {
    const state = await gateway(page, 200); m.historyPages = state.pages; await loadUntil(page, state, 100)
    state.hold = true
    await page.locator('.chat-thread').hover({ position: { x: 80, y: 120 } }); await page.mouse.wheel(0, -1_000_000)
    await expect.poll(() => !!state.pending).toBe(true)
    await settleLayout(page)
    const before = await rowAnchor(page); m.before = before; expect(before).not.toBeNull()
    // Release exactly one page. A subsequent intersection stays pending.
    const releaseOnePage = state.pending!
    state.pending = null
    releaseOnePage()
    await expect(page.getByTestId('conversation-minimap-marker')).toHaveCount(75)
    m.releasedPrependPages = 1
    await settleLayout(page)
    const after = await rowAnchor(page, before!.key); m.after = after; m.historyPages = state.pages
    m.rowStartDriftPx = after ? after.top - before!.top : null
    expect(after).not.toBeNull(); expect(Math.abs(after!.top - before!.top)).toBeLessThanOrEqual(2)
    expect(state.offset).toBe(50)
    m.nextPageHeld = !!state.pending
    expect((await snapshot(page)).ordinary).toBeLessThanOrEqual(30)
  })
})

test('the final history page retains the reading anchor when its load sentinel disappears', async ({ page }, info) => {
  await diagnose(page, info, async m => {
    const state = await gateway(page, 200)
    m.historyPages = state.pages
    await loadUntil(page, state, 50)
    expect(state.offset).toBe(50)
    state.hold = true
    await page.locator('.chat-thread').hover({ position: { x: 80, y: 120 } })
    await page.mouse.wheel(0, -1_000_000)
    await expect.poll(() => !!state.pending).toBe(true)
    await expect(page.getByTestId('history-load-sentinel')).toBeVisible()
    await settleLayout(page)
    const before = await rowAnchor(page)
    m.before = before
    expect(before).not.toBeNull()
    // The last 50 messages and the header's removal commit together.
    const releaseLastPage = state.pending!
    state.pending = null
    releaseLastPage()
    await expect(page.getByTestId('conversation-minimap-marker')).toHaveCount(100)
    await expect(page.getByTestId('history-load-sentinel')).toHaveCount(0)
    await settleLayout(page)
    const after = await rowAnchor(page, before!.key)
    m.after = after
    m.rowStartDriftPx = after ? after.top - before!.top : null
    expect(after).not.toBeNull()
    expect(after!.key).toBe(before!.key)
    expect(Math.abs(after!.top - before!.top)).toBeLessThanOrEqual(2)
    expect(state.offset).toBe(0)
    expect(state.pending).toBeNull()
    expect((await snapshot(page)).ordinary).toBeLessThanOrEqual(30)
  })
})

test('live-to-canonical handoff preserves the reading token inside a long answer', async ({ page }) => {
  test.setTimeout(30_000)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla.chat.virtualizeHistory', 'true')
  })
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  for (const endpoint of ['approvals', 'system/update', 'elevated-mode']) {
    await page.route(`**/api/${endpoint}`, route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ pending: [], enabled: false }),
    }))
  }

  const session = 'agent:main:webchat:e2e-terminal-handoff'
  const task = 'task-terminal-handoff'
  const generation = 'terminal-handoff-generation'
  let socket: WebSocketRoute | null = null
  let sequence = 0
  let running = true
  const history: Record<string, unknown>[] = Array.from({ length: 200 }, (_, index) => ({
    role: index % 2 ? 'assistant' : 'user',
    text: `Synthetic history ${index + 1}. ${'Windowed content. '.repeat(12)}`,
    id: `terminal-history-${index}`, message_id: `terminal-history-${index}`,
    timestamp: 1_800_000_000 + index,
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({
          policy: { concurrent_history_reads: true },
          auth: { principal: { isOwner: true } },
        }))
        return
      }
      const metadata = {
        current_stream_seq: sequence, stream_generation: generation,
        run_status: running ? 'running' : 'idle',
        active_task: running ? { task_id: task, status: 'running' } : null,
      }
      const payloads: Record<string, unknown> = {
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(session, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(session, {
          ...metadata, task_id: running ? task : null, events: [],
        }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(session, metadata),
        'chat.history': chatHistoryPayload(history),
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {} }))
    })
  })
  const send = (event: string, payload: Record<string, unknown>) => {
    if (!socket) throw new Error('Synthetic session socket is not connected')
    socket.send(JSON.stringify({ type: 'event', event, payload: {
      key: session, task_id: task, stream_generation: generation, stream_seq: ++sequence, ...payload,
    } }))
  }
  const answer = ('## Deterministic stream fixture\n\n```ts\nconst stable = true\n```\n\n'
    + '| A | B |\n|---|---|\n| 1 | 2 |\n\n'
    + Array.from({ length: 4000 }, (_, index) => (
      `incremental-${String(index).padStart(5, '0')} payload with safe markdown; `
    )).join('')).slice(0, 128 * 1024)

  await page.goto('/control/chat?session=' + encodeURIComponent(session))
  await expect(page.locator('.conn-pill.connected')).toBeVisible()
  await expect(page.locator('.chat-message-list')).toHaveAttribute('data-virtualized', 'true')
  send('session.event.text_delta', { text: answer.slice(0, 64 * 1024) })
  await expect(page.locator('.streaming-text-part')).toBeVisible()
  const thread = page.locator('.chat-thread')
  await expect.poll(() => thread.evaluate(el => (
    el.scrollHeight - el.scrollTop - el.clientHeight
  ))).toBeLessThanOrEqual(2)

  // Genuine upward input pauses follow before the second half grows below it.
  await thread.hover()
  await page.mouse.wheel(0, -400)
  await expect(page.locator('.chat-jump-latest')).toBeVisible()
  await page.waitForTimeout(150)
  const anchor = await page.evaluate(() => {
    const container = document.querySelector<HTMLElement>('.chat-thread')!
    const root = document.querySelector<HTMLElement>('.streaming-text-part')!
    const bounds = container.getBoundingClientRect()
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (const match of (node.textContent || '').matchAll(/incremental-\d{5}/g)) {
        const start = match.index ?? 0
        const range = document.createRange()
        range.setStart(node, start)
        range.setEnd(node, start + match[0].length)
        const rect = range.getBoundingClientRect()
        if (rect.bottom > bounds.top + 8 && rect.top < bounds.bottom - 8) {
          return { token: match[0], offset: rect.top - bounds.top }
        }
      }
    }
    throw new Error('Visible streaming text anchor missing')
  })
  send('session.event.text_delta', { text: answer.slice(64 * 1024) })
  await page.waitForTimeout(250)
  history.push({
    role: 'assistant', text: answer, id: 'terminal-final', message_id: 'terminal-final',
    timestamp: 1_800_000_201, turn_context: { turn_id: task },
  })
  running = false
  send('session.event.done', { status: 'succeeded', reason: 'completed', text_snapshot: answer })
  await expect(page.locator('.streaming-text-part')).toHaveCount(0)
  // The terminal projection owns its key until history reconciliation; the
  // invariant is the same text in a settled message row, not the fixture ID.
  const finalRow = page.locator('.chat-message-list__row').filter({ hasText: anchor.token })
  await expect(finalRow).toHaveCount(1)
  await expect(finalRow).toContainText(anchor.token)
  // Include the terminal row's measurements and late Markdown layout in the gate.
  await page.waitForTimeout(500)
  const after = await finalRow.evaluate((row, token) => {
    const container = document.querySelector<HTMLElement>('.chat-thread')!
    const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const start = (node.textContent || '').indexOf(token)
      if (start >= 0) {
        const range = document.createRange()
        range.setStart(node, start)
        range.setEnd(node, start + token.length)
        return range.getBoundingClientRect().top - container.getBoundingClientRect().top
      }
    }
    throw new Error('Canonical reading token was unmounted')
  }, anchor.token)
  expect(Math.abs(after - anchor.offset)).toBeLessThanOrEqual(2)
  await expect(page.locator('.chat-jump-latest')).toBeVisible()
})
