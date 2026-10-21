import { expect, test, type Locator, type Page } from '@playwright/test'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { installGateway, logs, MAIN, SIDE } from './support/real-conversation-gateway'

// Real App -> WorkbenchHost -> actual panel providers. Only Gateway I/O is
// synthetic; layout is measured from browser DOM, never reconstructed in a stub.
const main = (page: Page) => page.locator('.app-chat-pane .chat').first()
const side = (page: Page) => page.getByTestId('assistant-chat-panel')
const editor = (host: Locator) => host.locator('.chat-inline-editor')
const evidenceDirectory = new URL('../../../responsive-layout-review/', import.meta.url)
async function screenshot(page: Page, name: string) {
  await mkdir(evidenceDirectory, { recursive: true })
  await page.screenshot({ path: fileURLToPath(new URL(name, evidenceDirectory)), fullPage: true })
}
const themesRoot = new URL('../src/themes/', import.meta.url)
const themes = readdirSync(themesRoot, { withFileTypes: true }).filter(item => item.isDirectory()).flatMap(item => {
  const manifest = new URL(`${item.name}/manifest.ts`, themesRoot)
  if (!existsSync(manifest)) return []
  const source = readFileSync(manifest, 'utf8')
  return /kind:\s*['"]value['"]/.test(source) ? [source.match(/\bid:\s*['"]([^'"]+)['"]/)![1]] : []
})

test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    const path = info.outputPath('layout-gateway.json')
    await writeFile(path, JSON.stringify({ ...logs.get(page), geometry: await geometry(page).catch(() => null) }, null, 2))
    await info.attach('layout-gateway', { path, contentType: 'application/json' })
  }
})

async function prepare(page: Page, theme = 'light', locale = 'en') {
  await page.setViewportSize({ width: 1600, height: 950 })
  const gateway = await installGateway(page, false, false, false, false, { workspace: true, terminal: true, locale,
    ...(locale === 'de' ? { modelName: 'synthetic/extremely-long-model-name-with-a-detailed-release-description-2026' } : {}),
  })
  gateway.appendHistory(MAIN, { role: 'assistant', id: 'layout-files', text: '[HTML document](demo.html) and [Code file](demo.ts)' })
  await page.addInitScript(value => { if (window === window.top) localStorage.setItem('opensquilla-theme', value) }, theme)
  await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
  await expect(main(page)).toContainText('Earlier main answer')
  await expect(page.locator('.sidebar-brand-mark')).toBeVisible()
  await expect.poll(() => page.locator('.sidebar-brand-mark').evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
  return gateway
}

async function showSidebar(page: Page, open: boolean) {
  const collapse = page.getByTestId('sidebar-toggle-expanded')
  const expand = page.getByTestId('sidebar-toggle-collapsed')
  // A translated, inert navigation still satisfies Playwright's isVisible().
  // Read its actual expansion contract after the resize observer has run.
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  const expanded = await collapse.getAttribute('aria-expanded') === 'true'
  if (open !== expanded) await (open ? expand : collapse).click()
  await expect(collapse).toHaveAttribute('aria-expanded', String(open))
  if (open) await expect(page.locator('#sidebar-nav')).not.toHaveAttribute('inert', '')
  else await expect(page.locator('#sidebar-nav')).toHaveAttribute('inert', '')
}

async function openPanel(page: Page, kind: 'assistant-chat' | 'workspace-files' | 'terminal' | 'html' | 'code') {
  if (kind === 'html' || kind === 'code') {
    await main(page).getByRole('link', { name: `Open demo.${kind === 'html' ? 'html' : 'ts'}`, exact: true }).click()
    if (kind === 'html') await expect(page.getByTestId('workspace-file-panel').frameLocator('iframe').getByRole('heading', { name: 'Parity HTML document' })).toBeVisible()
    else await expect(page.getByTestId('workspace-file-panel')).toContainText('export const parity')
  } else {
    if (!await page.getByTestId('workbench-host').isVisible()) await page.getByTestId('topbar-workbench-toggle').click()
    const button = page.locator(`.app-workbench__new-panel-option[data-panel-kind="${kind}"], .app-workbench__new-panel-card[data-panel-kind="${kind}"]`)
    if (!await button.isVisible()) await page.getByTestId('workbench-new-panel-trigger').click()
    await page.locator(`[data-panel-kind="${kind}"]`).filter({ visible: true }).click()
    if (kind === 'assistant-chat') await expect(editor(side(page))).toBeVisible()
    if (kind === 'workspace-files') await expect(page.getByTestId('workspace-files-panel')).toBeVisible()
    if (kind === 'terminal') await expect(page.getByTestId('terminal-panel')).toContainText('Connected')
  }
}

async function geometry(page: Page) {
  return page.evaluate(() => {
    const workspace = document.querySelector<HTMLElement>('.app-workspace')!
    const pane = workspace.querySelector<HTMLElement>('.app-chat-pane')!
    const host = document.querySelector<HTMLElement>('[data-testid="workbench-host"]')!
    const rect = (element: HTMLElement) => {
      const value = element.getBoundingClientRect()
      return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height }
    }
    return { workspace: rect(workspace), pane: rect(pane), host: rect(host),
      hidden: getComputedStyle(pane).display === 'none', inert: pane.inert, ariaHidden: pane.getAttribute('aria-hidden'),
      hostHidden: getComputedStyle(host).display === 'none', hostInert: host.inert, hostAriaHidden: host.getAttribute('aria-hidden'),
      mode: host.dataset.layoutMode, appMode: workspace.dataset.workbenchLayout,
      viewport: { width: innerWidth, height: innerHeight }, scrollWidth: document.documentElement.scrollWidth }
  })
}

