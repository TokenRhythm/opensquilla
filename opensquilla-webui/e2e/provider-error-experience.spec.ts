import { expect, test } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload, sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const sessionKey = 'agent:main:webchat:synthetic-provider-error'
const turnId = 'synthetic-provider-turn'
const errorId = 'abcdef01'
const rateLimitMessage = 'The model provider is rate-limiting requests. Try again later.'
const rateLimitOutcome = {
  kind: 'failed', reason: '429', error_class: '429', failure_kind: 'rate_limited',
  error_id: errorId, retryable: true,
}

for (const { delivery, width } of [
  { delivery: 'live', width: 1280 }, { delivery: 'task-only', width: 1280 },
  { delivery: 'history', width: 1280 }, { delivery: 'history', width: 390 },
  { delivery: 'timeout-first', width: 1280 }, { delivery: 'timeout-last', width: 1280 },
  { delivery: 'conflict', width: 1280 },
] as const) {
  test(`provider failure keeps plain text through ${delivery} and reload at ${width}px`, async ({ page, context }, testInfo) => {
    const timeout = delivery === 'timeout-first' || delivery === 'timeout-last'
    const conflict = delivery === 'conflict'
    const safeMessage = timeout ? 'The task timed out before it could finish.' : rateLimitMessage
    const outcome = timeout ? {
      ...rateLimitOutcome, kind: 'interrupted', reason: 'timeout', error_class: 'llm_timeout',
      failure_kind: 'transport_transient',
    } : rateLimitOutcome
    const historyErrorId = conflict ? 'abcdef02' : errorId
    const runtimeErrors: string[] = []
    page.on('pageerror', error => runtimeErrors.push(error.message))
    await page.setViewportSize({ width, height: 900 })
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
    await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt' } }))
    await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
    await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
    await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
      path: fileURLToPath(new URL('../public/opensquilla-mark.png', import.meta.url)),
    }))
    let durable = delivery === 'history'
    let failTurn: (() => void) | undefined
    let disconnect: (() => void) | undefined
    let connections = 0
    await page.routeWebSocket(/\/ws$/, ws => {
      connections++
      disconnect = () => ws.close()
      const respond = (id: unknown, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
      const emit = (event: string, payload: Record<string, unknown>) => ws.send(JSON.stringify({
        type: 'event', event, payload: { key: sessionKey, task_id: turnId, turn_id: turnId, ...payload },
      }))
      ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
      ws.onMessage(raw => {
        const frame = JSON.parse(String(raw))
        if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
        if (frame.type !== 'req') return
        if (frame.method === 'connect') { ws.send(helloOkResponse()); return }
        if (frame.method === 'chat.history') {
          respond(frame.id, chatHistoryPayload(durable ? [{
            role: 'user', text: 'Synthetic request', message_id: 'synthetic-user',
            timestamp: '2026-01-01T09:00:00Z', turn_context: { turn_id: turnId },
          }, {
            role: 'assistant', text: 'Partial answer', message_id: 'synthetic-assistant',
            timestamp: '2026-01-01T09:00:01Z', turn_context: { turn_id: turnId },
          }] : [], {
            turn_outcomes: durable ? [{
              turn_id: turnId, task_id: turnId, status: timeout ? 'timeout' : 'failed',
              outcome: { ...outcome, error_id: historyErrorId },
              terminal_message: `${safeMessage} (ref: ${historyErrorId})`,
            }] : [],
          }))
          return
        }
        if (frame.method === 'chat.send') {
          respond(frame.id, {
            sessionKey, status: 'accepted', accepted: true, task_id: turnId, turn_id: turnId,
            message_id: 'synthetic-user',
          })
          failTurn = () => {
            emit('session.event.text_delta', { text: 'Partial answer', stream_seq: 1 })
            const failure = { code: outcome.error_class, message: safeMessage, terminal_message: safeMessage, turn_outcome: outcome }
            if (delivery === 'timeout-first') {
              emit('task.timeout', { ...failure, terminal_reason: 'timeout', stream_seq: 2 })
              emit('session.event.error', { ...failure, error_id: errorId, stream_seq: 3 })
            } else {
              if (delivery !== 'task-only') emit('session.event.error', { ...failure, error_id: errorId, stream_seq: 2 })
              emit(timeout ? 'task.timeout' : 'task.failed', { ...failure, terminal_reason: timeout ? 'timeout' : 'error', stream_seq: 3 })
            }
            durable = true
          }
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] }, 'commands.list_for_surface': { commands: [] },
          'config.get': { squilla_router: { enabled: false }, permissions: {}, skills: {} },
          'models.routing.get': { mode: 'direct' },
          'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
          'sessions.messages.subscribe': sessionMessagesSubscribePayload(sessionKey),
          'sessions.messages.hydrate': sessionMessagesHydratePayload(sessionKey),
          'sessions.messages.snapshot': sessionMessagesSnapshotPayload(sessionKey),
          'usage.status': { sessions: [] },
        }
        respond(frame.id, payloads[String(frame.method)] ?? {})
      })
    })
    await page.goto('/control/chat?session=' + encodeURIComponent(sessionKey))
    await expect(page.locator('.conn-pill.connected')).toBeVisible()
    if (delivery !== 'history') {
      const composer = page.getByRole('textbox', { name: 'Message to send' })
      await composer.fill('Synthetic request')
      await composer.press('Enter')
      await expect.poll(() => Boolean(failTurn)).toBe(true)
      failTurn!()
    }
    const card = page.locator('.msg-error')
    await expect(card).toHaveCount(1)
    await expect(card).toHaveCSS('border-top-width', '0px')
    await expect(card).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    await expect(card).toHaveCSS('box-shadow', 'none')
    await expect(card.locator('svg, code, strong, h1, h2, h3')).toHaveCount(0)
    await expect(card).not.toContainText('Turn failed')
    await expect(card).toContainText(safeMessage)
    if (!conflict) await expect(card).toContainText(errorId)
    await expect(page.locator('.msg-ai')).toContainText('Partial answer')
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
    await expect(card.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(0)
    if (conflict) {
      await expect(card.getByRole('button', { name: 'Copy diagnostic ID' })).toHaveCount(0)
    } else {
      await card.getByRole('button', { name: 'Copy diagnostic ID' }).click()
      await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(errorId)
    }
    if (timeout) await expect(page.locator('.msg-ai')).toContainText('Timed out')
    if (delivery !== 'history') {
      const before = connections
      disconnect!()
      await expect.poll(() => connections).toBeGreaterThan(before)
      await expect(page.locator('.conn-pill.connected')).toBeVisible()
      await expect(card).toHaveCount(1)
      await expect(card).toContainText(safeMessage)
      if (conflict) await expect(card.getByRole('button', { name: 'Copy diagnostic ID' })).toHaveCount(0)
      if (timeout) await expect(page.locator('.msg-ai')).toContainText('Timed out')
    }
    await page.reload()
    await expect(card).toHaveCount(1)
    await expect(card).toContainText(safeMessage)
    await expect(card).toContainText('The answer above was preserved, but this turn ended with an error.')
    await expect(card).toContainText(historyErrorId)
    await expect(page.locator('.msg-ai')).toContainText('Partial answer')
    if (delivery === 'history') await page.screenshot({ path: testInfo.outputPath('provider-error-history.png') })
    expect(runtimeErrors).toEqual([])
  })
}
