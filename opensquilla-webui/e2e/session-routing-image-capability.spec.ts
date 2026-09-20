import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

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

async function installGateway(page: Page, ensembleImageBlockReason = 'ensemble_mode_unsupported') {
  const methods: string[] = []
  const routingSets: Array<Record<string, unknown>> = []
  const chatSends: Array<Record<string, unknown>> = []
  const capabilitiesByMode = {
    ...CAPABILITIES_BY_MODE,
    ensemble: {
      image_input: {
        ...CAPABILITIES_BY_MODE.ensemble.image_input,
        reason: ensembleImageBlockReason,
      },
    },
  }
  const sessionRouting = {
    sessionKey: SESSION_KEY,
    mode: 'ensemble',
    revision: 2,
    source: 'session',
    initialized: true,
    appliesTo: 'next_accepted_turn',
  }

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
        ws.send(helloOkResponse({
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
        Object.assign(sessionRouting, { mode: 'router', revision: 3 })
        ws.send(response(String(frame.id), sessionRouting))
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
        'chat.history': chatHistoryPayload(history),
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
          image_input: capabilitiesByMode.ensemble.image_input,
          capabilities_by_mode: capabilitiesByMode,
        },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION_KEY),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION_KEY, {
          routing: sessionRouting,
        }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION_KEY, {
          routing: sessionRouting,
        }),
        'sessions.routing.get': sessionRouting,
        'usage.status': { sessions: [] },
      }
      ws.send(response(String(frame.id), payloads[method] ?? {}))
    })
  })

  return { chatSends, methods, routingSets }
}

for (const scenario of [
  {
    name: 'session Router capability overrides the blocked global Ensemble scalar for images',
    reason: 'ensemble_mode_unsupported',
    hardBlocked: false,
  },
  {
    name: 'hard image admission block appears in the send tooltip until the session switches to Router',
    reason: 'attachment_policy_denied',
    hardBlocked: true,
  },
]) {
  test(scenario.name, async ({
    page,
  }) => {
    const gateway = await installGateway(page, scenario.reason)
    await page.goto(CONTROL_URL + encodeURIComponent(SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.chat-textarea')).toBeEditable({ timeout: 10_000 })
    await expect(page.locator('.msg-user')).toHaveCount(2)
    await expect.poll(() => gateway.methods.filter(method => method === 'sessions.routing.get').length)
      .toBeGreaterThan(0)

    const routingButton = page.getByRole('button', {
      name: "Models & routing",
      exact: true,
    })
    await expect(routingButton).toHaveClass(/chat-model-routing-btn--llm_ensemble/)
    await page.locator('input[type="file"]').setInputFiles({
      name: 'router-capable.png',
      mimeType: 'image/png',
      buffer: PNG_DATA,
    })
    await page.locator('.chat-textarea').fill('Describe this image once.')
    await expect(page.locator('.attachment-chip')).toContainText('router-capable.png')
    const sendButton = page.locator('.chat-send-btn[aria-label="Send"]')
    const sendTooltip = page.locator('.chat-send-tooltip')
    await expect(page.locator('.chat-composer-send-status')).toHaveCount(0)
    if (scenario.hardBlocked) {
      await expect(sendButton).toBeDisabled()
      await expect(sendTooltip).toBeHidden()
      await page.locator('.chat-send-control').hover()
      await expect(sendTooltip).toBeVisible()
      await expect(sendTooltip).toContainText('image input')
    } else {
      await expect(sendTooltip).toHaveCount(0)
      await expect(sendButton).toBeEnabled()
    }
    expect(gateway.chatSends).toHaveLength(0)
    await routingButton.click()
    await page.getByRole('menuitemradio', { name: 'Intelligent model routing', exact: true }).click()

    await expect.poll(() => gateway.routingSets).toEqual([{
      sessionKey: SESSION_KEY,
      mode: 'router',
      expectedRevision: 2,
    }])
    await expect(routingButton).toHaveClass(/chat-model-routing-btn--squilla_router/)

    await expect(page.locator('.chat-composer-send-status')).toHaveCount(0)
    await expect(sendTooltip).toHaveCount(0)

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
}
