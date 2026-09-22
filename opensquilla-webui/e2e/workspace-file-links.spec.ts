import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload } from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:workspace-files'
const OTHER = 'agent:main:webchat:workspace-other'
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" onload="window.svgExecuted=true"><text>safe source</text></svg>'
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK0cAAAAASUVORK5CYII=', 'base64')
const BUILD = '#!/usr/bin/env python3\n\nIMG_DIR = \'_img/opt\'\n\nprint("build fixture")\n'
const ANSWER = [
  'Generated files fixture.', '', '| Format | File |', '| --- | --- |',
  '| SVG | `outputs/中文 图.svg` |', '| PNG | [PNG image](<outputs/中文 图.png>) |',
  '', '`outputs/missing.svg`', '', '```text', 'outputs/ignored.svg', '```',
].join('\n')

// Synthetic Gateway/HTTP fixtures exercise the shipped UI in a real browser.
// The separate live-API acceptance run verifies model output and actual files.
async function install(page: Page, deferred = false, buildFixture = false) {
  let release!: () => void
  const wait = new Promise<void>(resolve => { release = resolve })
  const resolves: { sessionKey: string; paths: string[] }[] = []
  const reads: { sessionKey: string; path: string; binding: string }[] = []
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    const scoped = window as unknown as { OPENSQUILLA_FEATURES?: Record<string, boolean> }
    scoped.OPENSQUILLA_FEATURES = { ...(scoped.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
  })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/v1/workspace-files/resolve', async route => {
    const sessionKey = route.request().headers()['x-opensquilla-session-key'] || ''
    const { paths } = route.request().postDataJSON() as { paths: string[] }
    resolves.push({ sessionKey, paths })
    if (deferred && sessionKey === SESSION) await wait
    const files = sessionKey === SESSION ? paths.filter(path => buildFixture
      ? path === '_img/build.py'
      : /中文 图\.(svg|png)$/.test(path)).map(path => ({
      requestedPath: path, path, name: path.split('/').pop(), size: path === '_img/build.py' ? BUILD.length
        : path.endsWith('.svg') ? SVG.length : PNG.length,
      mime: path === '_img/build.py' ? 'text/x-python' : path.endsWith('.svg') ? 'image/svg+xml' : 'image/png',
      kind: path === '_img/build.py' || path.endsWith('.svg') ? 'text' : 'image', contentUrl: 'ignored',
    })) : []
    await route.fulfill({ json: { workspaceBinding: 'binding-A', files } }).catch(() => undefined)
  })
  await page.route('**/api/v1/workspace-files/content?**', async route => {
    const url = new URL(route.request().url())
    const path = url.searchParams.get('path') || ''
    reads.push({ sessionKey: route.request().headers()['x-opensquilla-session-key'] || '', path,
      binding: url.searchParams.get('workspaceBinding') || '' })
    await route.fulfill({ body: buildFixture ? BUILD : path.endsWith('.svg') ? SVG : PNG,
      contentType: buildFixture ? 'text/x-python' : path.endsWith('.svg') ? 'image/svg+xml' : 'image/png' })
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
            : buildFixture ? 'Generated workspace file fixture. The source is `_img/build.py`; click it to inspect the read-only file.' : ANSWER }]),
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
  return { resolves, reads, release }
}

