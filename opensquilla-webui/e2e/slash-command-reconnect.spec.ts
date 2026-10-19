import { expect, test, type WebSocketRoute } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:slash-command-reconnect'
const COMMANDS = [
  { name: '/init', aliases: [], description: 'Initialize project instructions',
    category: 'turn', busy_policy: 'next_turn', presentation: 'turn',
    execution: { action: 'project.init' } },
  { name: '/compact', aliases: [], description: 'Compact conversation context',
    execution: { action: 'sessions.contextCompact' } },
  { name: '/plan', aliases: [], description: 'Plan the next task',
    execution: { action: 'plans.setMode' } },
]

// Real Chromium and the shipped RPC client; every Gateway exchange is synthetic.
test('reopening slash commands after transport reconnect restores init, compact, and plan without a reload', async ({ page }, testInfo) => {
  test.setTimeout(60_000)
  const sockets: WebSocketRoute[] = []
  const requests: Array<{ connection: number; method: string }> = []
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.route('**/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public-assets/opensquilla-mark.png', import.meta.url)),
    contentType: 'image/png',
  }))
  await page.routeWebSocket(/\/ws$/, socket => {
    sockets.push(socket)
    const connection = sockets.length
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as { type: string; id?: string; method?: string }
      if (frame.type === 'ping') {
        socket.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req' || !frame.method) return
      requests.push({ connection, method: frame.method })
      if (frame.method === 'connect') {
        socket.send(helloOkResponse({
          server: { conn_id: `slash-catalog-${connection}` },
          features: { methods: [
            'commands.list_for_surface', 'skills.candidates',
            'sessions.messages.subscribe', 'sessions.messages.snapshot',
            'sessions.messages.hydrate', 'sessions.messages.unsubscribe',
          ] },
        }))
        return
      }
      const payloads: Record<string, unknown> = {
        'commands.list_for_surface': { surface: 'web_chat', commands: COMMANDS },
        'skills.candidates': { generation: connection, candidates: [] },
        'chat.history': chatHistoryPayload(),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'agents.list': { agents: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'onboarding.status': { audioConfigured: true },
        'usage.status': { sessions: [] },
      }
      socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: true,
        payload: payloads[frame.method] ?? {} }))
    })
  })

  let documentId: string | null = null
  let documentIdAfterReconnect: string | null = null
  try {
    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'connected')
    documentId = await page.evaluate(() => {
      const marker = crypto.randomUUID()
      Object.defineProperty(window, '__slashReconnectDocument', { value: marker })
      return marker
    })
    const composer = page.locator('.chat-textarea')
    const menu = page.locator('.chat-slash')
    const expectCommands = async () => {
      for (const command of COMMANDS) {
        await expect(menu.getByRole('option', { name: new RegExp(`^${command.name} `) })).toBeVisible()
      }
    }
    await composer.fill('/')
    await expectCommands()
    await page.screenshot({ path: testInfo.outputPath('catalog-before-reconnect.png') })
    await composer.press('Escape')
    await expect(menu).toHaveCount(0)
    await composer.fill('Draft retained across reconnect')

    // Exercise the RpcClient transport boundary, not a direct store mutation.
    await sockets[0]!.close({ code: 1012, reason: 'Synthetic Gateway restart' })
    await expect.poll(() => sockets.length).toBe(2)
    await expect(page.locator('.topbar')).toHaveAttribute('data-connection-state', 'connected')
    await expect(composer).toHaveText('Draft retained across reconnect')
    documentIdAfterReconnect = await page.evaluate(() =>
      (window as unknown as { __slashReconnectDocument?: string }).__slashReconnectDocument ?? null,
    )
    expect(documentIdAfterReconnect).toBe(documentId)

    await composer.fill('/')
    await expect(menu).toBeVisible()
    await expectCommands()
    await expect.poll(() => requests.filter(request =>
      request.connection === 2 && request.method === 'commands.list_for_surface',
    ).length).toBeGreaterThan(0)
    await page.screenshot({ path: testInfo.outputPath('catalog-restored-after-reconnect.png') })
    expect(requests.filter(request => request.method === 'chat.send')).toEqual([])
    expect(pageErrors).toEqual([])
  } finally {
    await testInfo.attach('synthetic-gateway-reconnect', {
      body: JSON.stringify({ connections: sockets.length, documentId,
        documentIdAfterReconnect, requests, pageErrors }, null, 2),
      contentType: 'application/json',
    })
  }
})
