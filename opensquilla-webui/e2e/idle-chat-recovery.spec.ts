import { expect, test } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:e2e-idle-recovery'
const DRAFT = 'Synthetic follow-up preserved while the client is idle.'

for (const halfOpen of [false, true]) {
  test(`sends the retained draft after idle resume with a ${halfOpen ? 'half-open' : 'healthy'} connection`, async ({ page }) => {
    let connectionCount = 0
    let loseFirstConnection = false
    const subscriptions: number[] = []
    const sends: Array<Record<string, unknown>> = []

    // A suspended renderer's timers resume after a large elapsed-time jump.
    // This is a deterministic lifecycle regression, not physical sleep proof.
    await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') })
    await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
    await page.route('**/api/**', route => route.fulfill({ json: {} }))
    await page.route('**/api/approvals', route => route.fulfill({
      json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
    }))
    await page.routeWebSocket(/\/ws$/, socket => {
      const connection = ++connectionCount
      socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
      socket.onMessage(raw => {
        const frame = JSON.parse(String(raw))
        // Leave the socket physically open but drop all traffic on its peer.
        if (connection === 1 && loseFirstConnection) return
        if (frame.type === 'ping') {
          socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce }))
          return
        }
        if (frame.type !== 'req') return
        if (frame.method === 'connect') {
          socket.send(helloOkResponse({
            server: { conn_id: `idle-connection-${connection}` },
            policy: { transport_probe_nonce: true },
          }))
          return
        }
        if (frame.method === 'sessions.messages.subscribe') subscriptions.push(connection)
        if (frame.method === 'chat.send') sends.push(frame.params)
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {},
          },
          'models.routing.get': { mode: 'direct' },
          'sessions.list': {
            sessions: [{
              key: SESSION, title: 'Synthetic idle recovery', sessionKind: 'chat',
              surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main',
              updatedAt: 100, messageCount: 0, status: 'ok', runStatus: 'idle',
            }], count: 1, ts: 1_800_000_000, has_more: false,
          },
          'chat.history': chatHistoryPayload([]),
          'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION),
          'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION),
          'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION),
          'sessions.messages.unsubscribe': { subscribed: false },
          'sessions.subscribe': { subscribed: true },
          'usage.status': { sessions: [] },
          'chat.send': { accepted: true, session: SESSION, task_id: 'idle-follow-up' },
        }
        socket.send(JSON.stringify({
          type: 'res', id: frame.id, ok: true, payload: payloads[frame.method] ?? {},
        }))
      })
    })

    await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
    const input = page.locator('.chat-textarea')
    const send = page.locator('.chat-send-btn[aria-label="Send"]')
    await expect(send).toBeEnabled()
    await input.fill(DRAFT)
    const initialUrl = page.url()
    await page.evaluate(() => {
      Object.defineProperty(window, '__idleRecoveryComposer', {
        value: document.querySelector('.chat-textarea'),
      })
    })

    loseFirstConnection = halfOpen
    await page.clock.fastForward(2 * 60 * 60 * 1_000)
    await page.evaluate(() => document.dispatchEvent(new Event('resume')))
    // Let real network messages interleave with every virtual timer step.
    // Product probe/retry deadlines remain unchanged.
    for (let elapsed = 0; elapsed < 65_000; elapsed += 1_000) {
      await page.clock.runFor(1_000)
      if (halfOpen && connectionCount > 1 && await send.isEnabled()) break
      if (!halfOpen && elapsed >= 6_000) break
    }

    expect(connectionCount).toBe(halfOpen ? 2 : 1)
    expect(subscriptions).toContain(connectionCount)
    expect(sends).toHaveLength(0)
    expect(page.url()).toBe(initialUrl)
    expect(await page.evaluate(() => (
      (window as unknown as { __idleRecoveryComposer: Element }).__idleRecoveryComposer
        === document.querySelector('.chat-textarea')
    ))).toBe(true)
    await expect(input).toHaveValue(DRAFT)
    await expect(send).toBeEnabled()
    await input.press('Enter')
    await expect.poll(() => sends.length).toBe(1)
    expect(sends[0]).toMatchObject({ sessionKey: SESSION, message: DRAFT })
    await expect(input).toHaveValue('')
    await page.clock.runFor(2_000)
    expect(sends).toHaveLength(1)
  })
}
