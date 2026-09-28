import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2e-background-processes'

async function installGateway(page: Page) {
  const calls: { method: string; params: Record<string, unknown> }[] = []
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.routeWebSocket(/\/ws$/, (ws: WebSocketRoute) => {
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      const reply = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      const method = String(frame.method)
      if (method.startsWith('sessions.processes.')) calls.push({ method, params: frame.params })
      switch (method) {
        case 'connect':
          ws.send(helloOkResponse({
            features: { methods: ['sessions.processes.list', 'sessions.processes.log', 'sessions.processes.stop'],
              events: ['session.event.process_completed', 'session.event.done'] },
            auth: { principal: { isOwner: true, scopes: ['operator.read', 'operator.write'] },
              runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } },
          }))
          return
        case 'chat.history':
          reply(chatHistoryPayload([{ role: 'assistant', id: 'finished-answer', text: 'Service health check passed.', timestamp: 1000 }]))
          return
        case 'sessions.messages.subscribe': reply(sessionMessagesSubscribePayload(SESSION_KEY)); return
        case 'sessions.messages.hydrate': reply(sessionMessagesHydratePayload(SESSION_KEY)); return
        case 'sessions.messages.snapshot': reply(sessionMessagesSnapshotPayload(SESSION_KEY)); return
        case 'sessions.list':
          reply({ sessions: [{ key: SESSION_KEY, title: 'Background service', sessionKind: 'chat', surface: 'webchat',
            conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 1000, messageCount: 1, status: 'ok', runStatus: 'idle' }],
            count: 1, ts: 1_800_000_000, has_more: false })
          return
        case 'agents.list': reply({ agents: [] }); return
        case 'commands.list_for_surface': reply({ commands: [] }); return
        case 'models.routing.get': reply({ mode: 'direct' }); return
        default: reply({})
      }
    })
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
  })
  return { calls }
}

test('chat does not render a standalone session-process panel', async ({ page }) => {
  const gateway = await installGateway(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION_KEY)}`)

  await expect(page.getByText('Service health check passed.', { exact: true })).toBeVisible()
  await expect(page.getByTestId('background-processes')).toHaveCount(0)
  expect(gateway.calls).toEqual([])
})
