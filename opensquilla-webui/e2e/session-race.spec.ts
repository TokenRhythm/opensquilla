import { expect, test, type Page } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import { installSidebarFixture } from './support/sidebar-fixture'
import { expectComposerValue } from './utils'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

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

for (const targetHistoryCount of [0, 1, 5]) {
  test(`composer history preserves saved draft after session switch (${targetHistoryCount} target messages)`, async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const savedDraft = 'B saved draft survives history navigation'
    const historyRequests: string[] = []
    await page.route('**/api/**', route => route.fulfill({ json: {} }))
    await installSidebarFixture(page, {
      'sessions.list': {
        sessions: [SESSION_A, SESSION_B].map((key, index) => ({
          key, title: index === 0 ? SESSION_A_TITLE : SESSION_B_TITLE,
          sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct',
          effectiveAgentId: 'main', updatedAt: 200 - index,
          messageCount: index === 0 ? 5 : targetHistoryCount, status: 'ok', runStatus: 'idle',
        })),
        count: 2, ts: 1_800_000_000, has_more: false,
      },
      'chat.history': (params: Record<string, unknown>) => {
        const key = String(params.key || params.sessionKey || '')
        historyRequests.push(key)
        return chatHistoryPayload(Array.from({ length: key === SESSION_A ? 5 : targetHistoryCount }, (_, index) => ({
          role: 'user', text: `${key === SESSION_A ? 'A' : 'B'} request ${index + 1}`,
          message_id: `${key}-user-${index}`, timestamp: 1_800_000_000 + index,
        })))
      },
    })
    await page.addInitScript(({ key, text }) => {
      localStorage.setItem(`opensquilla.chat.draft:${key}`, text)
    }, { key: SESSION_B, text: savedDraft })
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    const composer = page.locator('.chat-textarea')
    await expect(page.locator('.chat-thread')).toContainText('A request 5')
    await composer.press('ArrowUp')
    await expectComposerValue(composer, 'A request 5')

    await page.locator('.sidebar-history-row[data-family="chats"]')
      .filter({ hasText: SESSION_B_TITLE }).locator('.sidebar-history-item').click()
    await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
    await expect.poll(() => historyRequests.includes(SESSION_B)).toBe(true)
    await expect(page.locator('.chat-thread')).not.toContainText('A request 5')
    if (targetHistoryCount) await expect(page.locator('.chat-thread')).toContainText('B request 1')
    await expectComposerValue(composer, savedDraft)
    for (const key of ['ArrowUp', 'ArrowDown']) {
      await composer.press(key)
      await expectComposerValue(composer, savedDraft)
      await expect.poll(() => page.evaluate(key => (
        localStorage.getItem(`opensquilla.chat.draft:${key}`)
      ), SESSION_B)).toBe(savedDraft)
    }
    expect(errors).toEqual([])
  })
}

test('late chat.send response cannot navigate away from the current session', async ({ page }) => {
  let delayedSend: { id: string; sendResponse: (payload: unknown) => void } | null = null

  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
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

        const sessionKey = String(frame.params?.key || frame.params?.sessionKey || '')
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': chatHistoryPayload(),
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
          'sessions.messages.subscribe': sessionMessagesSubscribePayload(sessionKey),
          'sessions.messages.hydrate': sessionMessagesHydratePayload(sessionKey),
          'sessions.messages.snapshot': sessionMessagesSnapshotPayload(sessionKey),
          'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
          'usage.status': { sessions: [] },
        }

        ws.send(wsResponse(String(frame.id), payloads[String(frame.method)] ?? {}))
      } catch {}
    })
  })

  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_A))
  await page.waitForSelector('.topbar[data-connection-state]', { timeout: 10000 })
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
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)
})
