import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload } from './support/session-read-fixtures'
import { expectComposerValue } from './utils'

const A = 'agent:main:webchat:launcher-a'
const B = 'agent:main:webchat:launcher-b'
type Wire = Record<string, unknown>

async function installLauncherGateway(page: Page, projectA: boolean, projectB = projectA) {
  const requests: Array<{ method: string; key: string; params?: Wire }> = []
  const materializedProjects = new Set<string>()
  let holdMetadata = true
  let holdHistory = true
  const metadataReplies: Array<() => void> = []
  const historyReplies: Array<() => void> = []
  const workspace = { id: 'launcher-project', name: 'Launcher project', path: 'C:/fixtures/launcher',
    available: true, removed: false, taskCount: 2, pinned: false }
  const project = (key: string) => materializedProjects.has(key) || (key === A ? projectA : key === B && projectB)
  const metadata = (key: string) => ({ workspaceId: project(key) ? workspace.id : null,
    projectWorkspace: project(key) ? workspace : null })
  const history = (key: string) => chatHistoryPayload([A, B].includes(key) ? [
    { id: `${key}-user`, role: 'user', text: `Question ${key === A ? 'A' : 'B'}`, timestamp: 1_800_000_000 },
    { id: `${key}-answer`, role: 'assistant', text: `Answer ${key === A ? 'A' : 'B'} is ready.`, timestamp: 1_800_000_001 },
  ] : [])
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-theme', 'dark')
    window.OPENSQUILLA_FEATURES = { ...(window.OPENSQUILLA_FEATURES || {}), artifactWorkbench: true }
  })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] } }))
  await page.route('**/api/v1/workspace-files/list?**', route => route.fulfill({ json: {
    workspaceBinding: 'launcher-project', relativePath: '', nextCursor: null, hasMore: false, truncated: false,
    entries: [{ name: 'welcome.md', path: 'welcome.md', type: 'file', kind: 'text', mime: 'text/markdown', size: 10, mtimeNs: '123' }],
  } }))
  await page.route('**/api/v1/workspace-files/root', route => route.fulfill({ json: {
    kind: 'workspace-root', name: workspace.name, path: workspace.path, workspaceBinding: 'a'.repeat(64),
  } }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as { type: string; id: string; method: string; params?: Wire; nonce?: string }
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      const key = String(frame.params?.key || frame.params?.sessionKey || frame.params?.session_key || A)
      requests.push({ method: frame.method, key, params: frame.params })
      const respond = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: ['sessions.messages.subscribe', 'sessions.messages.hydrate',
          'sessions.messages.snapshot', 'sessions.messages.unsubscribe', 'sessions.sidechat.create',
          'sessions.sidechat.close', 'sessions.create', 'sessions.routing.get', 'workspaces.list', 'workspaces.open',
          'terminal.create', 'terminal.resize', 'terminal.close'] },
        auth: { principal: { capabilities: ['chat.read', 'chat.write', 'host.execute'] },
          runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } } }))
        return
      }
      if (frame.method === 'sessions.create') {
        if (frame.params?.workspaceId === workspace.id) materializedProjects.add(key)
        respond({ key, sessionId: `fixture-${key}` })
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'onboarding.status': { audioConfigured: false }, 'usage.status': { sessions: [] },
        'workspaces.list': { workspaces: projectA || projectB ? [workspace] : [] },
        'sessions.list': { sessions: [A, B].map((sessionKey, index) => ({ key: sessionKey,
          title: `Launcher task ${index === 0 ? 'A' : 'B'}`, sessionKind: 'chat', surface: 'webchat',
          conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 1_800_000_000 - index,
          messageCount: 2, status: 'ok', runStatus: 'idle', workspaceId: project(sessionKey) ? workspace.id : null,
        })), count: 2, ts: 1_800_000_002, has_more: false },
        'chat.history': history(key),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, metadata(key)),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata(key)),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.routing.get': { key, mode: 'direct', revision: 0, source: 'default', initialized: true, appliesTo: 'next_accepted_turn' },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      const reply = () => respond(payloads[frame.method] ?? {})
      if (key === B && frame.method === 'chat.history' && holdHistory) { historyReplies.push(reply); return }
      if (key === B && ['sessions.messages.subscribe', 'sessions.messages.hydrate'].includes(frame.method) && holdMetadata) {
        metadataReplies.push(reply); return
      }
      reply()
    })
  })
  return { requests,
    releaseMetadata: () => { holdMetadata = false; metadataReplies.splice(0).forEach(reply => reply()) },
    releaseHistory: () => { holdHistory = false; historyReplies.splice(0).forEach(reply => reply()) },
  }
}