test('opens table SVG as text and a Markdown PNG path as an image through authenticated file access', async ({ page }) => {
  const state = await install(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Generated files fixture.' })
  await expect(answer.locator('.workspace-file-link')).toHaveCount(2)
  expect(state.resolves[0].paths).toEqual(['outputs/中文 图.svg', 'outputs/中文 图.png', 'outputs/missing.svg'])
  await answer.getByRole('link', { name: 'Open outputs/中文 图.svg', exact: true }).click()
  const source = page.getByTestId('workspace-file-panel')
  await expect(source).toBeVisible()
  await expect(source.locator('pre')).toContainText(SVG)
  await expect(source.locator('svg[onload], iframe, object, img')).toHaveCount(0)
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).svgExecuted)).toBeUndefined()
  await page.screenshot({ path: 'output/playwright/workspace-file-svg-source.png', fullPage: true })
  await answer.getByRole('button', { name: 'Open outputs/中文 图.svg', exact: true }).first().click()
  await expect(page.getByRole('menu')).toContainText('Copy relative path')
  await page.screenshot({ path: 'output/playwright/workspace-file-svg-menu.png', fullPage: true })
  await page.keyboard.press('Escape')
  await answer.getByRole('link', { name: 'Open outputs/中文 图.png', exact: true }).click()
  const image = page.getByRole('dialog', { name: '中文 图.png', exact: true }).locator('img')
  await expect(image).toBeVisible()
  await expect(image).toHaveAttribute('src', /^blob:/)
  await page.screenshot({ path: 'output/playwright/workspace-file-image.png', fullPage: true })
  expect(state.reads).toEqual([
    { sessionKey: SESSION, path: 'outputs/中文 图.svg', binding: 'binding-A' },
    { sessionKey: SESSION, path: 'outputs/中文 图.png', binding: 'binding-A' },
  ])
  await expect(answer.locator('a[href^="file:"]')).toHaveCount(0)
})

test('opens an inline _img/build.py path in the read-only source Workbench', async ({ page }) => {
  const state = await install(page, false, true)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Generated workspace file fixture.' })
  const link = answer.getByRole('link', { name: 'Open _img/build.py', exact: true })
  await expect(link).toHaveCount(1)
  await expect(link).toContainText('_img/build.py')
  await link.click()
  const source = page.getByTestId('workspace-file-panel')
  await expect(source).toBeVisible()
  await expect(page.getByText('build.py', { exact: true }).last()).toBeVisible()
  await expect(page.getByText(/_img\/build\.py/).last()).toBeVisible()
  await expect(source.locator('.workspace-file__number').first()).toHaveText('1')
  await expect(source.locator('pre')).toContainText('#!/usr/bin/env python3')
  await expect(source.locator('pre')).toContainText("IMG_DIR = '_img/opt'")
  await expect(source.locator('pre')).toContainText('print("build fixture")')
  await expect(page.getByText(/Read-only source/).last()).toBeVisible()
  await page.screenshot({ path: 'output/playwright/workspace-file-source.png', fullPage: true })
  await link.click({ button: 'right' })
  await expect(page.getByRole('menu')).toContainText('Copy relative path')
  await page.keyboard.press('Escape')
  await answer.getByRole('button', { name: 'Open _img/build.py', exact: true }).click()
  const menu = page.getByRole('menu')
  await expect(menu).toContainText('Open _img/build.py in Workbench')
  await expect(menu).toContainText('Copy relative path')
  await expect(menu).toContainText('Copy file contents')
  await expect(menu).toContainText('Download')
  await page.screenshot({ path: 'output/playwright/workspace-file-menu.png', fullPage: true })
  expect(state.resolves[0].paths).toEqual(['_img/build.py'])
  expect(state.reads).toEqual([{ sessionKey: SESSION, path: '_img/build.py', binding: 'binding-A' }])
})

test('late file resolution cannot decorate a different session after SPA navigation', async ({ page }) => {
  const state = await install(page, true)
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
})

test('file actions stay inside the viewport on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await install(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const answer = page.locator('.msg-ai').filter({ hasText: 'Generated files fixture.' })
  await expect(answer.locator('.workspace-file-link')).toHaveCount(2)
  await answer.getByRole('button', { name: 'Open outputs/中文 图.svg', exact: true }).first().click()
  const menu = page.getByRole('menu')
  await expect(menu).toBeVisible()
  const bounds = await menu.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844)
  await page.screenshot({ path: 'output/playwright/workspace-file-mobile.png', fullPage: true })
})
