import { expect, test, type Locator, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2e-fallback-model-transition'
const TURN_ID = 'turn-e2e-fallback-model-transition'
const REQUEST = 'Complete the synthetic fallback request.'
const ANSWER = 'The fallback request completed.'
const BASE_TIME = 1_800_000_000_000
const MODEL_A = 'model-a'
const MODEL_B = 'model-b'
const MODEL_C = 'model-c'

type WireObject = Record<string, unknown>
type StreamEvent = { event: string; payload: WireObject }

// Literal copy checks the user-facing contract without importing production
// dictionaries or the presentation helpers being tested.
const trying = (transition: string) => `Trying a backup model · ${transition}`
const tried = (transition: string) => `Tried a backup model · ${transition}`
const transitionAB = `${MODEL_A} → ${MODEL_B}`
const transitionBC = `${MODEL_B} → ${MODEL_C}`

test.use({ video: 'on' })
test.setTimeout(60_000)

async function prepareGateway(page: Page, locale = 'en') {
  const runtimeErrors: string[] = []
  const clockOffset = BASE_TIME - Date.now()
  const now = () => Date.now() + clockOffset
  page.on('pageerror', error => runtimeErrors.push(error.message))
  await page.addInitScript(({ clockOffset, locale }) => {
    // Keep the clock advancing: the live renderer coalesces updates by elapsed
    // time, so a fully frozen Date.now would prevent later phases from flushing.
    const realNow = Date.now.bind(Date)
    Date.now = () => realNow() + clockOffset
    localStorage.setItem('opensquilla-locale', locale)
  }, { clockOffset, locale })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt' } }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    path: fileURLToPath(new URL('../public-assets/opensquilla-mark.png', import.meta.url)),
  }))

  let accepted = false
  let settled = false
  let sends = 0
  let connections = 0
  let sequence = 0
  let answerOrder = 0
  let turnStartedAt = BASE_TIME
  let finishedAt = BASE_TIME
  let answerAt = BASE_TIME
  let generation = 'e2e-fallback-generation-a'
  let emit: ((frame: StreamEvent) => void) | undefined
  let disconnect: (() => void) | undefined
  const events: StreamEvent[] = []
  const phases: StreamEvent[] = []

  function publish(name: string, payload: WireObject, remember = true): StreamEvent {
    const frame = {
      event: name,
      payload: {
        key: SESSION_KEY, session_key: SESSION_KEY, task_id: TURN_ID, turn_id: TURN_ID,
        stream_seq: ++sequence, stream_generation: generation,
        emitted_at: now(),
        ...payload,
      },
    }
    if (remember) events.push(frame)
    emit!(frame)
    return frame
  }

  function activity(phase: string, activityId: string, models: WireObject = {}) {
    const frame = publish('session.event.provider_activity', {
      schema_version: 1, activity_id: activityId, phase,
      reason: phase === 'requesting' ? 'initial' : 'rate_limited',
      started_at: now(), heartbeat: false,
      ...models,
    })
    phases.push(frame)
    return frame
  }

  function history() {
    const messages: WireObject[] = accepted ? [{
      role: 'user', text: REQUEST, message_id: 'fallback-user',
      timestamp: turnStartedAt, turn_context: { turn_id: TURN_ID },
    }] : []
    if (settled) messages.push({
      role: 'assistant', text: ANSWER, message_id: 'fallback-assistant',
      timestamp: finishedAt, turn_context: { turn_id: TURN_ID },
    })
    return chatHistoryPayload(messages, {
      turn_outcomes: settled ? [{
        turn_id: TURN_ID, task_id: TURN_ID, status: 'succeeded',
        started_at: turnStartedAt, finished_at: finishedAt,
        outcome: { kind: 'completed' },
        activity_snapshot: {
          version: 2, task_id: TURN_ID, turn_id: TURN_ID, complete: true,
          reasoning_utf16_length: 0,
          entries: [...phases.map((frame, index) => ({
            type: 'phase', id: `provider:${frame.payload.phase}:${frame.payload.stream_seq}`,
            order: frame.payload.stream_seq, kind: 'provider', phase: frame.payload.phase,
            at: frame.payload.started_at,
            ended_at: phases[index + 1]?.payload.started_at ?? answerAt,
            reason: frame.payload.reason,
            ...(frame.payload.model ? { model: frame.payload.model } : {}),
            ...(frame.payload.from_model ? { from_model: frame.payload.from_model } : {}),
          })), {
            type: 'phase', id: `write:1:${answerOrder}`, order: answerOrder,
            kind: 'write', phase: 'writing', round: 1,
            at: answerAt, ended_at: finishedAt,
          }, {
            type: 'segment', id: 'text:0', order: answerOrder,
            segment_type: 'text', text_index: 0, text_utf16_length: ANSWER.length,
            at: answerAt, ended_at: finishedAt,
          }],
        },
      }] : [],
    })
  }

  await page.routeWebSocket(/\/ws$/, ws => {
    connections++
    emit = frame => ws.send(JSON.stringify({ type: 'event', ...frame }))
    disconnect = () => ws.close()
    const respond = (id: unknown, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({
          policy: { concurrent_history_reads: true },
          features: {
            methods: ['sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot'],
            events: ['session.event.provider_activity', 'session.event.text_delta', 'session.event.done'],
          },
        }))
        return
      }
      if (frame.method === 'chat.history') { respond(frame.id, history()); return }
      if (frame.method === 'chat.send') {
        sends++
        accepted = true
        turnStartedAt = now()
        respond(frame.id, {
          sessionKey: SESSION_KEY, status: 'accepted', accepted: true,
          task_id: TURN_ID, turn_id: TURN_ID, message_id: 'fallback-user',
        })
        publish('task.running', { status: 'running' }, false)
        publish('session.event.state_change', { to_state: 'thinking' })
        return
      }
      const active = accepted && !settled
      const metadata = {
        stream_generation: generation,
        current_stream_seq: active ? sequence : 0,
        run_status: active ? 'running' : 'idle',
        active_task: active ? {
          task_id: TURN_ID, turn_id: TURN_ID, session_key: SESSION_KEY, status: 'running',
        } : null,
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'sessions.list': { sessions: [], count: 0, ts: BASE_TIME / 1000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION_KEY, metadata),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION_KEY, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION_KEY, {
          ...metadata, task_id: active ? TURN_ID : null, events: active ? events : [],
        }),
        'usage.status': { sessions: [] },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
      }
      respond(frame.id, payloads[String(frame.method)] ?? {})
    })
  })

  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  return {
    runtimeErrors,
    sends: () => sends,
    async start() {
      await page.locator('.chat-textarea').fill(REQUEST)
      await page.locator('.chat-textarea').press('Enter')
      await expect.poll(() => accepted).toBe(true)
      await expect(page.locator('.assistant-activity--live')).toBeVisible()
      activity('requesting', 'request-model-a', { model: MODEL_A })
      const activityPanel = await openActivity(page)
      await expect(activityPanel.locator('.assistant-activity-status__row').filter({ hasText: locale === 'en' ? 'Waiting for model' : '等待模型' })).toBeVisible()
    },
    fallback(activityId: string, model: string, fromModel?: string) {
      return activity('fallback', activityId, {
        model, ...(fromModel ? { from_model: fromModel } : {}),
      })
    },
    duplicate(frame: StreamEvent) { emit!(frame) },
    heartbeat(frame: StreamEvent) {
      const { key: _key, session_key: _session, task_id: _task, turn_id: _turn,
        stream_seq: _seq, stream_generation: _generation, emitted_at: _emitted,
        ...payload } = frame.payload
      publish(frame.event, { ...payload, heartbeat: true })
    },
    async reconnect() {
      const before = connections
      disconnect!()
      await expect.poll(() => connections).toBeGreaterThan(before)
      await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
    },
    succeed() {
      settled = true
      answerAt = now()
      answerOrder = Number(publish('session.event.text_delta', { text: ANSWER, presentation: 'answer' }).payload.stream_seq)
      publish('session.event.done', { final_text: ANSWER })
      finishedAt = now()
      publish('task.succeeded', { terminal_reason: 'completed' }, false)
    },
    restart() { generation = 'e2e-fallback-generation-b' },
  }
}

