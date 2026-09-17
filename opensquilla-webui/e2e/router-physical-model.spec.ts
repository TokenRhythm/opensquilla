import { expect, test, type Locator, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION_KEY = 'agent:main:webchat:e2e-router-physical'
const TURN_ID = 'turn-router-physical'
const PRIMARY = 'deepseek-v4-pro'
const CANDIDATE = 'kimi-k2.7-code'
const EXTERNAL = 'deepseek-v4-pro-0813'
const AGGREGATOR = 'deepseek-flash'
const FUSION_ROUTE_MODEL = 'glm-5.2'
const FUSION_MEMBERS = [AGGREGATOR, 'glm-5.3-flash', 'qwen3.8-flash', 'qwen3.8-max']
const ANSWER = 'The synthetic fallback completed successfully.'
const BASE_TIME = 1_800_000_000_000
const ROUTE = {
  version: 2,
  tier: 'c1',
  model: PRIMARY,
  source: 'classifier',
  routing_applied: true,
  router_tier_snapshot: {
    version: 1,
    request_kind: 'text',
    tiers: [
      { tier: 'c1', model: PRIMARY, execution_kind: 'single_model' },
      { tier: 'c2', model: CANDIDATE, execution_kind: 'single_model' },
    ],
  },
}
const FUSION_ROUTE = {
  ...ROUTE,
  tier: 'c3', model: FUSION_ROUTE_MODEL,
  router_tier_snapshot: {
    ...ROUTE.router_tier_snapshot,
    tiers: [
      { tier: 'c1', model: PRIMARY, execution_kind: 'single_model' },
      { tier: 'c3', model: FUSION_ROUTE_MODEL, execution_kind: 'ensemble' },
    ],
  },
}

type WireEvent = { event: string; payload: Record<string, unknown> }

async function installGateway(page: Page, { legacy = false, fusion = false } = {}) {
  const route = fusion ? FUSION_ROUTE : ROUTE
  let socket: WebSocketRoute | undefined
  let started = false
  let settled = false
  let seq = 0
  let iteration = 1
  let streamedText = ''
  let currentModel = PRIMARY
  let userMessageId = 'user-router-physical'
  let generation = 'router-physical-generation-a'
  let holdHistory = false
  const heldHistory: Array<() => void> = []
  const events: WireEvent[] = []
  const executionLegs: Array<Record<string, unknown>> = []

  function emit(name: string, payload: Record<string, unknown> = {}) {
    if (!socket) throw new Error('Synthetic Gateway is not connected')
    const frame = {
      event: name,
      payload: {
        key: SESSION_KEY, session_key: SESSION_KEY,
        task_id: TURN_ID, turn_id: TURN_ID,
        stream_seq: ++seq, stream_generation: generation,
        emitted_at: BASE_TIME + seq * 100,
        ...payload,
      },
    }
    if (name.startsWith('session.event.')) events.push(frame)
    socket.send(JSON.stringify({ type: 'event', ...frame }))
  }

  function activity(model: string, reason = 'transport_transient', phase = 'requesting') {
    currentModel = model
    if (model && !executionLegs.some(leg => leg.model === model)) {
      executionLegs.push({
        kind: model === PRIMARY ? 'primary' : 'provider_fallback',
        provider: 'synthetic', model,
      })
    }
    emit('session.event.provider_activity', {
      activity_id: `provider-${executionLegs.length}`, phase, reason,
      retry_attempt: phase === 'retrying' ? 2 : 1,
      retry_limit: 3, retry_after_ms: phase === 'retry_wait' ? 2000 : 0,
      model_call_id: `${iteration}.0`, iteration,
      ...(!legacy ? { model } : {}),
    })
  }

  function usage() {
    return {
      model: currentModel,
      input_tokens: 24, output_tokens: 8,
      routed_tier: route.tier,
      routed_model: legacy ? PRIMARY : currentModel,
      routing_source: route.source,
      routing_applied: true,
      router_model_call_id: `${iteration}.0`, router_iteration: iteration,
      ...(!legacy ? { route_plan: route, execution_legs: executionLegs } : {}),
      ...(fusion ? {
        // Older receipts recorded the outer selector's unused baseline.
        execution_legs: [{ kind: 'primary', provider: 'synthetic', model: PRIMARY }],
        ensemble_trace: {
          profile: 'static_tokenrhythm_b5', total_candidates: 4,
          selected_candidate_count: 4, llm_request_count: 5,
          final_request: {
            role: 'aggregator', request_started: true,
            execution: { provider: 'synthetic', model: currentModel },
          },
        },
        model_usage_breakdown: [...FUSION_MEMBERS, currentModel].map((model, index) => ({
          role: index === 4 ? 'aggregator' : 'proposer', provider: 'synthetic', model,
          input_tokens: 4, output_tokens: 2, cost_usd: 0, request_count: 1,
        })),
      } : {}),
    }
  }

  function history() {
    if (!started) return chatHistoryPayload()
    const user = {
      role: 'user', text: 'Exercise the synthetic fallback chain.',
      id: userMessageId, message_id: userMessageId, timestamp: BASE_TIME,
      turn_context: { turn_id: TURN_ID },
    }
    if (!settled) return chatHistoryPayload([user])
    return chatHistoryPayload([user, {
      role: 'assistant', text: streamedText || ANSWER,
      id: 'assistant-router-physical', message_id: 'assistant-router-physical',
      timestamp: BASE_TIME + 10_000,
      turn_context: { turn_id: TURN_ID }, usage: usage(),
    }], {
      turn_outcomes: [{
        turn_id: TURN_ID, task_id: TURN_ID, status: 'succeeded',
        started_at: BASE_TIME, finished_at: BASE_TIME + 10_000,
        outcome: { kind: 'completed' },
      }],
    })
  }

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    const respond = (id: unknown, payload: unknown) => ws.send(JSON.stringify({
      type: 'res', id, ok: true, payload,
    }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ policy: { concurrent_history_reads: true } }))
        return
      }
      if (frame.method === 'chat.send') {
        started = true
        userMessageId = frame.params.client_message_id || userMessageId
        respond(frame.id, {
          accepted: true, session: SESSION_KEY, task_id: TURN_ID,
          user_message_id: userMessageId, stream_seq: seq,
        })
        emit('task.running')
        emit('session.event.state_change', { to_state: 'thinking' })
        emit('session.event.router_decision', route)
        activity(fusion ? '' : PRIMARY, 'initial')
        return
      }
      if (frame.method === 'chat.history' && holdHistory) {
        heldHistory.push(() => respond(frame.id, history()))
        return
      }
      const running = started && !settled
      const activeTask = running ? { task_id: TURN_ID, status: 'running' } : null
      const metadata = {
        stream_generation: generation,
        current_stream_seq: running ? seq : 0,
        run_status: running ? 'running' : 'idle',
        active_task: activeTask, tasks: activeTask ? [activeTask] : [],
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': history(),
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: {
            enabled: true, rollout_phase: 'full', visual_mode: 'real_candidates',
            tiers: fusion
              ? { c1: { model: PRIMARY }, c3: { model: FUSION_ROUTE_MODEL, ensemble_enabled: true } }
              : { c1: { model: PRIMARY }, c2: { model: CANDIDATE } },
          },
          llm_ensemble: { enabled: false }, permissions: {}, skills: {},
        },
        'models.routing.get': { mode: 'router' },
        'sessions.routing.get': { key: SESSION_KEY, mode: 'router', revision: 0 },
        'onboarding.status': { audioConfigured: false },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sessions.list': { sessions: [], count: 0, ts: BASE_TIME / 1000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION_KEY, metadata),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION_KEY, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION_KEY, {
          ...metadata, task_id: activeTask?.task_id ?? null,
          events: running ? events : [],
        }),
        'usage.status': { sessions: [] },
      }
      respond(frame.id, payloads[frame.method] ?? {})
    })
  })

  return {
    activity,
    aggregate() {
      FUSION_MEMBERS.forEach((model, index) => {
        for (const event_type of ['proposer_start', 'proposer_finish']) {
          emit('session.event.ensemble_progress', {
            event_type, proposer_index: index, proposer_provider: 'synthetic', proposer_model: model,
          })
        }
      })
      emit('session.event.ensemble_progress', {
        event_type: 'aggregator_start', proposer_provider: 'synthetic', proposer_model: AGGREGATOR,
      })
      activity(AGGREGATOR, 'initial')
    },
    text(text: string, callIteration = iteration) {
      iteration = callIteration
      streamedText += text
      emit('session.event.text_delta', {
        text, presentation: 'answer', model_call_id: `${iteration}.0`, iteration,
      })
    },
    replay() {
      emit('session.event.router_control_replay')
      iteration = 1
      streamedText = ''
      emit('session.event.router_decision', route)
    },
    holdHistory() { holdHistory = true },
    releaseHistory() {
      holdHistory = false
      heldHistory.splice(0).forEach(release => release())
    },
    finish(finalModel = currentModel) {
      currentModel = finalModel
      if (!executionLegs.some(leg => leg.model === finalModel)) {
        executionLegs.push({ kind: 'provider_fallback', provider: 'synthetic', model: finalModel })
      }
      settled = true
      streamedText += ANSWER
      emit('session.event.text_delta', {
        text: ANSWER, presentation: 'answer', model_call_id: `${iteration}.0`, iteration,
      })
      emit('session.event.usage', usage())
      emit('session.event.state_change', { to_state: 'completed', final_text: streamedText })
      emit('session.event.done', { final_text: streamedText, usage: usage() })
    },
    restart() { generation = 'router-physical-generation-b' },
  }
}

