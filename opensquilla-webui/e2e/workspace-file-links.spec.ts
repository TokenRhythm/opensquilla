import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload } from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:workspace-files'
const OTHER = 'agent:main:webchat:workspace-other'
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" onload="window.svgExecuted=true"><text>safe source</text></svg>'
const HTML = '<!doctype html>\n<html><script>window.htmlExecuted=true</script><body>safe source</body></html>\n'
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK0cAAAAASUVORK5CYII=', 'base64')
const BUILD = '#!/usr/bin/env python3\n\nIMG_DIR = \'_img/opt\'\n\nprint("build fixture")\n'
const LARGE = Array.from({ length: 450 }, (_, index) => `print("fixture line ${index + 1}${index === 428 ? ' distant-needle' : ''}")`).join('\n') + '\n'
const ANSWER = [
  'Generated files fixture.', '', '| Format | File |', '| --- | --- |',
  '| SVG | `outputs/中文 图.svg` |', '| PNG | [PNG image](<outputs/中文 图.png>) |',
  '', '`outputs/missing.svg`', '', '```text', 'outputs/ignored.svg', '```',
].join('\n')

interface FixtureOptions {
  deferred?: boolean
  source?: 'build' | 'large' | 'html'
  theme?: 'dark' | 'light'
  pageError?: boolean
}

function sourceLines(content: string) {
  // Match the Gateway's splitlines(keepends=True), including the last selected
  // line's terminator; a file ending with a blank line must keep that line.
  return content.match(/[^\r\n\v\f\x1c-\x1e\x85\u2028\u2029]*(?:\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029])|[^\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+$/g) ?? ['']
}