async function expectLayout(page: Page, expectedMode?: 'split' | 'main-only' | 'single-pane') {
  // Automatic width changes prioritize the main conversation. Only an explicit
  // user open/maximize may present the right pane alone on a narrow viewport.
  await expect(page.getByTestId('workbench-host')).toBeAttached()
  await expect.poll(async () => {
    const g = await geometry(page)
    const mode = expectedMode ?? (g.workspace.width < 841 ? 'main-only' : 'split')
    const inWorkspace = g.host.left >= g.workspace.left - 1 && g.host.right <= g.workspace.right + 1
      && g.host.bottom <= g.viewport.height + 1 && g.host.top >= g.workspace.top - 1
    const geometryOK = mode === 'main-only'
      ? !g.hidden && !g.inert && g.hostHidden && g.hostInert && g.hostAriaHidden === 'true'
        && Math.abs(g.pane.width - g.workspace.width) <= 1
      : inWorkspace && !g.hostHidden && !g.hostInert && (mode === 'single-pane'
        ? g.hidden && g.inert && g.ariaHidden === 'true' && Math.abs(g.host.width - g.workspace.width) <= 1
        : !g.hidden && !g.inert && g.pane.width >= 479 && g.host.width >= 359 && g.host.left >= g.pane.right - 1)
    return geometryOK && g.mode === mode && g.appMode === mode && g.scrollWidth <= g.viewport.width + 1
  }, { message: 'Automatic shrink must keep main usable; explicit right-pane views must stay within the workspace' }).toBe(true)
  const g = await geometry(page)
  expect(g.mode).toBe(expectedMode ?? (g.workspace.width < 841 ? 'main-only' : 'split'))
  expect(g.appMode).toBe(g.mode)
  const toggle = page.getByTestId('topbar-workbench-toggle')
  await expect(toggle).toHaveCount(1)
  const toggleHost = g.mode === 'main-only' ? page.locator('.topbar')
    : page.locator('.workbench-host__chrome')
  await expect(toggleHost.getByTestId('topbar-workbench-toggle')).toBeVisible()
  if (g.mode !== 'main-only') {
    const toggleBounds = (await toggle.boundingBox())!
    expect(g.host.right - (toggleBounds.x + toggleBounds.width)).toBeLessThanOrEqual(16)
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const close = page.locator('.workbench-host__tab-wrap.is-active .workbench-host__tab-close')
    if (await close.count()) {
      const box = (await close.boundingBox())!
      expect(box.width).toBe(24)
      expect(box.height).toBe(24)
    }
  }
  if (g.mode === 'main-only') await expect(page.getByTestId('topbar-workbench-toggle')).toHaveAttribute('aria-expanded', 'false')
  return g
}

async function setAvailableWidth(page: Page, target: number) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const workspace = await page.locator('.app-workspace').boundingBox()
    const viewport = page.viewportSize()!
    if (workspace && Math.abs(workspace.width - target) <= 1) return
    await page.setViewportSize({ width: Math.max(320, Math.round(viewport.width + target - (workspace?.width ?? viewport.width))), height: 950 })
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  }
  expect((await page.locator('.app-workspace').boundingBox())!.width).toBeCloseTo(target, 0)
}

async function expectInsideViewport(locator: Locator, page: Page) {
  await expect(locator).toBeVisible()
  await expect.poll(async () => {
    const bounds = await locator.boundingBox(), viewport = page.viewportSize()!
    return Boolean(bounds && bounds.x >= -1 && bounds.y >= -1
      && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1)
  }).toBe(true)
}

async function expectComposerControls(host: Locator) {
  const panel = host.locator('.chat-input-panel')
  await expect(panel).toBeVisible()
  const controls = await panel.evaluate(element => {
    const bounds = element.getBoundingClientRect()
    const buttons = [...element.querySelectorAll<HTMLElement>('.chat-input-footer button')]
      .filter(button => button.getClientRects().length > 0)
      .map(button => ({ rect: button.getBoundingClientRect().toJSON(), name: button.getAttribute('aria-label') || button.title || button.textContent }))
    return { bounds: bounds.toJSON(), buttons }
  })
  expect(controls.buttons.length).toBeGreaterThanOrEqual(4)
  for (const { rect, name } of controls.buttons) {
    expect(name).toBeTruthy()
    expect(rect.left).toBeGreaterThanOrEqual(controls.bounds.left - 1)
    expect(rect.right).toBeLessThanOrEqual(controls.bounds.right + 1)
    expect(rect.width).toBeGreaterThanOrEqual(28)
  }
  for (let i = 0; i < controls.buttons.length; i++) for (const other of controls.buttons.slice(i + 1)) {
    const rect = controls.buttons[i].rect
    expect(Math.min(rect.right, other.rect.right) - Math.max(rect.left, other.rect.left)).toBeLessThanOrEqual(1)
  }
  if (controls.bounds.width <= 480) {
    await expect(host.locator('.chat-input-footer')).toHaveAttribute('data-composer-density', 'icons')
    for (const selector of ['.chat-run-mode-btn__label', '.chat-model-routing-btn__label', '.chat-model-routing-btn__chevron', '.chat-model-routing-btn__default', '.chat-model-routing-btn__new']) {
      await expect(host.locator(selector)).not.toBeVisible()
    }
    const icon = await host.locator('.chat-model-routing-btn > .icon').first().boundingBox()
    expect(icon?.width).toBeGreaterThanOrEqual(16)
    expect(icon?.height).toBeGreaterThanOrEqual(16)
  }
}

