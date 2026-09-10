import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'

const CONTROL_URL = '/control/'
const SESSION_A = 'agent:main:webchat:e2e-race-a'
const SESSION_B = 'agent:main:webchat:e2e-race-b'
const SESSION_A_TITLE = 'Race Session A'
const SESSION_B_TITLE = 'Race Session B'

function wsResponse(id: string, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

async function readSessionDiag(page: Page): Promise<Array<{ source?: string; to?: string }>> {
  return page.evaluate(() => {
    const helper = (window as unknown as {
      OpenSquillaSessionDiag?: { read?: () => Array<{ source?: string; to?: string }> }
    }).OpenSquillaSessionDiag
    return helper?.read?.() ?? []
  })
}

test('late chat.send response cannot navigate away from the current session', async ({ page }) => {
  let delayedSend: { id: string; sendResponse: (payload: unknown) => void } | null = null

  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))

  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        if (frame.method === 'connect') {
          ws.send(helloOkResponse())
          return
        }
        if (frame.method === 'chat.send') {
          const id = String(frame.id)
          delayedSend = {
            id,
            sendResponse: payload => ws.send(wsResponse(id, payload)),
          }
          return
        }

        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
            permissions: {},
            skills: {},
          },
          'sessions.list': {
            sessions: [
              {
                key: SESSION_B,
                title: SESSION_B_TITLE,
                sessionKind: 'chat',
                surface: 'webchat',
                conversationKind: 'direct',
                effectiveAgentId: 'main',
                updatedAt: 200,
                messageCount: 0,
                status: 'ok',
                runStatus: 'idle',
              },
              {
                key: SESSION_A,
                title: SESSION_A_TITLE,
                sessionKind: 'chat',
                surface: 'webchat',
                conversationKind: 'direct',
                effectiveAgentId: 'main',
                updatedAt: 100,
                messageCount: 0,
                status: 'ok',
                runStatus: 'idle',
              },
            ],
            count: 2,
            ts: 1_800_000_000,
            has_more: false,
          },
          'sessions.messages.subscribe': {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          },
          'usage.status': { sessions: [] },
        }

        ws.send(wsResponse(String(frame.id), payloads[String(frame.method)] ?? {}))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_A))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await expect(page.locator('.chat-textarea')).toBeVisible()

  await page.locator('.chat-textarea').fill('hello from A')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect.poll(() => delayedSend?.id ?? '').not.toBe('')

  await page
    .locator('.sidebar-history-row[data-family="chats"]')
    .filter({ hasText: SESSION_B_TITLE })
    .locator('.sidebar-history-item')
    .click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)

  const send = delayedSend
  if (!send) throw new Error('chat.send was not captured')
  send.sendResponse({
    ok: true,
    sessionKey: SESSION_A,
    status: 'accepted',
  })

  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
  await expect.poll(async () => {
    const diag = await readSessionDiag(page)
    return diag.some(entry => entry.source === 'send.response.stale')
  }).toBe(true)
})