// Synthetic Gateway/HTTP fixtures exercise the shipped UI in a real browser.
// Text reads intentionally advertise and implement the same paging capability
// as the Gateway, so a missing page route cannot silently test the old fallback.
async function install(page: Page, options: FixtureOptions = {}) {
  const { deferred = false, source, theme = 'dark' } = options
  const sourcePath = source === 'html' ? 'outputs/source.html' : source === 'large' ? 'outputs/large.py' : '_img/build.py'
  const sourceContent = source === 'html' ? HTML : source === 'large' ? LARGE : BUILD
  const contentFor = (path: string) => path === sourcePath ? sourceContent : SVG
  let release!: () => void
  const wait = new Promise<void>(resolve => { release = resolve })
  const resolves: { sessionKey: string; paths: string[] }[] = []
  const reads: { sessionKey: string; path: string; binding: string }[] = []
  const pages: { sessionKey: string; path: string; binding: string; startLine: number; endLine: number }[] = []
  const searches: { path: string; query: string }[] = []
  await page.addInitScript(selectedTheme => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', selectedTheme)
    const scoped = window as unknown as { OPENSQUILLA_FEATURES?: Record<string, boolean>; workspaceCopied?: string }
    scoped.OPENSQUILLA_FEATURES = { ...(scoped.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: async (value: string) => { scoped.workspaceCopied = value },
    } })
  }, theme)
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/v1/workspace-files/resolve', async route => {
    const sessionKey = route.request().headers()['x-opensquilla-session-key'] || ''
    const { paths } = route.request().postDataJSON() as { paths: string[] }
    resolves.push({ sessionKey, paths })
    if (deferred && sessionKey === SESSION) await wait
    const files = sessionKey === SESSION ? paths.filter(path => source
      ? path === sourcePath
      : /中文 图\.(svg|png)$/.test(path)).map(path => ({
      requestedPath: path, path, name: path.split('/').pop(),
      size: path.endsWith('.png') ? PNG.length : Buffer.byteLength(contentFor(path)),
      mime: path.endsWith('.py') ? 'text/x-python' : path.endsWith('.html') ? 'text/html'
        : path.endsWith('.svg') ? 'image/svg+xml' : 'image/png',
      kind: path.endsWith('.png') ? 'image' : 'text', textPaging: !path.endsWith('.png'), nativeActions: false,
    })) : []
    await route.fulfill({ json: { workspaceBinding: 'binding-A', files } }).catch(() => undefined)
  })
  await page.route('**/api/v1/workspace-files/page?**', async route => {
    const url = new URL(route.request().url())
    const path = url.searchParams.get('path') || ''
    const startLine = Number(url.searchParams.get('startLine'))
    const endLine = Number(url.searchParams.get('endLine'))
    pages.push({ sessionKey: route.request().headers()['x-opensquilla-session-key'] || '', path,
      binding: url.searchParams.get('workspaceBinding') || '', startLine, endLine })
    if (options.pageError) {
      await route.fulfill({ status: 404, json: { code: 'WORKSPACE_FILE_UNAVAILABLE' } })
      return
    }
    const lines = sourceLines(contentFor(path))
    await route.fulfill({ json: { relativePath: path, content: lines.slice(startLine - 1, endLine).join(''),
      totalLines: lines.length, startLine, endLine: Math.min(endLine, lines.length) } })
  })
  await page.route('**/api/v1/workspace-files/search?**', async route => {
    const url = new URL(route.request().url())
    const path = url.searchParams.get('path') || ''
    const query = url.searchParams.get('query') || ''
    searches.push({ path, query })
    const lines = sourceLines(contentFor(path))
    const match = lines.findIndex(line => line.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
    await route.fulfill({ json: { relativePath: path, totalLines: lines.length, matchLine: match < 0 ? null : match + 1 } })
  })
  await page.route('**/api/v1/workspace-files/content?**', async route => {
    const url = new URL(route.request().url())
    const path = url.searchParams.get('path') || ''
    reads.push({ sessionKey: route.request().headers()['x-opensquilla-session-key'] || '', path,
      binding: url.searchParams.get('workspaceBinding') || '' })
    await route.fulfill({ body: path.endsWith('.png') ? PNG : contentFor(path),
      contentType: path.endsWith('.png') ? 'image/png' : path.endsWith('.html') ? 'text/html'
        : path.endsWith('.svg') ? 'image/svg+xml' : 'text/x-python' })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as { type?: string; id?: string; method?: string; params?: Record<string, unknown> }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ auth: { principal: { isOwner: true, authenticated: true, authState: 'authenticated',
          role: 'operator', scopes: ['operator.read', 'operator.write'] } } }))
        return
      }
      const key = String(frame.params?.key || frame.params?.sessionKey || SESSION)
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload([{ role: 'assistant', id: `answer-${key}`, timestamp: 1_800_000_000,
          text: key === OTHER ? 'Other workspace fixture. `outputs/中文 图.svg`'
            : source ? `Generated workspace file fixture. The source is \`${sourcePath}\`; click it to inspect the read-only file.` : ANSWER }]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} }, permissions: {}, skills: {} },
        'onboarding.status': { audioConfigured: false },
        'sandbox.run_mode.preference.get': { runMode: 'safe', source: 'config' },
        'sandbox.capability.status': { available: false }, 'usage.status': { sessions: [] },
      }
      ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload: payloads[frame.method || ''] ?? {} }))
    })
  })
  return { resolves, reads, pages, searches, release }
}

test('opens table SVG as text and a Markdown PNG path as an image through authenticated file access', async ({ page }) => {
  const state = await install(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Generated files fixture.' })
  await expect(answer.locator('.workspace-file-link')).toHaveCount(2)
  expect(state.resolves[0].paths).toEqual(['outputs/中文 图.svg', 'outputs/中文 图.png', 'outputs/missing.svg'])
  await expect(answer.locator('code').filter({ hasText: 'outputs/missing.svg' })).toBeVisible()
  await answer.getByRole('link', { name: 'Open outputs/中文 图.svg', exact: true }).click()
  const source = page.getByTestId('workspace-file-panel')
  await expect(source).toBeVisible()
  await expect(source.locator('pre')).toContainText(SVG)
  await expect(source.locator('svg[onload], iframe, object, img')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).svgExecuted)).toBeUndefined()
  await page.screenshot({ path: 'output/playwright/workspace-file-svg-source.png', fullPage: true })
  await answer.locator('.workspace-file-action-trigger').first().click()
  await expect(page.getByRole('menu')).toContainText('Copy relative path')
  await page.screenshot({ path: 'output/playwright/workspace-file-svg-menu.png', fullPage: true })
  await page.keyboard.press('Escape')
  await answer.getByRole('link', { name: 'Open outputs/中文 图.png', exact: true }).click()
  const image = page.getByRole('dialog', { name: '中文 图.png', exact: true }).locator('img')
  await expect(image).toBeVisible()
  await expect(image).toHaveAttribute('src', /^blob:/)
  await page.screenshot({ path: 'output/playwright/workspace-file-image.png', fullPage: true })
  expect(state.pages).toEqual([
    { sessionKey: SESSION, path: 'outputs/中文 图.svg', binding: 'binding-A', startLine: 1, endLine: 200 },
  ])
  expect(state.reads).toEqual([{ sessionKey: SESSION, path: 'outputs/中文 图.png', binding: 'binding-A' }])
  await expect(answer.locator('a[href^="file:"]')).toHaveCount(0)
})

