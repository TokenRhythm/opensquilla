import { expect, test, type Page } from '@playwright/test'
import { installSidebarFixture } from './support/sidebar-fixture'
import { expectComposerValue } from './utils'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

async function toolbarGeometry(page: Page, selectors: string[]) {
  const geometry = await page.locator('.chat-input-footer').evaluate((footer, selectors) => {
    const bounds = footer.getBoundingClientRect()
    return selectors.map(selector => {
      const button = footer.querySelector<HTMLElement>(selector)!
      const rect = button.getBoundingClientRect()
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
      return {
        name: selector, left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
        centerHit: Boolean(hit && button.contains(hit)),
        insideFooter: rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1,
      }
    })
  }, selectors)
  for (const [index, box] of geometry.entries()) {
    expect(box.centerHit, JSON.stringify(box)).toBe(true)
    expect(box.insideFooter, JSON.stringify(box)).toBe(true)
    for (const other of geometry.slice(index + 1)) {
      const overlapX = Math.min(box.right, other.right) - Math.max(box.left, other.left)
      const overlapY = Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top)
      expect(overlapX > 1 && overlapY > 1, `${box.name} overlaps ${other.name}`).toBe(false)
    }
  }
  return geometry
}

const SPLIT_SESSION = 'agent:main:webchat:composer-split'
async function openSplitComposer(page: Page, theme: string) {
  let running = false
  let sends = 0
  let aborts = 0
  const routing = { key: SPLIT_SESSION, mode: 'direct', revision: 0,
    modelSelection: { provider: 'synthetic', model: 'Kimi K2.6' } }
  const metadata = () => ({
    routing, run_status: running ? 'running' : 'idle',
    active_task: running ? { task_id: 'composer-task', status: 'running' } : null,
    last_task: aborts ? { task_id: 'composer-task', status: 'cancelled' } : null,
    tasks: running ? [{ task_id: 'composer-task', status: 'running' }] : [],
  })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/v1/artifacts/composer-notes', route => route.fulfill({
    contentType: 'text/plain', body: 'Synthetic workbench document. No real provider is called.',
  }))
  await installSidebarFixture(page, {
    'sessions.routing.get': routing,
    'sessions.routing.set': routing,
    'models.routing.get': { mode: 'direct' },
    'sessions.resolve': { key: SPLIT_SESSION, model: 'Kimi K2.6' },
    'sessions.list': { sessions: [{ key: SPLIT_SESSION, title: 'Composer split acceptance',
      sessionKind: 'chat', surface: 'webchat', messageCount: 2, runStatus: 'idle' }],
    count: 1, ts: 1_800_000_000, has_more: false },
    'chat.history': () => chatHistoryPayload([
      { role: 'user', text: 'Check the composer alongside the workbench.', id: 'composer-user' },
      { role: 'assistant', text: 'The document is ready for review.', id: 'composer-answer', artifacts: [{
        id: 'composer-notes', name: 'notes.txt', mime: 'text/plain', size: 64,
        download_url: '/api/v1/artifacts/composer-notes',
      }] },
    ]),
    'sessions.messages.subscribe': () => sessionMessagesSubscribePayload(SPLIT_SESSION, metadata()),
    'sessions.messages.snapshot': () => sessionMessagesSnapshotPayload(SPLIT_SESSION, metadata()),
    'sessions.messages.hydrate': () => sessionMessagesHydratePayload(SPLIT_SESSION, metadata()),
    'onboarding.status': { llmConfigured: true, hasConfig: true, audioConfigured: false },
    'chat.send': () => {
      sends += 1
      running = true
      return { accepted: true, session: SPLIT_SESSION, task_id: 'composer-task' }
    },
    'chat.abort': () => { aborts += 1; running = false; return { aborted: true, key: SPLIT_SESSION } },
  })
  await page.addInitScript(theme => {
    localStorage.setItem('opensquilla-theme', theme)
    localStorage.setItem('opensquilla.sidebar.width.v1', JSON.stringify({ version: 1, width: 286, source: 'custom' }))
    localStorage.setItem('opensquilla.workbench.width.v1', JSON.stringify({ version: 1, width: 936, source: 'user' }))
    window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
  }, theme)
  await page.goto(`/control/chat?session=${encodeURIComponent(SPLIT_SESSION)}`)
  await expect(page.locator('.chat-model-routing-btn')).toContainText('Kimi K2.6')
  await page.locator('.msg-artifact-chip').getByRole('button', { name: 'Open notes.txt', exact: true }).click()
  await expect(page.getByTestId('workbench-host')).toBeVisible()
  await expect(page.locator('.artifact-preview__text')).toContainText('Synthetic workbench document')
  // The document reveal can choose its initial split width. Exercise the real
  // divider to keep this test's deliberately constrained 520px composer.
  await expect.poll(() => page.getByTestId('workbench-host').evaluate(host => (
    Math.abs(host.getBoundingClientRect().width - parseFloat((host as HTMLElement).style.getPropertyValue('--workbench-width')))
  ))).toBeLessThanOrEqual(1)
  const panelWidth = (await page.locator('.chat-input-panel').boundingBox())!.width
  const divider = page.getByRole('separator', { name: 'Resize workbench', exact: true })
  const dividerBox = (await divider.boundingBox())!
  const dividerX = dividerBox.x + dividerBox.width / 2
  const dividerY = dividerBox.y + dividerBox.height / 2
  await page.mouse.move(dividerX, dividerY)
  await page.mouse.down()
  await page.mouse.move(dividerX - (panelWidth - 520), dividerY, { steps: 8 })
  await page.mouse.up()
  await expect.poll(async () => Math.round((await page.locator('.chat-input-panel').boundingBox())!.width)).toBe(520)
  return { sends: () => sends, aborts: () => aborts }
}

