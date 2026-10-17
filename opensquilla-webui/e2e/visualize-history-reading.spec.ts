import { expect, test, type Page } from '@playwright/test'
import { createHash } from 'node:crypto'
import { MAIN, installGateway } from './support/real-conversation-gateway'

type ReadingSample = {
  phase: string
  top: number | null
  width: number | null
  sameCard: boolean
  sameRow: boolean
  failed: boolean
  drafted: boolean
  loading: boolean
  frames: number
  virtual: string | null
}
type ReadingProbe = { phase: string; stopped: boolean; samples: ReadingSample[] }
type ProbeWindow = Window & { readingProbe?: ReadingProbe }

async function frames(page: Page, count = 8) {
  await page.evaluate(count => new Promise<void>(resolve => {
    const tick = () => { if (--count <= 0) resolve(); else requestAnimationFrame(tick) }
    requestAnimationFrame(tick)
  }), count)
}

test('keeps a failed historical canvas anchored through the next turn and workbench reflow', async ({ page }, info) => {
  // The real shared ChatView/ChatMessageList and sandbox run against a synthetic
  // Gateway. A short, nonvirtualized transcript reproduces the width-reflow
  // regression without keeping background conversation components alive.
  const gateway = await installGateway(page)
  const html = '<!doctype html><html><body><h1>Old flight game</h1><script>const particles = undefined; for (const particle of particles) {}</script></body></html>'
  const receipt = { previewStatus: 'inline-ready', presentation: 'inline', documentId: 'doc_old_flight',
    revisionId: 'rev_old_flight', artifactId: 'artifact_old_flight', name: 'flight-game.html', mime: 'text/html',
    size: Buffer.byteLength(html), sha256: createHash('sha256').update(html).digest('hex'),
    marker: 'visualize:doc_old_flight:rev_old_flight' }
  let oldReads = 0
  await page.route('**/api/v1/artifact-documents/*?**', route => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/doc_old_flight')) {
      oldReads++
      expect(url.searchParams.get('revisionId')).toBe(receipt.revisionId)
      return route.fulfill({ contentType: 'text/html', body: html })
    }
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><h1>Workbench preview</h1>' })
  })
  for (let index = 0; index < 14; index++) {
    gateway.appendHistory(MAIN, { id: `old-user-${index}`, role: 'user', text: `Earlier question ${index}` })
    gateway.appendHistory(MAIN, { id: `old-answer-${index}`, role: 'assistant', text: `Earlier answer ${index}.` })
  }
  gateway.appendHistory(MAIN, { id: 'old-flight-request', role: 'user', text: 'Show the flight game.' })
  gateway.appendHistory(MAIN, { id: 'old-flight-answer', message_id: 'old-flight-answer', role: 'assistant',
    text: `Old failed canvas.\n\n\`${receipt.marker}\`\n\nThis is the immutable old result.`,
    tool_calls: [{ type: 'tool_use', tool_use_id: 'old-flight-preview', name: 'open_workspace_preview',
      input: { path: 'flight-game.html', presentation: 'inline' }, result: JSON.stringify(receipt),
      execution_status: { status: 'success' } }] })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
  const host = page.locator('#app-main .chat').first()
  const card = host.locator('[data-visualize-canvas][aria-label="flight-game.html"]')
  const editor = host.getByRole('textbox', { name: 'Message to send', exact: true })
  await expect(card.locator('.visualize-error')).toContainText('particles is not iterable')
  await card.getByRole('button', { name: 'Ask to repair', exact: true }).click()
  await expect(card.getByRole('status')).toContainText('Added to the composer')
  await expect(editor).toContainText(receipt.revisionId)
  const readsAfterDrafting = oldReads
  expect(readsAfterDrafting).toBeGreaterThan(0)
  await host.getByRole('button', { name: 'Send', exact: true }).click()
  await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
  gateway.emit(MAIN, 'session.event.text_delta', { text: 'Starting repair investigation. ', presentation: 'intermediate' })
  await expect(host.locator('.conversation-live-edge')).toContainText('Starting repair investigation.')
  await editor.fill('Keep my next question. 保留草稿。')
  await card.hover()
  await page.mouse.wheel(0, -200)
  await expect(host.locator('.chat-thread')).toHaveClass(/chat-thread--reading-history/)
  await expect(card).toBeInViewport()
  await frames(page)
  const historyReads = () => gateway.requests.filter(request => request.method === 'chat.history').length
  const historyReadsBefore = historyReads()

  // Observe every animation frame, including intermediate layout frames. A
  // final boundingBox assertion misses the old excursion offscreen and back.
  await card.evaluate(element => {
    const row = element.closest('[data-chat-message-key]')
    const state: ReadingProbe = { phase: 'reading', stopped: false, samples: [] }
    ;(window as ProbeWindow).readingProbe = state
    const tick = () => {
      const current = document.querySelector('[data-visualize-canvas][aria-label="flight-game.html"]')
      const box = current?.getBoundingClientRect()
      state.samples.push({ phase: state.phase, top: box?.top ?? null, width: box?.width ?? null,
        sameCard: current === element && element.isConnected,
        sameRow: current?.closest('[data-chat-message-key]') === row,
        failed: Boolean(current?.querySelector('.visualize-error[data-error-kind="runtime"]')),
        drafted: Boolean(current?.textContent?.includes('Added to the composer')),
        loading: Boolean(current?.querySelector('.visualize-loading')),
        frames: current?.querySelectorAll('iframe').length ?? -1,
        virtual: current?.closest('[data-virtualized]')?.getAttribute('data-virtualized') ?? null })
      if (!state.stopped) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
  const phase = (value: string) => page.evaluate(value => { (window as ProbeWindow).readingProbe!.phase = value }, value)
  let samples: ReadingSample[] = []
  try {
    await frames(page)
    for (let index = 0; index < 3; index++) {
      await phase(`stream-${index}`)
      gateway.emit(MAIN, 'session.event.text_delta', { text: `Repair commentary ${index}. `, presentation: 'intermediate' })
      gateway.emit(MAIN, 'session.event.tool_use_start', { tool_use_id: `repair-read-${index}`, tool_name: 'read_file', input: { path: 'flight-game.html' } })
      gateway.emit(MAIN, 'session.event.tool_result', { tool_use_id: `repair-read-${index}`, tool_name: 'read_file', name: 'read_file', result: `read ${index}`, execution_status: { status: 'success' } })
      gateway.emit(MAIN, 'session.event.text_delta', { text: `Reply fragment ${index}. `, presentation: 'answer' })
      await expect(host.locator('.conversation-live-edge')).toContainText(`Reply fragment ${index}.`)
      gateway.emit(MAIN, 'sessions.changed', { reason: 'history_appended' })
      await frames(page)
    }
    await phase('open-workbench')
    gateway.registerDocument(MAIN, 'doc_workbench', 'fixed-flight.html')
    gateway.emit(MAIN, 'session.event.tool_use_start', { tool_use_id: 'workbench-preview', tool_name: 'open_workspace_preview', input: { path: 'fixed-flight.html' } })
    gateway.emit(MAIN, 'session.event.tool_result', { tool_use_id: 'workbench-preview', name: 'open_workspace_preview',
      result: JSON.stringify({ documentId: 'doc_workbench', resourceId: 'document:doc_workbench', entrypoint: '/task/fixed-flight.html', previewStatus: 'ready' }) })
    await expect(page.getByRole('tab', { name: 'fixed-flight.html', exact: true })).toHaveAttribute('aria-selected', 'true')
    await frames(page)
    await phase('close-workbench')
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(page.getByTestId('workbench-host')).not.toBeVisible()
    await frames(page)
    await phase('terminal-history')
    gateway.finish(MAIN, 'Repair investigation complete; old canvas not republished.')
    await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
    await expect(host).toContainText('Repair investigation complete; old canvas not republished.')
    // History signals can coalesce; require a real read, not one per signal.
    await expect.poll(historyReads).toBeGreaterThan(historyReadsBefore)
    await frames(page)
    await expect(editor).toHaveText('Keep my next question. 保留草稿。')
    await expect(card.getByRole('status')).toContainText('Added to the composer')
    expect(oldReads).toBe(readsAfterDrafting)
    expect(gateway.errors.filter(error => !error.includes('particles is not iterable'))).toEqual([])
  } finally {
    samples = await page.evaluate(() => {
      const state = (window as ProbeWindow).readingProbe!
      state.stopped = true
      return state.samples
    })
    await info.attach('historical-canvas-frames', {
      contentType: 'application/json', body: JSON.stringify({ samples, oldReads, readsAfterDrafting }, null, 2),
    })
  }
  for (const phase of ['reading', 'stream-0', 'stream-1', 'stream-2', 'open-workbench', 'close-workbench', 'terminal-history']) {
    expect(samples.filter(sample => sample.phase === phase).length, `Observed frames for ${phase}`).toBeGreaterThanOrEqual(2)
  }
  expect(samples.every(sample => sample.sameCard && sample.sameRow && sample.failed && sample.drafted
    && !sample.loading && sample.frames === 0 && sample.virtual === 'false')).toBe(true)
  const initial = samples[0]!
  expect(samples.some(sample => sample.phase === 'open-workbench' && sample.width! < initial.width! - 100)).toBe(true)
  expect(samples.some(sample => sample.phase === 'close-workbench' && Math.abs(sample.width! - initial.width!) < 2)).toBe(true)
  // Width-dependent line wrapping can shift the chosen text anchor a few px;
  // it must never expose the hundreds-of-pixels seek through estimated sizes.
  const maximumExcursion = Math.max(...samples.map(sample => Math.abs(sample.top! - initial.top!)))
  expect(maximumExcursion, 'The old failed card must stay at its reading position on every frame').toBeLessThanOrEqual(16)
})