for (const theme of ['dark', 'light'] as const) {
  test(`opens an inline _img/build.py path in the read-only source Workbench (${theme})`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const state = await install(page, { source: 'build', theme })
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const suffix = theme === 'dark' ? '' : '-light'
    const answer = page.locator('.msg-ai').filter({ hasText: 'Generated workspace file fixture.' })
    const link = answer.getByRole('link', { name: 'Open _img/build.py', exact: true })
    const trigger = answer.locator('.workspace-file-action-trigger')
    await expect(link).toHaveCount(1)
    await expect(link).toHaveText('_img/build.py')
    await expect(link).toHaveCSS('text-decoration-line', 'underline')
    await expect(link).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    await expect(trigger).toHaveCSS('color', await link.evaluate(el => getComputedStyle(el).color))
    await expect(answer.locator('.workspace-file-meta, .workspace-file-icon')).toHaveCount(0)
    await page.screenshot({ path: `output/playwright/workspace-file-inline${suffix}.png`, fullPage: true })
    await link.click()
    const source = page.getByTestId('workspace-file-panel')
    await expect(source).toBeVisible()
    await expect(page.getByText('build.py', { exact: true }).last()).toBeVisible()
    await expect(source.locator('.workspace-file__number').first()).toHaveText('1')
    await expect(source.locator('pre')).toContainText('#!/usr/bin/env python3')
    await expect(source.locator('pre')).toContainText("IMG_DIR = '_img/opt'")
    await expect(source.locator('pre')).toContainText('print("build fixture")')
    await expect(page.getByText(/Read-only source/).last()).toBeVisible()
    await page.screenshot({ path: `output/playwright/workspace-file-source${suffix}.png`, fullPage: true })
    await link.click({ button: 'right' })
    const menu = page.getByRole('menu')
    await expect(menu).toBeVisible()
    const contextActions = await menu.getByRole('menuitem').allTextContents()
    await page.keyboard.press('Escape')
    await expect(link).toBeFocused()
    await trigger.click()
    await expect(menu.getByRole('menuitem')).toHaveText(contextActions)
    await expect(menu).toContainText('Open _img/build.py in Workbench')
    await expect(menu).toContainText('Copy relative path')
    await expect(menu).toContainText('Copy file contents')
    await expect(menu).toContainText('Download')
    await expect(menu).not.toContainText('Open with default app')
    await expect(menu).not.toContainText('Finder')
    await page.screenshot({ path: `output/playwright/workspace-file-menu${suffix}.png`, fullPage: true })
    await menu.getByRole('menuitem', { name: 'Copy relative path', exact: true }).click()
    expect(await page.evaluate(() => (window as unknown as { workspaceCopied?: string }).workspaceCopied)).toBe('_img/build.py')
    await expect(trigger).toBeFocused()
    for (const key of ['ContextMenu', 'Shift+F10']) {
      await link.focus()
      await page.keyboard.press(key)
      await expect(menu.getByRole('menuitem')).toHaveText(contextActions)
      await page.keyboard.press('Escape')
      await expect(link).toBeFocused()
    }
    expect(state.resolves[0].paths).toEqual(['_img/build.py'])
    expect(state.pages).toEqual([{ sessionKey: SESSION, path: '_img/build.py', binding: 'binding-A', startLine: 1, endLine: 200 }])
    expect(state.reads).toEqual([])
  })
}