async function openActivity(page: Page): Promise<Locator> {
  const activity = page.getByTestId('assistant-activity').last()
  await expect(activity).toBeVisible()
  const toggle = activity.locator('button').first()
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  return activity
}

async function expectFallbackRows(page: Page, expected: string[], live = false) {
  const activity = await openActivity(page)
  const rows = activity.locator('.assistant-activity-status__row').filter({ hasText: /(?:Trying|Tried) a backup model/ })
  await expect(rows).toHaveCount(expected.length)
  // Phase durations may follow the copy; they must not change the transition.
  await expect(rows).toContainText(expected)
  if (live) await expect(rows.last()).toHaveClass(/assistant-activity-status__row--current/)
  else await expect(activity.locator('.assistant-activity-status__row--current').filter({ hasText: /(?:Trying|Tried) a backup model/ })).toHaveCount(0)
  await expect(page.locator('.msg-error')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
  return rows
}

async function capture(page: Page, name: string) {
  const path = test.info().outputPath(`${name}.png`)
  await page.screenshot({ path, fullPage: true })
  await test.info().attach(name, { path, contentType: 'image/png' })
}

for (const width of [1280, 390]) {
  test(`keeps each named fallback across duplicate events, reconnect, and cold history at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const gateway = await prepareGateway(page)
    await gateway.start()
    gateway.fallback('fallback-ab', MODEL_B, MODEL_A)
    await expectFallbackRows(page, [trying(transitionAB)], true)
    const second = gateway.fallback('fallback-bc', MODEL_C, MODEL_B)
    await expectFallbackRows(page, [tried(transitionAB), trying(transitionBC)], true)
    gateway.duplicate(second)
    gateway.heartbeat(second)
    await expectFallbackRows(page, [tried(transitionAB), trying(transitionBC)], true)
    await capture(page, `fallback-chain-live-${width}`)

    await gateway.reconnect()
    await expectFallbackRows(page, [tried(transitionAB), trying(transitionBC)], true)
    gateway.succeed()
    await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
    await expectFallbackRows(page, [tried(transitionAB), tried(transitionBC)])

    await page.reload()
    await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
    await expectFallbackRows(page, [tried(transitionAB), tried(transitionBC)])
    await capture(page, `fallback-chain-history-${width}`)
    gateway.restart()
    await page.reload()
    await expectFallbackRows(page, [tried(transitionAB), tried(transitionBC)])
    await expect(page.locator('.assistant-answer')).toHaveCount(1)
    expect(gateway.sends()).toBe(1)
    expect(gateway.runtimeErrors).toEqual([])
  })

  test(`keeps a target-only fallback without inventing its source at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const gateway = await prepareGateway(page)
    await gateway.start()
    gateway.fallback('fallback-target-only', MODEL_B)
    const liveRows = await expectFallbackRows(page, [trying(MODEL_B)], true)
    await expect(liveRows).not.toContainText(MODEL_A)
    await expect(liveRows).not.toContainText('→')
    await capture(page, `fallback-target-only-live-${width}`)
    gateway.succeed()
    await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
    await page.reload()
    const historyRows = await expectFallbackRows(page, [tried(MODEL_B)])
    await expect(historyRows).not.toContainText(MODEL_A)
    await expect(historyRows).not.toContainText('→')
    expect(gateway.sends()).toBe(1)
    expect(gateway.runtimeErrors).toEqual([])
  })
}