async function startTurn(page: Page) {
  await page.goto('/control/chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 15_000 })
  await page.locator('.chat-textarea').fill('Exercise the synthetic fallback chain.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect(page.locator('.router-fx')).toHaveCount(1)
  return page.locator('.router-fx')
}

async function expectRoutePool(strip: Locator) {
  await expect(strip).toHaveAttribute('data-source', ROUTE.source)
  await expect(strip.locator('.router-fx-cell .nm')).toHaveCount(2)
  expect(await strip.locator('.router-fx-cell .nm').evaluateAll(nodes => (
    nodes.map(node => node.getAttribute('aria-label'))
  ))).toEqual([PRIMARY, CANDIDATE])
}

async function expectExecuting(
  strip: Locator,
  model: string,
  options: { settled?: boolean; announce?: boolean } = {},
) {
  await expectRoutePool(strip)
  const label = options.settled ? `Executed by ${model}` : `Currently executing ${model}`
  await expect(strip).toHaveAttribute('aria-label', label)
  await expect(strip.getByTestId('router-execution-model')).toHaveText(label)
  if (options.announce !== false) {
    await expect(strip.getByRole('status')).toHaveText(label)
  }
  if (model === CANDIDATE) {
    await expect(strip.locator('.router-fx-cell.win .nm')).toHaveAttribute('aria-label', CANDIDATE)
  } else {
    await expect(strip.locator('.router-fx-cell.win')).toHaveCount(0)
    await expect(strip.locator('.router-fx-grid')).not.toContainText(model)
  }
}

test('tracks candidate fallback and retry, then out-of-pool success and terminal history', async ({ page }) => {
  const gateway = await installGateway(page)
  const strip = await startTurn(page)
  await expect(strip.locator('.router-fx-cell.win .nm')).toHaveAttribute('aria-label', PRIMARY)
  gateway.activity(CANDIDATE, 'provider_overloaded', 'fallback')
  await expectExecuting(strip, CANDIDATE)
  await expect(page.locator('.assistant-activity')).toContainText('Switching to backup model')
  gateway.activity(CANDIDATE)
  await expectExecuting(strip, CANDIDATE)
  gateway.activity(CANDIDATE, 'rate_limited', 'retry_wait')
  await expectExecuting(strip, CANDIDATE)
  await expect(page.locator('.assistant-activity')).toContainText('Rate limited')
  gateway.activity(CANDIDATE, 'transport_transient', 'retrying')
  await expectExecuting(strip, CANDIDATE)
  await expect(page.locator('.assistant-activity')).toContainText('Retrying')
  gateway.activity(EXTERNAL)
  await expectExecuting(strip, EXTERNAL)
  // Keep the live completion announcement separate from the history refresh,
  // whose static card deliberately does not announce an old result again.
  gateway.holdHistory()
  gateway.finish()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expect(strip).toHaveAttribute('data-settled', 'true')
  await expectExecuting(strip, EXTERNAL, { settled: true })
  gateway.releaseHistory()
  await page.reload()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expectExecuting(strip, EXTERNAL, { settled: true, announce: false })
  gateway.restart()
  await page.reload()
  await expectExecuting(strip, EXTERNAL, { settled: true, announce: false })
  await expect(strip).toHaveCount(1)
})

test('restores the active physical model from replay and continues after refresh', async ({ page }) => {
  const gateway = await installGateway(page)
  const strip = await startTurn(page)
  gateway.activity(CANDIDATE)
  await expectExecuting(strip, CANDIDATE)
  gateway.holdHistory()
  await page.reload()
  await expectExecuting(strip, CANDIDATE, { announce: false })
  gateway.releaseHistory()
  await expect(page.locator('.msg-user')).toContainText('Exercise the synthetic fallback chain.')
  await expectExecuting(strip, CANDIDATE, { announce: false })
  gateway.activity(EXTERNAL)
  await expectExecuting(strip, EXTERNAL)
  await page.reload()
  await expectExecuting(strip, EXTERNAL, { announce: false })
  gateway.holdHistory()
  gateway.finish()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expectExecuting(strip, EXTERNAL, { settled: true })
  await expect(strip).toHaveCount(1)
  gateway.releaseHistory()
})

test('keeps the in-pool fallback highlighted after terminal history restoration', async ({ page }) => {
  const gateway = await installGateway(page)
  const strip = await startTurn(page)
  gateway.activity(CANDIDATE)
  await expectExecuting(strip, CANDIDATE)
  gateway.holdHistory()
  gateway.finish()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expectExecuting(strip, CANDIDATE, { settled: true })
  gateway.releaseHistory()
  await page.reload()
  await expectExecuting(strip, CANDIDATE, { settled: true, announce: false })
})

for (const firstIteration of [1, 2]) {
  test(`settles control replay from call ${firstIteration}.0 to a new call 1.0`, async ({ page }) => {
    const gateway = await installGateway(page)
    await startTurn(page)
    gateway.activity(CANDIDATE)
    // A tool-only first call can leave the card unbound until iteration two.
    // Control replay starts a new Agent whose first call is numbered 1.0 again.
    gateway.text('Synthetic first attempt.', firstIteration)
    await expectExecuting(page.locator('.router-fx'), CANDIDATE)
    gateway.replay()
    gateway.activity(EXTERNAL)
    gateway.text('Synthetic replay attempt.')
    const strips = page.locator('.router-fx')
    await expect(strips).toHaveCount(2)
    await expectExecuting(strips.nth(0), CANDIDATE, { settled: true })
    await expectExecuting(strips.nth(1), EXTERNAL)

    gateway.holdHistory()
    await page.reload()
    await expect(strips).toHaveCount(2)
    await expectExecuting(strips.nth(0), CANDIDATE, { settled: true, announce: false })
    await expectExecuting(strips.nth(1), EXTERNAL, { announce: false })
    gateway.releaseHistory()
    await expect(page.locator('.msg-user')).toContainText('Exercise the synthetic fallback chain.')
    await expectExecuting(strips.nth(0), CANDIDATE, { settled: true, announce: false })
    await expectExecuting(strips.nth(1), EXTERNAL, { announce: false })
    gateway.holdHistory()
    gateway.finish()
    await expect(page.locator('.assistant-answer')).toHaveText('Synthetic replay attempt.' + ANSWER)
    await expect(strips).toHaveCount(2)
    await expectExecuting(strips.nth(0), CANDIDATE, { settled: true, announce: false })
    await expectExecuting(strips.nth(1), EXTERNAL, { settled: true })
  })
}

test('uses terminal execution legs when the final provider activity was not delivered', async ({ page }) => {
  const gateway = await installGateway(page)
  const strip = await startTurn(page)
  gateway.activity(CANDIDATE)
  await expectExecuting(strip, CANDIDATE)
  gateway.holdHistory()
  gateway.finish(EXTERNAL)
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expectExecuting(strip, EXTERNAL, { settled: true })
  gateway.releaseHistory()
  await page.reload()
  await expectExecuting(strip, EXTERNAL, { settled: true, announce: false })
})

test('accepts old Gateway activity and history without physical model fields', async ({ page }) => {
  const gateway = await installGateway(page, { legacy: true })
  const strip = await startTurn(page)
  gateway.activity(CANDIDATE)
  gateway.activity(EXTERNAL, 'transport_transient', 'retrying')
  await expectRoutePool(strip)
  await expect(strip).toHaveAttribute('aria-label', `Router selected ${PRIMARY}`)
  await expect(strip.locator('.router-fx-cell.win .nm')).toHaveAttribute('aria-label', PRIMARY)
  await expect(strip.getByTestId('router-execution-model')).toHaveCount(0)
  gateway.finish()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await page.reload()
  await expect(strip).toHaveAttribute('aria-label', `Router selected ${PRIMARY}`)
  await expect(strip.getByTestId('router-execution-model')).toHaveCount(0)
})

test('shows the physical C5 aggregator and ignores an unused baseline in older terminal receipts', async ({ page }) => {
  const gateway = await installGateway(page, { fusion: true })
  const strip = await startTurn(page)
  await expect(strip).toHaveAttribute('data-panel', 'router-ensemble-sequence')
  await expect(strip.getByTestId('router-execution-model')).toHaveCount(0)
  gateway.aggregate()
  await expect(strip.getByTestId('router-execution-model')).toHaveText(`Currently executing ${AGGREGATOR}`)
  await expect(strip.locator('.router-fx-cell.win')).toHaveCount(0)
  await expect(strip).toHaveAttribute('aria-label', `Currently executing ${AGGREGATOR}`)
  await expect(strip.locator('.router-fx-sr-only')).toHaveText(`Currently executing ${AGGREGATOR}`)
  gateway.holdHistory()
  gateway.finish()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expect(strip.getByTestId('router-execution-model')).toHaveText(`Executed by ${AGGREGATOR}`)
  await expect(strip).toHaveAttribute('aria-label', `Executed by ${AGGREGATOR}`)
  await expect(strip.locator('.router-fx-sr-only')).toHaveText(`Executed by ${AGGREGATOR}`)
  gateway.releaseHistory()
  await page.reload()
  await expect(page.locator('.assistant-answer')).toHaveText(ANSWER)
  await expect(strip).toHaveAttribute('data-panel', 'router-ensemble-sequence')
  await expect(strip).toHaveAttribute('aria-label', `Executed by ${AGGREGATOR}`)
  await expect(strip.getByTestId('router-execution-model')).toHaveText(`Executed by ${AGGREGATOR}`)
  await expect(strip.locator('.router-fx-cell.win')).toHaveCount(0)
})