test('loads bounded pages, jumps and searches beyond the first page, and copies the complete file', async ({ page }) => {
  const state = await install(page, { source: 'large' })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await page.getByRole('link', { name: 'Open outputs/large.py', exact: true }).click()
  const source = page.getByTestId('workspace-file-panel')
  await expect(source.locator('.workspace-file__range')).toHaveText('Lines 1–200 of 450')
  await expect(source.locator('[data-line="201"]')).toHaveCount(0)
  expect(state.reads).toEqual([])
  await source.getByRole('button', { name: 'Show more lines', exact: true }).click()
  await expect(source.locator('.workspace-file__range')).toHaveText('Lines 201–400 of 450')
  await expect(source.locator('[data-line="201"]')).toContainText('fixture line 201')
  await source.getByRole('spinbutton', { name: 'Jump to line' }).fill('429')
  await source.getByRole('spinbutton', { name: 'Jump to line' }).press('Enter')
  await expect(source.locator('.workspace-file__range')).toHaveText('Lines 401–450 of 450')
  await expect(source.locator('[data-line="429"]')).toContainText('distant-needle')
  await source.getByRole('spinbutton', { name: 'Jump to line' }).fill('1')
  await source.getByRole('spinbutton', { name: 'Jump to line' }).press('Enter')
  await expect(source.locator('.workspace-file__range')).toHaveText('Lines 1–200 of 450')
  await source.getByRole('searchbox', { name: 'Search source' }).fill('distant-needle')
  await source.getByRole('searchbox', { name: 'Search source' }).press('Enter')
  await expect(source.locator('[data-line="429"]')).toContainText('distant-needle')
  expect(state.searches).toEqual([{ path: 'outputs/large.py', query: 'distant-needle' }])
  expect(state.pages.map(({ startLine, endLine }) => [startLine, endLine])).toEqual([
    [1, 200], [201, 400], [401, 600], [1, 200], [401, 600],
  ])
  expect(state.reads).toEqual([])
  await source.getByRole('button', { name: 'Copy file contents', exact: true }).click()
  await expect(source.getByRole('button', { name: 'Copied', exact: true })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { workspaceCopied?: string }).workspaceCopied)).toBe(LARGE)
  expect(state.reads).toEqual([{ sessionKey: SESSION, path: 'outputs/large.py', binding: 'binding-A' }])
})

test('ordinary workspace HTML stays source text without running its script', async ({ page }) => {
  const state = await install(page, { source: 'html' })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await page.getByRole('link', { name: 'Open outputs/source.html', exact: true }).click()
  const source = page.getByTestId('workspace-file-panel')
  await expect(source.locator('pre')).toContainText('<script>window.htmlExecuted=true</script>')
  await expect(source.locator('script, iframe, object')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).htmlExecuted)).toBeUndefined()
  expect(state.pages).toHaveLength(1)
  expect(state.reads).toEqual([])
})

test('a changed file shows a source error without falling back to a complete read', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const state = await install(page, { source: 'build', theme: 'light', pageError: true })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await page.getByRole('link', { name: 'Open _img/build.py', exact: true }).click()
  await expect(page.getByTestId('workspace-file-panel').getByRole('alert')).toContainText('Unable to open the file')
  expect(state.pages).toHaveLength(1)
  expect(state.reads).toEqual([])
  await page.screenshot({ path: 'output/playwright/workspace-file-error-light.png', fullPage: true })
})

test('late file resolution cannot decorate a different session after SPA navigation', async ({ page }) => {
  const state = await install(page, { deferred: true })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  await expect.poll(() => state.resolves.length).toBeGreaterThan(0)
  await page.evaluate(key => {
    history.pushState({}, '', `/control/chat?session=${encodeURIComponent(key)}`)
    dispatchEvent(new PopStateEvent('popstate'))
  }, OTHER)
  await expect(page.locator('.msg-ai').filter({ hasText: 'Other workspace fixture.' })).toBeVisible()
  state.release()
  await expect.poll(() => state.resolves.some(item => item.sessionKey === OTHER)).toBe(true)
  await expect(page.locator('.msg-ai .workspace-file-link')).toHaveCount(0)
  expect(state.reads).toEqual([])
  expect(state.pages).toEqual([])
})

test('inline file actions and source stay inside the viewport on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await install(page, { source: 'build' })
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Generated workspace file fixture.' })
  await answer.locator('.workspace-file-action-trigger').click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  const bounds = await menu.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844)
  await page.screenshot({ path: 'output/playwright/workspace-file-mobile-menu.png', fullPage: true })
  await page.keyboard.press('Escape')
  await answer.getByRole('link', { name: 'Open _img/build.py', exact: true }).click()
  await expect(page.getByTestId('workspace-file-panel').locator('pre')).toContainText('print("build fixture")')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({ path: 'output/playwright/workspace-file-mobile.png', fullPage: true })
})