test('wraps full model IDs in the Chinese narrow timeline', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 })
  const gateway = await prepareGateway(page, 'zh-Hans')
  await gateway.start()
  const from = 'primary-provider/deep-reasoning-model-production-release-2026-10-13-region-east'
  const to = 'backup-provider/backup-reasoning-model-production-release-2026-10-13-region-west'
  gateway.fallback('fallback-long-models', to, from)
  const activity = await openActivity(page)
  const rows = activity.locator('.assistant-activity-status__row').filter({ hasText: to })
  await expect(rows).toHaveCount(1)
  await expect(rows).toContainText(`正在尝试备用模型 · ${from} → ${to}`)
  await expect(rows).toHaveClass(/assistant-activity-status__row--current/)
  const bounds = await rows.evaluate(element => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const rects = [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0)
    return {
      left: Math.min(...rects.map(rect => rect.left)),
      right: Math.max(...rects.map(rect => rect.right)),
      lines: new Set(rects.map(rect => Math.round(rect.top))).size,
    }
  })
  expect(bounds.left).toBeGreaterThanOrEqual(0)
  expect(bounds.right).toBeLessThanOrEqual(390)
  expect(bounds.lines).toBeGreaterThan(1)
  await capture(page, 'fallback-long-models-mobile')
  expect(gateway.runtimeErrors).toEqual([])
})
