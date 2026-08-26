import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/chat?session='
const SESSION_KEY = 'agent:main:webchat:e2e-session-routing-image'
const PNG_DATA = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
  'base64',
)

const CAPABILITIES_BY_MODE = {
  direct: {
    image_input: { admission: 'allowed', reason: 'model_vision_supported' },
  },
  router: {
    image_input: { admission: 'allowed', reason: 'router_image_route_available' },
  },
  ensemble: {
    image_input: { admission: 'blocked', reason: 'ensemble_mode_unsupported' },
  },
}

function response(id: string, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

async function installGateway(page: Page) {
  const methods: string[] = []
  const routingSets: Array<Record<string, unknown>> = []
  const chatSends: Array<Record<string, unknown>> = []

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.route('**/api/elevated-mode', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ enabled: false }),
  }))
  await page.route('**/api/system/update', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      current: '0.0.0-e2e',
      latest: null,
      available: false,
      url: null,
      checkedAt: null,
    }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message)) as {
        id?: string
        method?: string
        params?: Record<string, unknown>
        type?: string
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      methods.push(method)
      if (method === 'connect') {
        ws.send(JSON.stringify({
          type: 'hello-ok',
          protocol: 3,
          server: { version: 'e2e', conn_id: 'session-routing-image-gateway' },
          features: {
            methods: [
              'chat.send',
              'models.routing.get',
              'models.routing.set',
              'sessions.routing.get',
              'sessions.routing.set',
            ],
            events: ['models.routing.changed', 'sessions.routing.changed'],
          },
          snapshot: {},
          policy: { tick_interval_ms: 30_000 },
          auth: {
            principal: {
              isOwner: true,
              authState: 'authenticated',
            },
          },
        }))
        return
      }
      if (method === 'sessions.routing.set') {
        routingSets.push(frame.params || {})
        ws.send(response(String(frame.id), {
          sessionKey: SESSION_KEY,
          mode: 'router',
          revision: 3,
          source: 'session',
          initialized: true,
        }))
        return
      }
      if (method === 'models.routing.set') {
        ws.send(response(String(frame.id), {}))
        return
      }
      if (method === 'chat.send') {
        chatSends.push(frame.params || {})
        ws.send(response(String(frame.id), {
          accepted: true,
          session: SESSION_KEY,
          task_id: 'session-routing-image-task',
          stream_seq: 1,
        }))
        return
      }

      const history = [
        { role: 'user', text: 'First question', message_id: 'history-user-1' },
        { role: 'assistant', text: 'First answer', message_id: 'history-assistant-1' },
        { role: 'user', text: 'Second question', message_id: 'history-user-2' },
        { role: 'assistant', text: 'Second answer', message_id: 'history-assistant-2' },
      ]
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': { messages: history, has_more: false, canonical_complete: true },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          llm_ensemble: { enabled: true, selection_mode: 'static_openrouter_b5' },
          permissions: {},
          skills: {},
        },
        'models.routing.get': {
          mode: 'ensemble',
          selection_mode: 'static_openrouter_b5',
          image_input: CAPABILITIES_BY_MODE.ensemble.image_input,
          capabilities_by_mode: CAPABILITIES_BY_MODE,
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.snapshot': {
          key: SESSION_KEY,
          events: [],
          current_stream_seq: 0,
        },
        'sessions.messages.subscribe': {
          key: SESSION_KEY,
          sessionId: 'session-routing-image',
          subscribed: true,
          hydration_complete: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: 'idle',
        },
        'sessions.messages.hydrate': {
          key: SESSION_KEY,
          sessionId: 'session-routing-image',
          hydration_complete: true,
          run_status: 'idle',
        },
        'sessions.routing.get': {
          sessionKey: SESSION_KEY,
          mode: 'ensemble',
          revision: 2,
          source: 'session',
          initialized: true,
        },
        'usage.status': { sessions: [] },
      }
      ws.send(response(String(frame.id), payloads[method] ?? {}))
    })
  })

  return { chatSends, methods, routingSets }
}

test('session Router capability overrides the blocked global Ensemble scalar for images', async ({
  page,
}) => {
  const gateway = await installGateway(page)
  await page.goto(CONTROL_URL + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.chat-textarea')).toBeEditable({ timeout: 10_000 })
  await expect(page.locator('.msg-user')).toHaveCount(2)
  await expect.poll(() => gateway.methods.filter(method => method === 'sessions.routing.get').length)
    .toBeGreaterThan(0)

  const routingButton = page.getByRole('button', {
    name: "This chat's model routing",
    exact: true,
  })
  await expect(routingButton).toHaveClass(/chat-model-routing-btn--llm_ensemble/)
  await routingButton.click()
  await page.getByRole('radio', { name: /AI-powered single-model router/ }).click()

  await expect.poll(() => gateway.routingSets).toEqual([{
    sessionKey: SESSION_KEY,
    mode: 'router',
    expectedRevision: 2,
  }])
  await expect(routingButton).toHaveClass(/chat-model-routing-btn--squilla_router/)

  await page.locator('input[type="file"]').setInputFiles({
    name: 'router-capable.png',
    mimeType: 'image/png',
    buffer: PNG_DATA,
  })
  await page.locator('.chat-textarea').fill('Describe this image once.')
  await expect(page.locator('.attachment-chip')).toContainText('router-capable.png')
  await expect(page.locator('.chat-composer-send-status')).toHaveCount(0)

  const sendButton = page.locator('.chat-send-btn[aria-label="Send"]')
  await expect(sendButton).toBeEnabled()
  await sendButton.click()

  await expect.poll(() => gateway.chatSends.length).toBe(1)
  expect(gateway.chatSends[0]?.message).toBe('Describe this image once.')
  expect(gateway.chatSends[0]?.attachments).toEqual([
    expect.objectContaining({ name: 'router-capable.png', mime: 'image/png' }),
  ])
  expect(gateway.methods).not.toContain('models.routing.set')
  expect(gateway.routingSets).toHaveLength(1)
})
