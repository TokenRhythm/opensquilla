import type { Page } from '@playwright/test'
import { helloOkResponse } from './gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './session-read-fixtures'

export const SIDEBAR_SESSIONS = Array.from({ length: 24 }, (_, index) => ({
  key: `agent:main:webchat:e2e-sidebar-${index + 1}`,
  title: `Synthetic task ${String(index + 1).padStart(2, '0')}`,
  sessionKind: 'chat',
  surface: 'webchat',
  conversationKind: 'direct',
  effectiveAgentId: 'main',
  updatedAt: 1_800_000_000 - index,
  messageCount: 2,
  status: 'ok',
  runStatus: 'idle',
}))

export const SIDEBAR_SESSION_KEYS = SIDEBAR_SESSIONS.map(session => session.key)
export const SIDEBAR_ACTIVE_SESSION_KEY = SIDEBAR_SESSION_KEYS[2]!

type RpcFrame = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
  nonce?: string
}

/** Synthetic, offline gateway shared by interaction tests and visual checks. */
export async function installSidebarFixture(page: Page, rpcPayloads: Record<string, unknown> = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      let frame: RpcFrame
      try {
        frame = JSON.parse(String(message)) as RpcFrame
      } catch {
        return
      }
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong', nonce: frame.nonce }))
        return
      }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({
          features: { methods: Object.keys(rpcPayloads) },
          auth: {
            principal: { isOwner: true, authenticated: true, authState: 'authenticated' },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          },
        }))
        return
      }
      const key = String(frame.params?.key || frame.params?.sessionKey || SIDEBAR_ACTIVE_SESSION_KEY)
      const payloads: Record<string, unknown> = {
        'chat.history': chatHistoryPayload([
          {
            role: 'user',
            text: 'Organize the synthetic example tasks for the next review.',
            message_id: `${key}-user`,
            timestamp: '2026-07-22T10:00:00Z',
          },
          {
            role: 'assistant',
            text: 'The example tasks are ready. Drag a task in the sidebar to adjust its order.',
            message_id: `${key}-assistant`,
            timestamp: '2026-07-22T10:00:01Z',
          },
        ]),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {},
          skills: {},
        },
        'onboarding.status': { audioConfigured: false },
        'sandbox.capability.status': { available: false },
        'sessions.list': {
          sessions: SIDEBAR_SESSIONS,
          count: SIDEBAR_SESSIONS.length,
          ts: 1_800_000_000,
          has_more: false,
        },
        'usage.status': { sessions: [] },
      }
      const override = rpcPayloads[String(frame.method)]
      ws.send(JSON.stringify({
        type: 'res',
        id: frame.id,
        ok: true,
        payload: typeof override === 'function' ? override(frame.params || {})
          : override ?? payloads[String(frame.method)] ?? {},
      }))
    })
  })
}