async function sampleTransition(page: Page, action: () => Promise<unknown>) {
  await page.evaluate(() => {
    const samples: Array<Record<string, number | boolean>> = []
    ;(window as any).__layoutFrames = new Promise(resolve => {
      const start = performance.now()
      const read = () => {
        const pane = document.querySelector<HTMLElement>('.app-chat-pane')!
        const host = document.querySelector<HTMLElement>('[data-testid="workbench-host"]')
        const workspace = document.querySelector<HTMLElement>('.app-workspace')!.getBoundingClientRect()
        const a = pane.getBoundingClientRect(), b = host?.getBoundingClientRect()
        const toggle = document.querySelector<HTMLElement>('[data-testid="topbar-workbench-toggle"]')!
        const toggleRect = toggle.getBoundingClientRect()
        samples.push({ time: performance.now() - start, paneVisible: getComputedStyle(pane).display !== 'none',
          paneLeft: a.left, paneRight: a.right, paneWidth: a.width,
          hostVisible: Boolean(host && getComputedStyle(host).display !== 'none'), leaving: Boolean(host?.classList.contains('workbench-presence-leave-active')),
          hostLeft: b?.left ?? 0, hostRight: b?.right ?? 0, hostWidth: b?.width ?? 0,
          workspaceLeft: workspace.left, workspaceRight: workspace.right, scrollWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth,
          toggleInPanel: Boolean(toggle.closest('[data-testid="workbench-host"]')),
          toggleRight: toggleRect.right, toggleLeft: toggleRect.left })
        if (performance.now() - start < 450) requestAnimationFrame(read)
        else resolve(samples)
      }
      requestAnimationFrame(read)
    })
  })
  await action()
  const samples = await page.evaluate(() => (window as any).__layoutFrames) as Array<Record<string, number | boolean>>
  expect(samples.length).toBeGreaterThan(3)
  for (const sample of samples) {
    if (sample.hostVisible && sample.hostWidth > 1) {
      expect(sample.hostLeft, JSON.stringify(sample)).toBeGreaterThanOrEqual(Number(sample.workspaceLeft) - 1)
      expect(sample.hostRight, JSON.stringify(sample)).toBeLessThanOrEqual(Number(sample.workspaceRight) + 1)
      if (sample.paneVisible && sample.paneWidth > 1) expect(sample.hostLeft, JSON.stringify(sample)).toBeGreaterThanOrEqual(Number(sample.paneRight) - 1)
    }
    expect(sample.scrollWidth).toBeLessThanOrEqual(Number(sample.viewportWidth) + 1)
    if (sample.toggleInPanel && Number(sample.hostWidth) >= 48) {
      expect(sample.toggleRight).toBeLessThanOrEqual(Number(sample.hostRight) + 1)
      expect(Number(sample.hostRight) - Number(sample.toggleRight)).toBeLessThanOrEqual(16)
    }
  }
  return samples
}

function expectTrackMotion(samples: Array<Record<string, number | boolean>>, from: number, to: number) {
  const intermediateWidths = samples.filter(frame => frame.hostVisible)
    .map(frame => Number(frame.hostWidth))
    .filter(width => width > Math.min(from, to) + 2 && width < Math.max(from, to) - 2)
  expect(new Set(intermediateWidths.map(width => Math.round(width))).size,
    'The workbench track must have intermediate widths, not jump between its endpoints').toBeGreaterThan(1)
}