async function openLauncher(page: Page, count: number) {
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto(`/control/chat?session=${A}`)
  await expect(page.locator('#app-main .chat')).toContainText('Answer A is ready.')
  await page.getByTestId('topbar-workbench-toggle').click()
  const host = page.getByTestId('workbench-host')
  const cards = page.getByTestId('workbench-start-panel').locator('[data-panel-kind]')
  await expect(cards).toHaveCount(count)
  await expect(cards.first()).toBeVisible()
  await expect(page.getByTestId('workbench-start-panel').locator('[data-panel-kind="assistant-chat"]')).toBeEnabled()
  await expect.poll(() => host.evaluate(element => element.getAnimations().every(animation =>
    animation.playState === 'finished' || animation.playState === 'idle'))).toBe(true)
  const boxes = await cards.evaluateAll(elements => elements.map(element => {
    const rect = element.getBoundingClientRect()
    return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height }
  }))
  expect(boxes[0]!.width).toBeLessThanOrEqual(380)
  for (const [index, box] of boxes.entries()) {
    expect(box.height).toBeGreaterThanOrEqual(68)
    expect(Math.abs(box.left - boxes[0]!.left)).toBeLessThan(1)
    expect(Math.abs(box.width - boxes[0]!.width)).toBeLessThan(1)
    if (index > 0) expect(box.top).toBeGreaterThanOrEqual(boxes[index - 1]!.bottom)
  }
  return { host, cards }
}

for (const withProject of [false, true]) {
  test(`launcher keeps ${withProject ? 4 : 3} vertical cards while sidebar session history and workspace resolve`, async ({ page }, info) => {
    const gateway = await installLauncherGateway(page, withProject)
    const { host, cards } = await openLauncher(page, withProject ? 4 : 3)
    // Observe the real App across every rendered frame; no store injection,
    // readiness delay, or synthetic event supplies the navigation state.
    const probe = await host.evaluateHandle(element => {
      const original = element
      const snapshots: Array<{ sameHost: boolean; visible: boolean; background: string; box: number[]; cards: number[][] }> = []
      let running = true
      const sample = () => {
        const current = document.querySelector('[data-testid="workbench-host"]')
        const style = getComputedStyle(original)
        const rect = original.getBoundingClientRect()
        snapshots.push({ sameHost: current === original && original.isConnected,
          visible: style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) === 1,
          background: style.backgroundColor, box: [rect.x, rect.y, rect.width, rect.height],
          cards: Array.from(original.querySelectorAll('[data-testid="workbench-start-panel"] [data-panel-kind]')).map(card => {
            const box = card.getBoundingClientRect(); return [box.x, box.y, box.width, box.height]
          }) })
      }
      const frame = () => { if (running) { sample(); requestAnimationFrame(frame) } }
      sample(); requestAnimationFrame(frame)
      return { stop: () => { sample(); running = false; return snapshots } }
    })
    await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
    await expect.poll(() => gateway.requests.some(request => request.key === B && request.method === 'chat.history')).toBe(true)
    await expect.poll(() => gateway.requests.some(request => request.key === B && request.method === 'sessions.messages.subscribe')).toBe(true)
    await expect(cards).toHaveCount(withProject ? 4 : 3)
    for (const card of await cards.all()) await expect(card).toBeDisabled()
    gateway.releaseMetadata()
    await expect(page.locator('#app-main .chat-thread')).toHaveAttribute('data-session-key', B)
    for (const card of await cards.all()) await expect(card).toBeDisabled()
    await expect(page.getByTestId('workbench-start-panel')).toHaveAttribute('aria-busy', 'true')
    gateway.releaseHistory()
    await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
    await expect(cards).toHaveCount(withProject ? 4 : 3)
    for (const kind of ['terminal', 'assistant-chat', ...(withProject ? ['workspace-files'] : [])]) {
      await expect(host.locator(`[data-testid="workbench-start-panel"] [data-panel-kind="${kind}"]`)).toBeEnabled()
    }
    const snapshots = await probe.evaluate(value => value.stop())
    await probe.dispose()
    await info.attach('launcher-pending-frames', { body: JSON.stringify(snapshots, null, 2), contentType: 'application/json' })
    expect(snapshots.length).toBeGreaterThan(1)
    for (const snapshot of snapshots) {
      expect(snapshot.sameHost).toBe(true)
      expect(snapshot.visible).toBe(true)
      expect(snapshot.background).toBe(snapshots[0]!.background)
      expect(snapshot.cards).toHaveLength(withProject ? 4 : 3)
      for (const [index, value] of snapshot.box.entries()) expect(Math.abs(value - snapshots[0]!.box[index]!)).toBeLessThan(1)
      for (const [index, box] of snapshot.cards.entries()) {
        for (const [coordinate, value] of box.entries()) expect(Math.abs(value - snapshots[0]!.cards[index]![coordinate]!)).toBeLessThan(1)
      }
    }
    // Pure Web deliberately does not acquire the native browser capability.
    await expect(host.locator('[data-testid="workbench-start-panel"] [data-panel-kind="browser"]')).toBeDisabled()
    expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)/.test(request.method))).toHaveLength(0)
  })
}