// Real App/ChatView/ChatComposer with an isolated, synthetic Gateway. These
// checks never send a task or read/write the installed desktop profile.
async function openComposer(page: Page, locale = 'en') {
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await installSidebarFixture(page, {
    'sessions.routing.get': { mode: 'router', revision: 0 },
    'sessions.routing.set': { mode: 'router', revision: 1 },
    'models.routing.get': { mode: 'router' },
    'workspaces.list': { workspaces: [] },
    'workspaces.open': {},
  })
  await page.addInitScript(locale => {
    localStorage.setItem('opensquilla-locale', locale)
    localStorage.setItem('opensquilla-theme', 'miami')
  }, locale)
  await page.goto('/control/chat/new')
  await expect(page.locator('.chat-textarea')).toBeVisible()
  await expect(page.locator('.chat-run-mode-btn')).toBeEnabled()
  await expect(page.locator('.chat-model-routing-btn')).toBeVisible()
  await expect(page.locator('.chat-model-routing-btn')).toHaveClass(/chat-model-routing-btn--squilla_router/)
}

test('ordinary Escape preserves the displayed and persisted draft across reload', async ({ page }) => {
  await openComposer(page)
  const textarea = page.locator('.chat-textarea')
  const draft = 'Keep this unfinished task. 保留 Miami 配色。'
  await textarea.fill(draft)
  await expect.poll(() => page.evaluate(() => {
    const key = localStorage.getItem('opensquilla.chat.recent-draft-session')
    return key ? localStorage.getItem(`opensquilla.chat.draft:${key}`) : null
  })).toBe(draft)

  await textarea.press('Escape')
  await expectComposerValue(textarea, draft)
  await page.reload()
  await expectComposerValue(textarea, draft)

  await page.locator('.chat-run-mode-btn').click()
  await expect(page.locator('.composer-run-mode')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('.composer-run-mode')).toHaveCount(0)
  await expectComposerValue(textarea, draft)
})