test.describe('actual App responsive workbench layout', () => {
  test('moves the single pane toggle with its header and preserves keyboard focus and side state', async ({ page }) => {
    const gateway = await prepare(page)
    const artifact = gateway.registerDocument(MAIN, 'doc_toggle_focus', 'focus.html')
    gateway.appendHistory(MAIN, { role: 'assistant', id: 'toggle-focus-artifact', text: 'A completed deliverable.', artifacts: [artifact] })
    await page.reload()
    await expect(page.getByTestId('chat-session-action-deliverables')).toBeVisible()
    const toggle = page.getByTestId('topbar-workbench-toggle')
    await expect(page.locator('.topbar').getByTestId('topbar-workbench-toggle')).toBeVisible()
    await toggle.focus()
    await toggle.press('Enter')
    await expectLayout(page, 'split')
    await expect(toggle).toBeFocused()
    await openPanel(page, 'assistant-chat')
    await editor(side(page)).fill('Draft survives moving the pane toggle')
    await toggle.focus()
    await toggle.press('Enter')
    await expect(page.getByTestId('workbench-host')).not.toBeVisible()
    await expect(page.locator('.topbar').getByTestId('topbar-workbench-toggle')).toBeFocused()
    await toggle.press('Enter')
    await expectLayout(page, 'split')
    await expect(toggle).toBeFocused()
    await expect(editor(side(page))).toHaveText('Draft survives moving the pane toggle')
    await page.setViewportSize({ width: 840, height: 950 })
    await expectLayout(page, 'main-only')
    await expect(toggle).toBeFocused()
    await page.setViewportSize({ width: 390, height: 950 })
    await toggle.press('Enter')
    await expectLayout(page, 'single-pane')
    await expect(toggle).toBeFocused()
    await toggle.press('Enter')
    await expect(page.getByTestId('workbench-host')).not.toBeVisible()
    await expect(page.locator('.topbar').getByTestId('topbar-workbench-toggle')).toBeFocused()
    await page.setViewportSize({ width: 1600, height: 950 })
    await toggle.press('Enter')
    await expectLayout(page, 'split')
    await expect(editor(side(page))).toHaveText('Draft survives moving the pane toggle')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('keeps navigation open at 1200px without overlaying the main conversation', async ({ page }) => {
    const gateway = await prepare(page)
    await openPanel(page, 'assistant-chat')
    await page.setViewportSize({ width: 1200, height: 950 })
    await showSidebar(page, true)
    await expectLayout(page)
    await expectComposerControls(main(page))
    await expectComposerControls(side(page))
    await screenshot(page, 'after-1200-navigation-split.png')
    await page.setViewportSize({ width: 1000, height: 950 })
    await showSidebar(page, true)
    await expectLayout(page, 'main-only')
    await expectComposerControls(main(page))
    await expect(page.getByTestId('sidebar-toggle-expanded')).toBeVisible()
    expect(gateway.errors).toEqual([])
  })

  for (const kind of ['assistant-chat', 'workspace-files', 'html', 'code', 'terminal'] as const) {
    test(`${kind} yields to main on automatic shrink and restores its split when widened`, async ({ page }) => {
      const gateway = await prepare(page)
      await openPanel(page, kind)
      const htmlFrame = page.getByTestId('workspace-file-panel').locator('iframe')
      const htmlFrameHandle = kind === 'html' ? await htmlFrame.elementHandle() : null
      const htmlFrameSource = kind === 'html' ? await htmlFrame.getAttribute('src') : null
      if (kind === 'html') await page.getByTestId('workspace-file-panel').frameLocator('iframe')
        .getByRole('button', { name: 'Try interaction', exact: true }).click()
      await showSidebar(page, false)
      for (const width of [1200, 960, 842, 841, 840, 721, 720, 480, 390, 841, 1200]) {
        await setAvailableWidth(page, width)
        await expectLayout(page)
        if (kind === 'html') {
          expect(await htmlFrame.evaluate((element, original) => element === original, htmlFrameHandle)).toBe(true)
          expect(await htmlFrame.getAttribute('src')).toBe(htmlFrameSource)
          await expect(page.getByTestId('workspace-file-panel').frameLocator('iframe')
            .getByRole('button', { name: 'Clicked once', exact: true, includeHidden: true })).toHaveText('Clicked once')
        }
      }
      expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
      if (kind === 'terminal') expect(gateway.requests.filter(request => request.method === 'terminal.create')).toHaveLength(1)
      expect(gateway.errors).toEqual([])
    })
  }

  test('keeps the same drafts and executions through drag, navigation, resize and return-to-chat', async ({ page }) => {
    const gateway = await prepare(page)
    await openPanel(page, 'assistant-chat')
    const retainedMainEditor = await editor(main(page)).elementHandle()
    const retainedSideEditor = await editor(side(page)).elementHandle()
    for (const host of [main(page), side(page)]) {
      await editor(host).fill('Keep running during layout changes')
      await host.getByRole('button', { name: 'Send', exact: true }).click()
    }
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    gateway.emit(MAIN, 'session.event.text_delta', { text: 'Main execution remains live.' })
    gateway.emit(SIDE, 'session.event.text_delta', { text: 'Side execution remains live.' })
    await editor(main(page)).fill('Main unsent draft')
    await editor(side(page)).fill('Side unsent draft')
    const resize = page.getByTestId('workbench-resizer')
    const start = (await resize.boundingBox())!
    await page.mouse.move(start.x + start.width / 2, start.y + 90)
    await page.mouse.down()
    for (const delta of [80, 160, -80, -160, 20]) {
      await page.mouse.move(start.x + delta, start.y + 90)
      await expectLayout(page)
    }
    await page.mouse.up()
    await resize.focus()
    await resize.press('Home')
    await expectLayout(page)
    expect((await page.getByTestId('workbench-host').boundingBox())!.width).toBeCloseTo(360, 0)
    await expectComposerControls(side(page))
    await resize.press('End')
    await expectLayout(page)
    const minimumMainWidth = (await page.locator('.app-chat-pane').boundingBox())!.width
    // The split budget reserves the one-pixel divider in addition to 480px.
    expect(minimumMainWidth).toBeGreaterThanOrEqual(480)
    expect(minimumMainWidth).toBeLessThanOrEqual(481)
    await expectComposerControls(main(page))
    const preferred = await page.evaluate(() => localStorage.getItem('opensquilla.workbench.width.v1'))
    await showSidebar(page, false)
    for (const width of [1450, 1100, 960, 841, 840, 640, 390]) {
      await setAvailableWidth(page, width)
      await expectLayout(page)
    }
    await screenshot(page, 'after-390-main-priority-running.png')
    await expect(editor(main(page))).toHaveText('Main unsent draft')
    await expect(main(page).locator('.chat-stop-btn')).toBeEnabled()
    gateway.emit(SIDE, 'session.event.text_delta', { text: 'Side execution progressed while automatically hidden.' })
    gateway.emit(MAIN, 'session.event.text_delta', { text: 'Main remains live while the right pane yields.' })
    await expect(main(page).locator('.conversation-live-edge')).toContainText('Main remains live while the right pane yields.')
    await page.getByTestId('topbar-workbench-toggle').click()
    await expectLayout(page, 'single-pane')
    await expect(editor(side(page))).toHaveText('Side unsent draft')
    await expect(side(page).locator('.conversation-live-edge')).toContainText('Side execution progressed while automatically hidden.')
    await page.getByTestId('workbench-return-chat').click()
    await expectLayout(page, 'main-only')
    await expect(editor(main(page))).toHaveText('Main unsent draft')
    // Return only ends the temporary narrow presentation. The user's original
    // split preference and the same side child must return on widening.
    await setAvailableWidth(page, 1400)
    await expectLayout(page)
    await expect(main(page).locator('.chat-stop-btn')).toBeEnabled()
    await expect(side(page).locator('.chat-stop-btn')).toBeEnabled()
    expect(await editor(main(page)).evaluate((element, original) => element === original, retainedMainEditor)).toBe(true)
    expect(await editor(side(page)).evaluate((element, original) => element === original, retainedSideEditor)).toBe(true)
    expect(await page.evaluate(() => localStorage.getItem('opensquilla.workbench.width.v1'))).toBe(preferred)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
  })

  test('keeps narrow German composer buttons and popovers usable while resizing an open menu', async ({ page }) => {
    const gateway = await prepare(page, 'dark', 'de')
    await openPanel(page, 'assistant-chat')
    for (const [host, key] of [[main(page), MAIN], [side(page), SIDE]] as const) {
      await editor(host).fill('Modellauswahl initialisieren')
      await host.locator('.chat-send-btn').click()
      await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send' && (request.params?.sessionKey ?? request.params?.session_key ?? request.params?.key) === key).length).toBe(1)
      gateway.finish(key, 'Bereit.', 'succeeded', [], { model: 'synthetic/extremely-long-model-name-with-a-detailed-release-description-2026' })
      await expect(host).toContainText('Bereit.')
      await expect(host.locator('.chat-model-routing-btn')).toHaveAttribute('aria-description', /extremely-long-model-name/)
    }
    await page.setViewportSize({ width: 1200, height: 950 })
    await showSidebar(page, true)
    await expectLayout(page)
    for (const host of [main(page), side(page)]) {
      await page.setViewportSize({ width: 1200, height: 950 })
      await editor(host).fill('Gespeicherter Entwurf')
      await expectComposerControls(host)
      const routing = host.locator('.chat-model-routing-btn')
      const originalTrigger = await routing.elementHandle()
      await routing.click()
      await expectInsideViewport(page.locator('.composer-model-routing'), page)
      await screenshot(page, 'after-german-model-menu.png')
      await page.setViewportSize({ width: 1280, height: 950 })
      await expectInsideViewport(page.locator('.composer-model-routing'), page)
      await page.keyboard.press('Escape')
      await expect(routing).toBeFocused()
      expect(await routing.evaluate((element, original) => element === original, originalTrigger)).toBe(true)
      await host.locator('.chat-plus-btn').click()
      await expectInsideViewport(page.locator('.composer-add-menu'), page)
      await page.keyboard.press('Escape')
      await expect(host.locator('.chat-plus-btn')).toBeFocused()
      await expect(editor(host)).toHaveText('Gespeicherter Entwurf')
    }
    expect(gateway.errors).toEqual([])
  })

  for (const [name, trigger, popup] of [
    ['model routing', '.chat-model-routing-btn', '.composer-model-routing'],
    ['permissions', '.chat-run-mode-btn', '.composer-run-mode'],
    ['add', '.chat-plus-btn', '.composer-add-menu'],
  ] as const) test(`closes the side ${name} menu when its owner becomes hidden by automatic shrink`, async ({ page }) => {
    const gateway = await prepare(page)
    await openPanel(page, 'assistant-chat')
    await editor(side(page)).fill('Side draft survives hidden popover cleanup')
    await side(page).locator(trigger).click()
    await expect(page.locator(popup)).toBeVisible()
    await page.setViewportSize({ width: 390, height: 950 })
    await expectLayout(page, 'main-only')
    await expect(page.locator(popup)).toHaveCount(0)
    await expect(side(page).locator(trigger)).toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('.app-chat-pane'))), {
      message: 'Closing a hidden owner menu must leave focus in the visible main conversation',
    }).toBe(true)
    await page.setViewportSize({ width: 1600, height: 950 })
    await expectLayout(page, 'split')
    await expect(page.locator(popup)).toHaveCount(0)
    await expect(editor(side(page))).toHaveText('Side draft survives hidden popover cleanup')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('closes the main model menu when its owner becomes hidden by explicit right-pane reveal', async ({ page }) => {
    const gateway = await prepare(page)
    await openPanel(page, 'assistant-chat')
    await page.setViewportSize({ width: 390, height: 950 })
    await expectLayout(page, 'main-only')
    await editor(main(page)).fill('Main draft survives explicit reveal')
    await main(page).locator('.chat-model-routing-btn').click()
    await expect(page.locator('.composer-model-routing')).toBeVisible()
    await page.getByTestId('topbar-workbench-toggle').focus()
    await page.getByTestId('topbar-workbench-toggle').press('Enter')
    await expectLayout(page, 'single-pane')
    await expect(page.locator('.composer-model-routing')).toHaveCount(0)
    await expect(main(page).locator('.chat-model-routing-btn')).toHaveAttribute('aria-expanded', 'false')
    await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[data-testid="workbench-host"]')))).toBe(true)
    await page.getByTestId('workbench-return-chat').click()
    await expectLayout(page, 'main-only')
    await expect(page.locator('.composer-model-routing')).toHaveCount(0)
    await expect(editor(main(page))).toHaveText('Main draft survives explicit reveal')
    expect(gateway.errors).toEqual([])
  })

  test('keeps main first on shrink and reveals a narrow panel only for explicit user intent', async ({ page }) => {
    const gateway = await prepare(page)
    await openPanel(page, 'assistant-chat')
    await editor(main(page)).fill('Main draft remains available')
    await editor(side(page)).fill('Side draft remains mounted')
    await editor(side(page)).focus()
    await page.setViewportSize({ width: 840, height: 950 })
    await expectLayout(page, 'main-only')
    await expect(editor(main(page))).toBeVisible()
    await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('.app-chat-pane'))), {
      message: 'Focus must leave the automatically hidden right pane and return to the main conversation',
    }).toBe(true)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expectLayout(page, 'single-pane')
    for (const width of [720, 390]) {
      await page.setViewportSize({ width, height: 950 })
      await expectLayout(page, 'single-pane')
      await expect(editor(side(page))).toHaveText('Side draft remains mounted')
      await expectComposerControls(side(page))
    }
    // Widening ends the temporary reveal. A subsequent automatic shrink once
    // again chooses main, rather than carrying a stale right-pane preference.
    await page.setViewportSize({ width: 841, height: 950 })
    await expectLayout(page, 'split')
    await page.setViewportSize({ width: 390, height: 950 })
    await expectLayout(page, 'main-only')
    await expectComposerControls(main(page))
    await openPanel(page, 'html')
    await expectLayout(page, 'single-pane')
    await page.getByTestId('workbench-return-chat').click()
    await expectLayout(page, 'main-only')
    await expect(editor(main(page))).toHaveText('Main draft remains available')
    await page.setViewportSize({ width: 1200, height: 950 })
    await expectLayout(page, 'split')
    await expect(page.getByTestId('workspace-file-panel').frameLocator('iframe').getByRole('heading', { name: 'Parity HTML document' })).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('registers an agent-opened artifact on narrow screens without replacing the main conversation', async ({ page }) => {
    const gateway = await prepare(page)
    await page.setViewportSize({ width: 390, height: 950 })
    await editor(main(page)).fill('Generate a background HTML preview')
    await main(page).getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.registerDocument(MAIN, 'doc_narrow_background', 'background.html')
    const result = { documentId: 'doc_narrow_background', resourceId: 'document:doc_narrow_background', entrypoint: '/task/background.html', previewStatus: 'ready' }
    gateway.emit(MAIN, 'session.event.tool_use_start', { tool_use_id: 'background-preview', tool_name: 'open_workspace_preview', input: { path: 'background.html' } })
    gateway.emit(MAIN, 'session.event.tool_result', { tool_use_id: 'background-preview', name: 'open_workspace_preview', result: JSON.stringify(result) })
    await expect.poll(() => gateway.requests.filter(request => request.method === 'workbench.resources.open').length).toBe(1)
    gateway.emit(MAIN, 'session.event.text_delta', { text: 'Main remains visible after the artifact is ready.' })
    await expect(main(page).locator('.conversation-live-edge')).toContainText('Main remains visible after the artifact is ready.')
    await expectLayout(page, 'main-only')
    await editor(main(page)).fill('Keep typing while the preview is retained')
    await expect(editor(main(page))).toBeFocused()
    await page.getByTestId('topbar-workbench-toggle').click()
    await expectLayout(page, 'single-pane')
    await expect(page.frameLocator('.artifact-preview__frame--html').getByRole('heading', { name: 'Canonical child preview', exact: true })).toBeVisible()
    await page.getByTestId('workbench-return-chat').click()
    await expectLayout(page, 'main-only')
    await expect(editor(main(page))).toHaveText('Keep typing while the preview is retained')
    expect(gateway.requests.filter(request => request.method === 'chat.abort')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('preserves a user-maximized right pane when resizing to a narrow viewport', async ({ page }) => {
    const gateway = await prepare(page)
    await openPanel(page, 'assistant-chat')
    await editor(side(page)).fill('Explicit fullscreen draft')
    await page.getByTestId('workbench-maximize').click()
    await expectLayout(page, 'single-pane')
    for (const width of [840, 390, 1300]) {
      await page.setViewportSize({ width, height: 950 })
      await expectLayout(page, 'single-pane')
      await expect(editor(side(page))).toHaveText('Explicit fullscreen draft')
    }
    await page.getByTestId('workbench-maximize').click()
    await expectLayout(page, 'split')
    await page.getByTestId('workbench-maximize').click()
    await page.setViewportSize({ width: 390, height: 950 })
    await expectLayout(page, 'single-pane')
    await page.getByTestId('workbench-return-chat').click()
    await expect(editor(main(page))).toBeVisible()
    await expect(page.getByTestId('workbench-host')).not.toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
  })

  test('partitions every animation frame through open, maximize, restore and maximized collapse', async ({ page }) => {
    await prepare(page)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const opening = await sampleTransition(page, () => openPanel(page, 'assistant-chat'))
    await expectLayout(page)
    const splitWidth = (await page.getByTestId('workbench-host').boundingBox())!.width
    expectTrackMotion(opening, 0, splitWidth)
    const maximizing = await sampleTransition(page, () => page.getByTestId('workbench-maximize').click())
    await expectLayout(page, 'single-pane')
    const fullWidth = (await page.getByTestId('workbench-host').boundingBox())!.width
    expectTrackMotion(maximizing, splitWidth, fullWidth)
    const restoring = await sampleTransition(page, () => page.getByTestId('workbench-maximize').click())
    await expectLayout(page)
    expectTrackMotion(restoring, fullWidth, splitWidth)
    await sampleTransition(page, () => page.getByTestId('workbench-maximize').click())
    const closing = await sampleTransition(page, () => page.getByTestId('workbench-return-chat').click())
    expectTrackMotion(closing, fullWidth, 0)
    const firstLeaving = closing.find(frame => frame.leaving)
    expect(firstLeaving, 'Closing a maximized pane should animate from its actual width, not jump to the split preference').toBeTruthy()
    expect(firstLeaving!.hostWidth).toBeGreaterThan(splitWidth + 20)
    await expect(page.getByTestId('workbench-host')).not.toBeVisible()
    await expect(editor(main(page))).toBeVisible()
  })

  test('honors reduced motion and keeps viewport resize and split drag direct', async ({ page }) => {
    await prepare(page)
    await openPanel(page, 'assistant-chat')
    await page.getByTestId('workbench-maximize').click()
    await expectLayout(page, 'single-pane')
    const reducedDurations = await page.getByTestId('workbench-host').evaluate(element =>
      getComputedStyle(element).transitionDuration.split(',').map(value => Number.parseFloat(value)))
    // The global reduced-motion kill switch is 0.01ms !important, not 0s.
    for (const duration of reducedDurations) expect(duration).toBeLessThanOrEqual(0.00001)
    await page.getByTestId('workbench-maximize').click()
    await expectLayout(page)
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.setViewportSize({ width: 1450, height: 950 })
    await expectLayout(page)
    expect(await page.getByTestId('workbench-host').evaluate(element => getComputedStyle(element).transitionDuration)).toMatch(/^0s(?:, 0s)*$/)
    const handle = await page.getByTestId('workbench-resizer').boundingBox()
    await page.mouse.move(handle!.x + 1, handle!.y + 60)
    await page.mouse.down()
    await page.mouse.move(handle!.x + 50, handle!.y + 60)
    expect(await page.getByTestId('workbench-host').evaluate(element => getComputedStyle(element).transitionDuration)).toMatch(/^0s(?:, 0s)*$/)
    await page.mouse.up()
    await expectLayout(page)
  })

  test('hands focus out of automatically yielded navigation without changing its saved preference', async ({ page }) => {
    await prepare(page)
    await page.setViewportSize({ width: 1300, height: 950 })
    await openPanel(page, 'assistant-chat')
    await showSidebar(page, true)
    await page.getByTestId('sidebar-toggle-expanded').focus()
    const before = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage).filter(([key]) => /sidebar/i.test(key))))
    await page.setViewportSize({ width: 1000, height: 950 })
    await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeVisible()
    await expect.poll(() => page.evaluate(() => Boolean(document.activeElement && !document.activeElement.closest('[inert]') && document.activeElement.getClientRects().length))).toBe(true)
    await expectLayout(page)
    expect(await page.evaluate(() => Object.fromEntries(Object.entries(localStorage).filter(([key]) => /sidebar/i.test(key))))).toEqual(before)
    for (const width of [842, 841, 840, 839, 767, 390]) {
      await page.setViewportSize({ width, height: 950 })
      await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeVisible()
      await expect(page.locator('#sidebar-nav')).toHaveAttribute('inert', '')
      await expectLayout(page)
      expect(await page.evaluate(() => Object.fromEntries(Object.entries(localStorage).filter(([key]) => /sidebar/i.test(key))))).toEqual(before)
    }
    await page.setViewportSize({ width: 1300, height: 950 })
    await expect(page.getByTestId('sidebar-toggle-expanded')).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('#sidebar-nav')).not.toHaveAttribute('inert', '')
    await expectLayout(page)
  })

  for (const theme of ['light', 'dark']) test(`${theme} keeps the active tab close visible and places Add immediately after tabs until overflow`, async ({ page }) => {
    const gateway = await prepare(page, theme)
    await showSidebar(page, false)
    await openPanel(page, 'html')
    const host = page.getByTestId('workbench-host')
    const add = page.getByTestId('workbench-new-panel-trigger')
    const tabs = host.getByRole('tab')
    const tabGeometry = () => host.evaluate(element => {
      const strip = element.querySelector<HTMLElement>('[role="tablist"]')!
      const region = element.querySelector<HTMLElement>('.workbench-host__tab-region')!
      const last = strip.querySelector<HTMLElement>('.workbench-host__tab-wrap:last-child')!
      const add = element.querySelector<HTMLElement>('[data-testid="workbench-new-panel-trigger"]')!
      const actions = element.querySelector<HTMLElement>('.workbench-host__actions')!
      const s = strip.getBoundingClientRect(), r = region.getBoundingClientRect()
      const l = last.getBoundingClientRect(), a = add.getBoundingClientRect(), end = actions.getBoundingClientRect()
      return { overflow: strip.scrollWidth > strip.clientWidth + 1, lastRight: l.right,
        stripRight: s.right, regionRight: r.right, addLeft: a.left, addRight: a.right, actionsLeft: end.left,
        rowAligned: Math.abs((a.top + a.bottom) / 2 - (s.top + s.bottom) / 2) <= 1,
        inViewport: a.left >= 0 && a.right <= innerWidth,
      }
    })
    const expectAddPosition = async (overflow: boolean) => {
      await expect.poll(async () => {
        const g = await tabGeometry()
        const edge = overflow ? g.stripRight : g.lastRight
        return g.overflow === overflow && g.addLeft >= edge - 1 && g.addLeft - edge <= 8
          && g.addRight <= g.actionsLeft && g.addRight <= g.regionRight + 1
          && (!overflow || g.regionRight - g.addRight <= 1) && g.rowAligned && g.inViewport
      }).toBe(true)
    }
    const expectActiveClose = async () => {
      await editor(main(page)).focus()
      await page.mouse.move(0, 0)
      const close = host.locator('.workbench-host__tab-wrap.is-active .workbench-host__tab-close')
      await expect(close).toHaveCSS('opacity', '1')
      await expect(close).toHaveCSS('border-radius', '4px')
      const box = (await close.boundingBox())!
      expect(box.width).toBe(24)
      expect(box.height).toBe(24)
    }
    await expect(tabs).toHaveCount(1)
    await expectActiveClose()
    await expectAddPosition(false)
    await openPanel(page, 'code')
    await expect(tabs).toHaveCount(2)
    await expectActiveClose()
    const inactive = host.locator('.workbench-host__tab-wrap:not(.is-active)')
    const inactiveClose = inactive.locator('.workbench-host__tab-close')
    await expect(inactiveClose).toHaveCSS('opacity', '0')
    await inactive.hover()
    await expect(inactiveClose).toHaveCSS('opacity', '1')
    await page.mouse.move(0, 0)
    await inactive.getByRole('tab').focus()
    await expect(inactiveClose).toHaveCSS('opacity', '1')
    await tabs.first().click()
    await expectActiveClose()
    await expect(inactiveClose).toHaveCSS('opacity', '0')
    await expectAddPosition(false)
    for (const kind of ['terminal', 'assistant-chat', 'workspace-files'] as const) await openPanel(page, kind)
    await expect(tabs).toHaveCount(5)
    await page.setViewportSize({ width: 390, height: 950 })
    await expectLayout(page, 'main-only')
    await page.getByTestId('topbar-workbench-toggle').click()
    await expectLayout(page, 'single-pane')
    await expectAddPosition(true)
    await add.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('workbench-new-panel-menu-list')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(add).toBeFocused()
    await page.setViewportSize({ width: 1600, height: 950 })
    await expectLayout(page, 'split')
    for (let count = 5; count > 1; count -= 1) {
      const close = host.locator('.workbench-host__tab-close').last()
      await close.hover()
      await close.click()
      await expect(tabs).toHaveCount(count - 1)
    }
    await expectActiveClose()
    await expectAddPosition(false)
    expect(gateway.requests.filter(request => request.method === 'chat.abort')).toHaveLength(0)
  })

  test('keeps the selected right tab visible as a multi-tab workspace shrinks', async ({ page }) => {
    const gateway = await prepare(page)
    for (const kind of ['html', 'code', 'terminal', 'assistant-chat'] as const) await openPanel(page, kind)
    await expect(page.getByTestId('workbench-host').getByRole('tab')).toHaveCount(4)
    await editor(side(page)).fill('Retain the active side draft')
    for (const width of [1200, 840, 390, 1200]) {
      await page.setViewportSize({ width, height: 950 })
      await expectLayout(page)
      if (width < 841) {
        await expect(editor(main(page))).toBeVisible()
        await page.getByTestId('topbar-workbench-toggle').click()
        await expectLayout(page, 'single-pane')
      }
      await expect.poll(() => page.getByTestId('workbench-host').evaluate(element => {
        const tabs = element.querySelector('[role="tablist"]')!.getBoundingClientRect()
        const active = element.querySelector('[role="tab"][aria-selected="true"]')!.getBoundingClientRect()
        return active.left >= tabs.left - 1 && active.right <= tabs.right + 1
      }), { message: 'The active tab must remain inside the visible tab strip after narrowing' }).toBe(true)
      const close = page.locator('.workbench-host__tab-wrap.is-active .workbench-host__tab-close')
      for (const activate of [() => close.hover(), () => close.focus()]) {
        await activate()
        await expect(close).toHaveCSS('opacity', '1')
        await expect(close).toHaveCSS('border-radius', '4px')
        const bounds = (await close.boundingBox())!
        expect(bounds.width).toBe(24)
        expect(bounds.height).toBe(24)
      }
      await expect(editor(side(page))).toHaveText('Retain the active side draft')
      if (width < 841) {
        await page.getByTestId('workbench-return-chat').click()
        await expectLayout(page, 'main-only')
      }
    }
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close' || request.method === 'chat.abort')).toHaveLength(0)
  })

  test('preserves an explicitly collapsed navigation through phone and desktop widths', async ({ page }) => {
    await prepare(page)
    await showSidebar(page, false)
    const before = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage).filter(([key]) => /sidebar/i.test(key))))
    for (const width of [767, 390, 1300]) {
      await page.setViewportSize({ width, height: 950 })
      await expect(page.getByTestId('sidebar-toggle-collapsed')).toBeVisible()
      await expect(page.locator('#sidebar-nav')).toHaveAttribute('inert', '')
      await expect(editor(main(page))).toBeVisible()
      expect(await page.evaluate(() => Object.fromEntries(Object.entries(localStorage).filter(([key]) => /sidebar/i.test(key))))).toEqual(before)
    }
    await openPanel(page, 'assistant-chat')
    await expectLayout(page)
    await expect(page.locator('#sidebar-nav')).toHaveAttribute('inert', '')
  })

  for (const theme of themes) test(`${theme} keeps geometry valid with explicit navigation open and closed`, async ({ page }) => {
    await prepare(page, theme)
    await openPanel(page, 'assistant-chat')
    await page.setViewportSize({ width: 1200, height: 950 })
    await showSidebar(page, true)
    await expectLayout(page)
    await showSidebar(page, false)
    await expectLayout(page)
    await setAvailableWidth(page, 840)
    await expectLayout(page)
    await expectComposerControls(main(page))
    await page.getByTestId('topbar-workbench-toggle').click()
    await expectLayout(page, 'single-pane')
    await expectComposerControls(side(page))
  })

  test('keeps the native browser capability boundary explicit in WebUI', async ({ page }) => {
    await prepare(page)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(page.locator('[data-panel-kind="browser"]')).toBeDisabled()
    await expect(page.locator('[data-panel-kind="browser"]')).toContainText('requires OpenSquilla Desktop')
    expect(await page.evaluate(() => Boolean(window.opensquillaDesktop))).toBe(false)
    // This checks the WebUI boundary, not native WebContents rendering.
  })
})