test('resolved no-project session removes only its workspace entry and side keeps the launcher canvas', async ({ page }) => {
  const gateway = await installLauncherGateway(page, true, false)
  const { host, cards } = await openLauncher(page, 4)
  const background = await host.evaluate(element => getComputedStyle(element).backgroundColor)
  await page.getByRole('button', { name: 'Launcher task B', exact: true }).click()
  await expect.poll(() => gateway.requests.some(request => request.key === B && request.method === 'chat.history')).toBe(true)
  await expect(cards).toHaveCount(4)
  gateway.releaseMetadata()
  gateway.releaseHistory()
  await expect(page.locator('#app-main .chat')).toContainText('Answer B is ready.')
  await expect(cards).toHaveCount(3)
  await expect(host.locator('[data-testid="workbench-start-panel"] [data-panel-kind="workspace-files"]')).toHaveCount(0)
  await host.locator('[data-testid="workbench-start-panel"] [data-panel-kind="assistant-chat"]').click()
  await expect(page.getByTestId('assistant-chat-panel')).toBeVisible()
  expect(await host.evaluate(element => getComputedStyle(element).backgroundColor)).toBe(background)
  expect(gateway.requests.filter(request => /sessions.sidechat.(create|close)/.test(request.method))).toHaveLength(0)
})

test('project draft materialization through Files does not leave the reopened panel menu preparing', async ({ page }) => {
  const gateway = await installLauncherGateway(page, true)
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.goto('/control/chat/new?agent=main&project=launcher-project')
  await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'connected')
  const composer = page.locator('#app-main .chat-textarea')
  await composer.fill('Keep the project draft while opening files')
  const draftKey = await page.locator('#app-main .chat-thread').getAttribute('data-session-key')
  expect(draftKey).toBeTruthy()
  await page.getByTestId('topbar-workbench-toggle').click()
  const launcher = page.getByTestId('workbench-start-panel')
  await expect(launcher.locator('[data-panel-kind="assistant-chat"]')).toHaveCount(0)
  await launcher.locator('[data-panel-kind="workspace-files"]').click()
  await expect(page.getByTestId('workspace-files-panel')).toContainText('welcome.md')
  expect(new URL(page.url()).searchParams.get('session')).toBe(draftKey)
  await expectComposerValue(composer, 'Keep the project draft while opening files')
  expect(gateway.requests.filter(request => request.method === 'sessions.create')).toHaveLength(1)
  expect(gateway.requests.find(request => request.method === 'sessions.create')?.params).toMatchObject({
    session_key: draftKey, workspaceId: 'launcher-project',
  })
  await page.getByTestId('workbench-new-panel-trigger').click()
  const menu = page.getByTestId('workbench-new-panel-menu-list')
  await expect(menu).toBeVisible()
  await expect(menu.locator('[data-panel-kind="workspace-files"]')).toBeEnabled()
  await expect(menu.locator('[data-panel-kind="terminal"]')).toBeEnabled()
  await expect(menu.locator('[data-panel-kind="assistant-chat"]')).toHaveCount(0)
  expect(gateway.requests.filter(request => request.key === draftKey && request.method === 'chat.history')).toHaveLength(0)
  expect(gateway.requests.filter(request => ['chat.send', 'sessions.sidechat.create'].includes(request.method))).toHaveLength(0)
})