for (const { width, locale } of [
  { width: 360, locale: 'en' }, { width: 487, locale: 'en' },
  { width: 535, locale: 'en' }, { width: 1440, locale: 'en' },
  { width: 487, locale: 'zh-Hans' }, { width: 487, locale: 'de' },
]) {
  test(`ordinary composer keeps separate hit targets and usable popovers at ${width}px in ${locale}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 940 })
    await openComposer(page, locale)
    await page.locator('.chat-textarea').fill('Check every composer control without sending.')
    const controls = ['.chat-plus-btn', '.chat-project-choose', '.chat-run-mode-btn', '.chat-model-routing-btn', '.chat-send-btn']
    const geometry = await page.locator('.chat-input-footer').evaluate((footer, selectors) => {
      const buttons = selectors.map(selector => footer.querySelector<HTMLElement>(selector)).filter((element): element is HTMLElement => Boolean(element))
      return buttons.map(button => {
        const rect = button.getBoundingClientRect()
        const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
        return {
          name: button.className, left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
          centerHit: Boolean(hit && button.contains(hit)),
        }
      })
    }, controls)
    expect(geometry).toHaveLength(5)
    for (const [index, box] of geometry.entries()) {
      expect(box.centerHit, JSON.stringify(box)).toBe(true)
      expect(box.left).toBeGreaterThanOrEqual(0)
      expect(box.right).toBeLessThanOrEqual(width)
      for (const other of geometry.slice(index + 1)) {
        const overlapX = Math.min(box.right, other.right) - Math.max(box.left, other.left)
        const overlapY = Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top)
        expect(overlapX > 1 && overlapY > 1, `${box.name} overlaps ${other.name}`).toBe(false)
      }
    }
    const permission = geometry.find(box => box.name.includes('chat-run-mode-btn'))!
    const model = geometry.find(box => box.name.includes('chat-model-routing-btn'))!
    // A narrow viewport alone must not force a second row: only the available
    // composer width and the actual controls determine whether they fit.
    if (width === 1440) expect(Math.abs(permission.top - model.top)).toBeLessThanOrEqual(5)

    await page.locator('.chat-run-mode-btn').click()
    await expect(page.locator('.composer-run-mode')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.composer-run-mode')).toHaveCount(0)
    await page.locator('.chat-plus-btn').click()
    const menu = page.locator('.composer-add-menu')
    await expect(menu).toBeVisible()
    const menuBox = await menu.boundingBox()
    expect(menuBox!.x).toBeGreaterThanOrEqual(0)
    expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(width)
    await page.locator('.chat-plus-btn').click()
    await expect(menu).toHaveCount(0)
    await page.locator('.chat-model-routing-btn').click()
    await expect(page.locator('.composer-model-routing')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.composer-model-routing')).toHaveCount(0)
    await page.locator('.chat-input-panel').screenshot({ path: testInfo.outputPath(`composer-miami-${width}-${locale}.png`) })
  })
}

for (const theme of ['dark', 'light', 'miami']) {
  test(`split chat keeps short model and Send/Stop on one row in ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1800, height: 1083 })
    const fixture = await openSplitComposer(page, theme)
    const panel = page.locator('.chat-input-panel')
    const panelBox = (await panel.boundingBox())!
    expect(panelBox.width).toBeGreaterThanOrEqual(500)
    expect(panelBox.width).toBeLessThanOrEqual(540)

    await page.locator('.chat-textarea').fill('Continue the synthetic review.')
    await expect(page.locator('.chat-send-btn')).toBeEnabled()
    const selectors = ['.chat-plus-btn', '.chat-run-mode-btn', '.chat-model-routing-btn']
    const before = await toolbarGeometry(page, [...selectors, '.chat-send-btn'])
    const centers = before.map(box => (box.top + box.bottom) / 2)
    expect(Math.max(...centers) - Math.min(...centers)).toBeLessThanOrEqual(5)
    const footerHeight = (await page.locator('.chat-input-footer').boundingBox())!.height
    await page.screenshot({ path: testInfo.outputPath(`split-${theme}-send.png`) })
    await page.locator('.chat-send-btn').click()
    await expect.poll(fixture.sends).toBe(1)
    await expect(page.locator('.chat-stop-btn')).toBeVisible()
    const active = await toolbarGeometry(page, [...selectors, '.chat-stop-btn'])
    const activeCenters = active.map(box => (box.top + box.bottom) / 2)
    expect(Math.max(...activeCenters) - Math.min(...activeCenters)).toBeLessThanOrEqual(5)
    // Stop is intentionally 8px taller than Send; neither state adds a row.
    expect(Math.abs((await page.locator('.chat-input-footer').boundingBox())!.height - footerHeight)).toBeLessThanOrEqual(10)
    await testInfo.attach('toolbar-geometry', {
      body: JSON.stringify({ panel: panelBox, idle: before, running: active }, null, 2),
      contentType: 'application/json',
    })
    await page.screenshot({ path: testInfo.outputPath(`split-${theme}-stop.png`) })
    await page.locator('.chat-stop-btn').click()
    await expect.poll(fixture.aborts).toBe(1)
  })
}

test('520px new-task composer compacts longer controls without losing the right-edge action', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 568, height: 940 })
  await openComposer(page)
  // Use the real responsive layout to match the split-chat composer width;
  // this scenario additionally has Choose project and the long router label.
  const initialWidth = (await page.locator('.chat-input-panel').boundingBox())!.width
  await page.setViewportSize({ width: Math.round(568 + 520 - initialWidth), height: 940 })
  await expect.poll(async () => Math.round((await page.locator('.chat-input-panel').boundingBox())!.width)).toBe(520)
  await page.locator('.chat-textarea').fill('Check compact toolbar without sending.')
  const geometry = await toolbarGeometry(page, [
    '.chat-plus-btn', '.chat-project-choose', '.chat-run-mode-btn',
    '.chat-model-routing-btn', '.chat-send-btn',
  ])
  const permission = geometry.find(box => box.name === '.chat-run-mode-btn')!
  const model = geometry.find(box => box.name === '.chat-model-routing-btn')!
  const send = geometry.find(box => box.name === '.chat-send-btn')!
  await expect(page.locator('.chat-input-footer')).toHaveAttribute('data-composer-density', 'compact')
  expect(Math.abs((model.top + model.bottom) / 2 - (permission.top + permission.bottom) / 2)).toBeLessThanOrEqual(5)
  const rightEdge = await page.locator('.chat-input-footer').evaluate(footer => (
    footer.getBoundingClientRect().right - parseFloat(getComputedStyle(footer).paddingRight)
  ))
  expect(Math.abs(send.right - rightEdge)).toBeLessThanOrEqual(2)
  await page.screenshot({ path: testInfo.outputPath('new-task-520-compact.png') })
})
