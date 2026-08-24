import { writeFile } from 'node:fs/promises'
import { isAbsolute } from 'node:path'

import {
  expect,
  test,
  type Page,
  type WebSocketRoute,
} from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-long-task-resilience'
const TASK_ID = 'task-e2e-long-running'
const ROUTER_SESSION_KEY = 'agent:main:webchat:e2e-router-reconnect'
const ROUTER_TASK_ID = 'task-e2e-router-reconnect'
const ROUTER_TURN_ID = 'turn-e2e-router-reconnect'

type RpcRequest = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type PendingServerRow = {
  pendingInputId: string
  clientRequestId: string
  clientMessageId: string
  requestFingerprint: string
  message: string
  attachments: Array<{
    name: string
    mime: string
    type: string
    size: number
    sha256_ref: string
  }>
  revision: number
  createdAt: number
  updatedAt: number
}

async function dropSyntheticFile(
  page: Page,
  file: { name: string; type: string; bytes: string },
) {
  await page.evaluate((fileSpec) => {
    const transfer = new DataTransfer()
    transfer.items.add(new File([fileSpec.bytes], fileSpec.name, { type: fileSpec.type }))
    const chat = document.querySelector('.chat')
    if (!chat) throw new Error('chat root not found')
    for (const type of ['dragenter', 'dragover', 'drop']) {
      chat.dispatchEvent(new DragEvent(type, {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    }
  }, file)
}

const PENDING_METHODS = [
  'sessions.pending_inputs.enqueue',
  'sessions.pending_inputs.list',
  'sessions.pending_inputs.update',
  'sessions.pending_inputs.cancel',
  'sessions.pending_inputs.dispatch',
]

function successResponse(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function errorResponse(
  id: string | number | undefined,
  code: string,
  message: string,
  options: { accepted?: boolean; retryable?: boolean } = {},
) {
  return JSON.stringify({
    type: 'res',
    id,
    ok: false,
    error: { code, message, ...options },
  })
}

function eventFrame(event: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event, payload })
}

function hello(methods: string[] = [], events: string[] = []) {
  return JSON.stringify({
    protocol: 3,
    policy: {
      tick_interval_ms: 30_000,
      concurrent_history_reads: true,
      webui_stream_idle_grace_ms: 1_260_000,
    },
    features: {
      methods: [
        'sessions.messages.subscribe',
        'sessions.messages.unsubscribe',
        'sessions.messages.snapshot',
        'sessions.messages.hydrate',
        ...methods,
      ],
      events: [
        'session.event.text_delta',
        'session.event.thinking',
        'session.event.tool_use_delta',
        'session.event.provider_activity',
        'session.event.run_heartbeat',
        'session.event.done',
        ...events,
      ],
    },
    auth: {
      principal: { isOwner: true },
      runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
    },
  })
}

function basePayload(method: string): unknown {
  const payloads: Record<string, unknown> = {
    'agents.list': { agents: [] },
    'commands.list_for_surface': { commands: [] },
    'config.get': {
      squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
      permissions: {},
      skills: {},
    },
    'models.routing.get': { mode: 'direct' },
    'onboarding.status': { audioConfigured: false },
    'sessions.list': { sessions: [], has_more: false },
    'sessions.messages.hydrate': {
      hydration_complete: true,
      workspaceId: null,
      run_status: 'idle',
    },
    'sessions.messages.unsubscribe': { subscribed: false },
    'sessions.subscribe': { subscribed: true },
    'usage.status': { sessions: [] },
  }
  return payloads[method] ?? {}
}

async function preparePage(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
}

function runningSubscription(
  generation: string,
  currentStreamSeq: number,
  extra: Record<string, unknown> = {},
) {
  return {
    subscribed: true,
    hydration_complete: true,
    replay_complete: true,
    current_stream_seq: currentStreamSeq,
    stream_generation: generation,
    run_status: 'running',
    active_task: { task_id: TASK_ID, status: 'running' },
    ...extra,
  }
}

test.describe('0.5.0 long-task resilience', () => {
  // The heap/long-task row is an absolute fixed-runner gate. Do not contend it
  // with three unrelated Chromium renderer processes when the repository-wide
  // config enables fullyParallel; that changes GC scheduling and turns the
  // measured browser heap into a host-load assertion. CI already uses one
  // worker, and local runs must exercise the same deterministic isolation.
  test.describe.configure({ mode: 'serial' })

  test('resets the stream cursor across a Gateway generation and automatically re-subscribes', async ({
    page,
  }) => {
    test.setTimeout(30_000)
    await preparePage(page)

    const generations = ['generation-before-restart', 'generation-after-restart']
    const sockets: WebSocketRoute[] = []
    const subscribeParams: Array<Record<string, unknown>> = []
    let replacementHandshakeAt = 0
    let replacementSubscribeAt = 0
    let releaseReplacementSubscription: (() => void) | null = null
    let initialSnapshotComplete = false

    await page.routeWebSocket(/\/ws$/, ws => {
      const socketIndex = sockets.length
      const generation = generations[Math.min(socketIndex, generations.length - 1)]!
      sockets.push(ws)
      ws.send(eventFrame('connect.challenge', {}))
      ws.onMessage(message => {
        let frame: RpcRequest
        try {
          frame = JSON.parse(String(message)) as RpcRequest
        } catch {
          return
        }
        if (frame.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          if (socketIndex === 1) replacementHandshakeAt = performance.now()
          ws.send(hello())
          return
        }
        if (method === 'sessions.messages.subscribe') {
          subscribeParams.push({ ...(frame.params || {}) })
          if (socketIndex === 1) {
            replacementSubscribeAt = performance.now()
            releaseReplacementSubscription = () => ws.send(successResponse(
              frame.id,
              runningSubscription(generation, 0, {
                replay_gap: true,
                replay_gap_reason: 'stream_generation_changed',
              }),
            ))
            return
          }
          ws.send(successResponse(frame.id, runningSubscription(generation, 50)))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          if (socketIndex === 0) initialSnapshotComplete = true
          ws.send(successResponse(frame.id, {
            key: SESSION_KEY,
            task_id: TASK_ID,
            events: [],
            current_stream_seq: socketIndex === 0 ? 50 : 0,
            stream_generation: generation,
            run_status: 'running',
            active_task: { task_id: TASK_ID, status: 'running' },
          }))
          return
        }
        if (method === 'sessions.messages.hydrate') {
          ws.send(successResponse(frame.id, {
            ...runningSubscription(generation, socketIndex === 0 ? 50 : 0),
            hydration_complete: true,
            workspaceId: null,
          }))
          return
        }
        if (method === 'chat.history') {
          ws.send(successResponse(frame.id, {
            messages: [{
              role: 'user',
              text: 'Keep this long-running task alive across a Gateway restart.',
              id: 'generation-user-message',
              message_id: 'generation-user-message',
              timestamp: Math.floor(Date.now() / 1000) - 60,
              turn_context: { turn_id: TASK_ID },
            }],
            has_more: false,
            canonical_complete: true,
          }))
          return
        }
        ws.send(successResponse(frame.id, basePayload(method)))
      })
    })

    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => sockets.length).toBe(1)
    await expect.poll(() => initialSnapshotComplete).toBe(true)
    await expect(
      page.getByText('Keep this long-running task alive across a Gateway restart.', {
        exact: true,
      }),
    ).toBeVisible()

    sockets[0]!.send(eventFrame('session.event.text_delta', {
      key: SESSION_KEY,
      task_id: TASK_ID,
      stream_generation: generations[0],
      stream_seq: 51,
      text: 'Incremental output before restart.',
    }))
    await expect(page.getByText('Incremental output before restart.', { exact: true })).toBeVisible()

    await sockets[0]!.close({ code: 1012, reason: 'deterministic Gateway restart' })
    await expect.poll(() => sockets.length, { timeout: 4_000 }).toBe(2)
    await expect.poll(() => Boolean(releaseReplacementSubscription)).toBe(true)

    expect(replacementSubscribeAt - replacementHandshakeAt).toBeLessThanOrEqual(2_000)
    expect(subscribeParams[1]).toMatchObject({
      key: SESSION_KEY,
      since_stream_generation: generations[0],
      since_stream_seq: 51,
    })
    // A healthy WebSocket alone is insufficient. The connection pill stays
    // degraded until the session subscription itself has recovered.
    await expect(page.locator('.conn-pill.connected')).toHaveCount(0)

    const release = releaseReplacementSubscription
    if (!release) throw new Error('replacement subscription was not captured')
    release()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 2_000 })

    const firstTokenAt = performance.now()
    sockets[1]!.send(eventFrame('session.event.text_delta', {
      key: SESSION_KEY,
      task_id: TASK_ID,
      stream_generation: generations[1],
      // The new process deliberately starts at a smaller sequence. This is the
      // regression that used to be swallowed by max(oldSeq, newSeq).
      stream_seq: 1,
      text: 'First incremental token after restart.',
    }))
    await expect(
      page.getByText('First incremental token after restart.', { exact: true }),
    ).toBeVisible({ timeout: 2_000 })
    expect(performance.now() - firstTokenAt).toBeLessThanOrEqual(2_000)
  })

  test('keeps one router card across reconnect snapshot, done, and history refresh', async ({
    page,
  }) => {
    test.setTimeout(30_000)
    await preparePage(page)
    await page.addInitScript(() => {
      window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
    })
    await page.emulateMedia({ reducedMotion: 'reduce' })

    const generation = 'generation-router-reconnect'
    const sockets: WebSocketRoute[] = []
    let initialSnapshotComplete = false
    let settledHistory = false
    const routerPayload = {
      session_key: ROUTER_SESSION_KEY,
      task_id: ROUTER_TASK_ID,
      turn_id: ROUTER_TURN_ID,
      stream_generation: generation,
      stream_seq: 10,
      tier: 'c1',
      tier_index: 1,
      model: 'deepseek/deepseek-v4-pro',
      baseline_model: 'deepseek/deepseek-v4-pro',
      source: 'squilla_router',
      confidence: 0.8,
      routing_applied: true,
    }
    const history = () => {
      const messages: Array<Record<string, unknown>> = [{
        role: 'user',
        text: 'Build a long HTML document.',
        id: 'router-reconnect-user',
        message_id: 'router-reconnect-user',
        timestamp: Math.floor(Date.now() / 1_000) - 10,
        turn_context: { turn_id: ROUTER_TURN_ID },
      }]
      if (settledHistory) {
        messages.push({
          role: 'assistant',
          text: 'The document is ready.',
          id: 'router-reconnect-assistant',
          message_id: 'router-reconnect-assistant',
          timestamp: Math.floor(Date.now() / 1_000),
          turn_context: { turn_id: ROUTER_TURN_ID },
          usage: {
            routed_tier: 'c1',
            routed_model: 'deepseek/deepseek-v4-pro',
            routing_source: 'squilla_router',
            router_model_call_id: '1.0',
            router_iteration: 1,
          },
        })
      }
      return { messages, has_more: false, canonical_complete: true }
    }

    await page.routeWebSocket(/\/ws$/, ws => {
      const socketIndex = sockets.length
      sockets.push(ws)
      ws.send(eventFrame('connect.challenge', {}))
      ws.onMessage(message => {
        let frame: RpcRequest
        try {
          frame = JSON.parse(String(message)) as RpcRequest
        } catch {
          return
        }
        if (frame.type === 'ping') {
          ws.send(JSON.stringify({ type: 'pong' }))
          return
        }
        if (frame.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(hello([], [
            'session.event.router_decision',
            'session.event.turn_committed',
          ]))
          return
        }
        if (method === 'chat.history') {
          ws.send(successResponse(frame.id, history()))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          const replayRouter = socketIndex > 0 && !settledHistory
          ws.send(successResponse(frame.id, {
            key: ROUTER_SESSION_KEY,
            task_id: settledHistory ? null : ROUTER_TASK_ID,
            stream_generation: generation,
            current_stream_seq: settledHistory ? 11 : replayRouter ? 10 : 9,
            events: replayRouter
              ? [{ event: 'session.event.router_decision', payload: routerPayload }]
              : [],
          }))
          if (socketIndex === 0) initialSnapshotComplete = true
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(successResponse(frame.id, {
            subscribed: true,
            replay_complete: true,
            stream_generation: generation,
            current_stream_seq: settledHistory ? 11 : socketIndex > 0 ? 10 : 9,
            run_status: settledHistory ? 'idle' : 'running',
            active_task: settledHistory
              ? null
              : { task_id: ROUTER_TASK_ID, status: 'running' },
          }))
          return
        }
        if (method === 'sessions.messages.hydrate') {
          ws.send(successResponse(frame.id, {
            hydration_complete: true,
            stream_generation: generation,
            current_stream_seq: settledHistory ? 11 : socketIndex > 0 ? 10 : 9,
            run_status: settledHistory ? 'idle' : 'running',
            active_task: settledHistory
              ? null
              : { task_id: ROUTER_TASK_ID, status: 'running' },
          }))
          return
        }
        if (method === 'config.get') {
          ws.send(successResponse(frame.id, {
            squilla_router: {
              enabled: true,
              rollout_phase: 'full',
              tiers: {
                c0: { model: 'openai/gpt-5.4-mini' },
                c1: { model: 'deepseek/deepseek-v4-pro' },
                c2: { model: 'z-ai/glm-5.2' },
              },
            },
            permissions: {},
            skills: {},
          }))
          return
        }
        if (method === 'models.routing.get') {
          ws.send(successResponse(frame.id, { mode: 'router' }))
          return
        }
        ws.send(successResponse(frame.id, basePayload(method)))
      })
    })

    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(ROUTER_SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => initialSnapshotComplete).toBe(true)
    sockets[0]!.send(eventFrame('session.event.router_decision', routerPayload))
    await expect(page.locator('.router-fx')).toHaveCount(1)

    await sockets[0]!.close({ code: 1012, reason: 'deterministic reconnect' })
    await expect.poll(() => sockets.length, { timeout: 4_000 }).toBe(2)
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 4_000 })
    await expect(page.locator('.router-fx')).toHaveCount(1)

    sockets[1]!.send(eventFrame('session.event.done', {
      session_key: ROUTER_SESSION_KEY,
      task_id: ROUTER_TASK_ID,
      turn_id: ROUTER_TURN_ID,
      stream_generation: generation,
      stream_seq: 11,
      reason: 'completed',
      text: 'The document is ready.',
      usage: {
        model: 'deepseek/deepseek-v4-pro',
        routed_tier: 'c1',
        routed_model: 'deepseek/deepseek-v4-pro',
        routing_source: 'squilla_router',
        router_model_call_id: '1.0',
        router_iteration: 1,
      },
    }))
    await expect(page.locator('.router-fx')).toHaveCount(1)
    await expect(page.locator('.router-fx[data-settled="true"]')).toHaveCount(1)
    await expect(page.getByText('The document is ready.', { exact: true })).toBeVisible()

    settledHistory = true
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.router-fx')).toHaveCount(1)
    await expect(page.getByText('The document is ready.', { exact: true })).toHaveCount(1)
  })

  test('projects reasoning, Retry-After, retry, fallback, and stale progress without leaking errors', async ({
    page,
  }) => {
    test.setTimeout(30_000)
    await page.clock.install({ time: new Date('2026-08-12T00:00:00Z') })
    await preparePage(page)
    const generation = 'generation-provider-activity'
    let socket: WebSocketRoute | null = null
    let snapshotComplete = false

    await page.routeWebSocket(/\/ws$/, ws => {
      socket = ws
      ws.send(eventFrame('connect.challenge', {}))
      ws.onMessage(message => {
        let frame: RpcRequest
        try {
          frame = JSON.parse(String(message)) as RpcRequest
        } catch {
          return
        }
        if (frame.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(hello())
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(successResponse(frame.id, runningSubscription(generation, 0)))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          snapshotComplete = true
          ws.send(successResponse(frame.id, {
            ...runningSubscription(generation, 0),
            key: SESSION_KEY,
            task_id: TASK_ID,
            events: [],
          }))
          return
        }
        if (method === 'chat.history') {
          ws.send(successResponse(frame.id, { messages: [], has_more: false }))
          return
        }
        ws.send(successResponse(frame.id, basePayload(method)))
      })
    })

    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => snapshotComplete).toBe(true)
    const liveLabel = page.locator('.assistant-activity--live .assistant-activity__live-label')
    const sendActivity = (streamSeq: number, payload: Record<string, unknown>) => {
      if (!socket) throw new Error('provider activity websocket is not connected')
      socket.send(eventFrame('session.event.provider_activity', {
        key: SESSION_KEY,
        task_id: TASK_ID,
        stream_generation: generation,
        stream_seq: streamSeq,
        schema_version: 1,
        activity_id: `provider-activity-${streamSeq}`,
        started_at: Date.now(),
        heartbeat: false,
        ...payload,
      }))
    }

    sendActivity(1, { phase: 'requesting', reason: 'initial' })
    await page.clock.runFor(50)
    await expect(liveLabel).toHaveText('Waiting for model')

    await page.evaluate(() => {
      const state = { startedAt: performance.now(), visibleAt: 0 }
      Object.defineProperty(window, '__reasoningPhaseTiming', {
        configurable: true,
        value: state,
      })
      const label = document.querySelector('.assistant-activity--live .assistant-activity__live-label')
      if (!label) throw new Error('live provider activity label not found')
      const observer = new MutationObserver(() => {
        if (label.textContent?.trim() !== 'Thinking deeply' || state.visibleAt > 0) return
        state.visibleAt = performance.now()
        observer.disconnect()
      })
      observer.observe(label, { childList: true, characterData: true, subtree: true })
    })
    sendActivity(2, { phase: 'reasoning', reason: 'reasoning_only' })
    await page.clock.runFor(50)
    await expect(liveLabel).toHaveText('Thinking deeply', { timeout: 1_000 })
    const reasoningLatency = await page.evaluate(() => {
      const state = (window as unknown as {
        __reasoningPhaseTiming: { startedAt: number; visibleAt: number }
      }).__reasoningPhaseTiming
      return state.visibleAt - state.startedAt
    })
    expect(reasoningLatency).toBeGreaterThanOrEqual(0)
    expect(reasoningLatency).toBeLessThanOrEqual(1_000)

    const privateProviderBody = 'upstream-secret-debug-body-must-not-render'
    sendActivity(3, {
      phase: 'retry_wait',
      reason: 'rate_limited',
      retry_attempt: 1,
      retry_limit: 3,
      retry_after_ms: 3_000,
      provider_error_body: privateProviderBody,
    })
    await page.clock.runFor(50)
    await expect(liveLabel).toHaveText('Rate limited · retrying in 3s')

    if (!socket) throw new Error('provider activity websocket is not connected')
    socket.send(eventFrame('session.event.run_heartbeat', {
      key: SESSION_KEY,
      task_id: TASK_ID,
      stream_generation: generation,
      stream_seq: 4,
    }))
    await page.clock.runFor(1_100)
    await expect(liveLabel).toHaveText('Rate limited · retrying in 2s')
    await expect(page.locator('body')).not.toContainText(privateProviderBody)

    // Heartbeats prove transport liveness but must not disguise a provider
    // stall. Advance the browser clock without another semantic signal.
    await page.clock.runFor(20_100)
    await expect(liveLabel).toHaveText('Still working — no recent signal')
    await expect(page.getByRole('button', { name: /Stop .*response/ })).toBeEnabled()

    sendActivity(5, {
      phase: 'retrying',
      reason: 'rate_limited',
      retry_attempt: 2,
      retry_limit: 3,
    })
    await page.clock.runFor(50)
    await expect(liveLabel).toHaveText('Retrying 2/3')

    sendActivity(6, {
      phase: 'fallback',
      reason: 'provider_overloaded',
      retry_attempt: 3,
      retry_limit: 3,
    })
    await page.clock.runFor(50)
    // Fallback must be visible before any token from the backup leg arrives.
    await expect(liveLabel).toHaveText('Switching to backup model')
    socket.send(eventFrame('session.event.text_delta', {
      key: SESSION_KEY,
      task_id: TASK_ID,
      stream_generation: generation,
      stream_seq: 7,
      text: 'Backup-leg output arrived incrementally.',
    }))
    await page.clock.runFor(100)
    await expect(
      page.getByText('Backup-leg output arrived incrementally.', { exact: true }),
    ).toBeVisible()
  })

  test('keeps 200-message history windowed and the composer responsive under a deterministic delta flood', async ({
    page,
  }, testInfo) => {
    test.setTimeout(180_000)
    await preparePage(page)
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.addInitScript(() => {
      const metrics = {
        inputPaintMs: [] as number[],
        longTasksMs: [] as number[],
      }
      Object.defineProperty(window, '__longTaskUxMetrics', { value: metrics })
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) metrics.longTasksMs.push(entry.duration)
      }).observe({ type: 'longtask', buffered: true })
      document.addEventListener('input', event => {
        if (!(event.target instanceof HTMLTextAreaElement)) return
        const startedAt = performance.now()
        requestAnimationFrame(() => metrics.inputPaintMs.push(performance.now() - startedAt))
      }, true)
    })

    const generation = 'generation-performance-fixture'
    let socket: WebSocketRoute | null = null
    let streamSeq = 0
    let running = true
    const now = Math.floor(Date.now() / 1000)
    const history: Array<Record<string, unknown>> = Array.from({ length: 200 }, (_, index) => ({
      role: index % 2 === 0 ? 'user' : 'assistant',
      text: `Synthetic history ${index + 1}. ${'Windowed content. '.repeat(12)}`,
      id: `long-history-${index + 1}`,
      message_id: `long-history-${index + 1}`,
      timestamp: now - (200 - index) * 5,
    }))

    await page.routeWebSocket(/\/ws$/, ws => {
      socket = ws
      ws.send(eventFrame('connect.challenge', {}))
      ws.onMessage(message => {
        let frame: RpcRequest
        try {
          frame = JSON.parse(String(message)) as RpcRequest
        } catch {
          return
        }
        if (frame.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(hello())
          return
        }
        if (method === 'sessions.messages.subscribe') {
          ws.send(successResponse(frame.id, running
            ? runningSubscription(generation, streamSeq)
            : {
                subscribed: true,
                hydration_complete: true,
                replay_complete: true,
                current_stream_seq: streamSeq,
                stream_generation: generation,
                run_status: 'idle',
                active_task: null,
              }))
          return
        }
        if (method === 'sessions.messages.snapshot') {
          ws.send(successResponse(frame.id, {
            key: SESSION_KEY,
            task_id: running ? TASK_ID : undefined,
            events: [],
            current_stream_seq: streamSeq,
            stream_generation: generation,
            run_status: running ? 'running' : 'idle',
            active_task: running ? { task_id: TASK_ID, status: 'running' } : null,
          }))
          return
        }
        if (method === 'chat.history') {
          ws.send(successResponse(frame.id, {
            messages: history,
            has_more: false,
            canonical_complete: true,
          }))
          return
        }
        ws.send(successResponse(frame.id, basePayload(method)))
      })
    })

    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    const list = page.locator('.chat-message-list')
    const thread = page.locator('.chat-thread')
    await expect(list).toHaveAttribute('data-virtualized', 'true')
    await expect.poll(async () => Number(await list.getAttribute('data-rendered-message-count')))
      .toBeLessThanOrEqual(30)
    await expect.poll(async () => thread.evaluate(element => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))).toBeLessThanOrEqual(2)

    // Chromium native anchoring and the variable-window JS compensator must
    // not both apply an above-viewport height delta. Exercise the real
    // ResizeObserver path and verify a visible row remains fixed to <=2px.
    await thread.hover()
    await page.mouse.wheel(0, -900)
    await expect.poll(async () => thread.evaluate(element => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))).toBeGreaterThan(60)
    const resizeAnchor = await page.evaluate(() => {
      const container = document.querySelector<HTMLElement>('.chat-thread')
      if (!container) throw new Error('chat thread missing')
      const containerRect = container.getBoundingClientRect()
      const rows = Array.from(
        document.querySelectorAll<HTMLElement>('[data-testid="chat-message-row"]'),
      )
      const above = rows.filter(row => row.getBoundingClientRect().bottom <= containerRect.top)
        .at(-1)
      const visible = rows.find(row => {
        const rect = row.getBoundingClientRect()
        return rect.top >= containerRect.top && rect.bottom <= containerRect.bottom
      })
      if (!above || !visible) throw new Error('virtual overscan did not expose anchor rows')
      const aboveKey = above.dataset.chatMessageKey
      const visibleKey = visible.dataset.chatMessageKey
      if (!aboveKey || !visibleKey) throw new Error('virtual row key missing')
      return {
        aboveKey,
        visibleKey,
        visibleTop: visible.getBoundingClientRect().top,
      }
    })
    await page.locator(
      `[data-chat-message-key="${resizeAnchor.aboveKey}"]`,
    ).evaluate(element => {
      (element as HTMLElement).style.paddingTop = '120px'
    })
    await page.waitForTimeout(500)
    const visibleTopAfterResize = await page.locator(
      `[data-chat-message-key="${resizeAnchor.visibleKey}"]`,
    ).evaluate(element => element.getBoundingClientRect().top)
    expect(Math.abs(visibleTopAfterResize - resizeAnchor.visibleTop)).toBeLessThanOrEqual(2)
    await page.locator(
      `[data-chat-message-key="${resizeAnchor.aboveKey}"]`,
    ).evaluate(element => {
      (element as HTMLElement).style.paddingTop = ''
    })
    await page.waitForTimeout(250)
    await page.getByRole('button', { name: 'Jump to latest' }).click()
    await expect.poll(async () => thread.evaluate(element => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))).toBeLessThanOrEqual(2)
    await page.evaluate(() => {
      const metrics = (window as unknown as {
        __longTaskUxMetrics: { inputPaintMs: number[]; longTasksMs: number[] }
      }).__longTaskUxMetrics
      metrics.inputPaintMs.length = 0
      metrics.longTasksMs.length = 0
    })

    // This is a Chromium release gate, so CDP is required rather than an
    // optional performance.memory probe. A missing CDP capability must fail the
    // row instead of silently converting the heap limits into a skipped check.
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Performance.enable')
    await cdp.send('HeapProfiler.enable')
    await cdp.send('HeapProfiler.collectGarbage')
    const beforeHeap = await cdp.send('Runtime.getHeapUsage')
    const beforePerformance = await cdp.send('Performance.getMetrics')
    const metricValue = (
      payload: { metrics: Array<{ name: string; value: number }> },
      name: string,
    ) => payload.metrics.find(metric => metric.name === name)?.value ?? 0
    let heapPhase = 'ready'
    const heapSamples = [{ usedSize: beforeHeap.usedSize, phase: heapPhase }]
    let sampleHeap = true
    const heapSampler = (async () => {
      while (sampleHeap) {
        const usage = await cdp.send('Runtime.getHeapUsage')
        heapSamples.push({ usedSize: usage.usedSize, phase: heapPhase })
        await new Promise<void>(resolve => setTimeout(resolve, 250))
      }
    })()

    const send = (event: string, payload: Record<string, unknown>) => {
      if (!socket) throw new Error('performance fixture websocket is not connected')
      socket.send(eventFrame(event, {
        key: SESSION_KEY,
        task_id: TASK_ID,
        stream_generation: generation,
        stream_seq: ++streamSeq,
        ...payload,
      }))
    }
    // Keep the stream adversarial but consumable by the browser: ~500 wire
    // events/s. The prior unpaced fixture only measured an ever-growing socket
    // backlog and could time out before testing paint or input responsiveness.
    const yieldToBrowser = () => new Promise<void>(resolve => setTimeout(resolve, 20))
    const pace = async (index: number) => {
      if (index % 10 === 9) await yieldToBrowser()
    }
    const closedMarkdownBlock = [
      '## Deterministic stream fixture\n\n',
      '```ts\nconst stable = true\n```\n\n',
      '| A | B |\n|---|---|\n| 1 | 2 |\n\n',
    ].join('')
    // One bounded rich prefix plus a long active tail proves that live output
    // does not reparse the whole 128 KiB answer on all 4,000 deltas.
    const activeTail = Array.from(
      { length: 4_000 },
      (_, index) => `incremental-${String(index).padStart(5, '0')} payload with safe markdown; `,
    ).join('')
    const markdownPayload = (closedMarkdownBlock + activeTail)
      .slice(0, 128 * 1_024)
    expect(markdownPayload).toHaveLength(128 * 1_024)

    let textDeltasSent = 0

    const flood = (async () => {
      heapPhase = 'reasoning'
      send('session.event.tool_use_start', {
        tool_use_id: 'long-task-perf-tool',
        name: 'synthetic_idempotent_tool',
        input: {},
      })
      for (let index = 0; index < 20_000; index += 1) {
        send('session.event.thinking', { text: index % 100 === 0 ? 'r' : '.' })
        await pace(index)
      }
      heapPhase = 'tool-fragments'
      for (let index = 0; index < 10_000; index += 1) {
        send('session.event.tool_use_delta', {
          tool_use_id: 'long-task-perf-tool',
          name: 'synthetic_idempotent_tool',
          fragment: index % 100 === 0 ? '"k":' : '0',
        })
        await pace(index)
      }
      send('session.event.tool_result', {
        tool_use_id: 'long-task-perf-tool',
        name: 'synthetic_idempotent_tool',
        result: 'synthetic result',
        execution_status: { status: 'success' },
      })
      heapPhase = 'answer-stream'
      for (let index = 0; index < 4_000; index += 1) {
        textDeltasSent = index + 1
        send('session.event.text_delta', {
          text: markdownPayload.slice(
            Math.floor(index * markdownPayload.length / 4_000),
            Math.floor((index + 1) * markdownPayload.length / 4_000),
          ),
        })
        await pace(index)
      }
    })()

    const composer = page.locator('.chat-textarea')
    const samples = Array.from({ length: 30 }, (_, index) => `word-${index} `)
    let expectedDraft = ''
    for (const sample of samples) {
      expectedDraft += sample
      await composer.pressSequentially(sample, { delay: 6 })
      await expect(composer).toHaveValue(expectedDraft)
      await page.waitForTimeout(600)
    }

    // Verify follow mode while the answer is actively growing, then move the
    // reader away from the live edge and preserve that exact scroll anchor.
    await expect.poll(() => textDeltasSent, { timeout: 150_000 }).toBeGreaterThan(100)
    await expect(page.getByText(/Deterministic stream fixture/).last()).toBeVisible()
    await expect.poll(async () => thread.evaluate(element => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))).toBeLessThanOrEqual(2)
    const bottomGapWhileFollowing = await thread.evaluate(element => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))
    // Use the browser's actual input path: a synthetic scrollTop assignment can
    // race a stale IntersectionObserver delivery and does not model user intent.
    await thread.hover()
    await page.mouse.wheel(0, -400)
    await expect.poll(async () => thread.evaluate(element => (
      element.scrollHeight - element.scrollTop - element.clientHeight
    ))).toBeGreaterThan(60)
    await page.waitForTimeout(150)
    const upscrollScrollTop = await thread.evaluate(element => element.scrollTop)
    const visualAnchor = await page.evaluate(() => {
      const container = document.querySelector<HTMLElement>('.chat-thread')
      const liveAnswer = document.querySelector<HTMLElement>('.streaming-text-part')
      if (!container) throw new Error('chat thread missing')
      if (!liveAnswer) throw new Error('streaming answer missing')
      const containerRect = container.getBoundingClientRect()
      const walker = document.createTreeWalker(liveAnswer, NodeFilter.SHOW_TEXT)
      let node = walker.nextNode()
      while (node) {
        const text = node.textContent || ''
        const matches = text.matchAll(/incremental-\d{5}/g)
        for (const match of matches) {
          const start = match.index ?? -1
          if (start < 0) continue
          const range = document.createRange()
          range.setStart(node, start)
          range.setEnd(node, start + match[0].length)
          const rect = range.getBoundingClientRect()
          if (rect.bottom > containerRect.top + 8 && rect.top < containerRect.bottom - 8) {
            return { token: match[0], offsetTop: rect.top - containerRect.top }
          }
        }
        node = walker.nextNode()
      }
      throw new Error('visible streaming text anchor missing')
    })
    const readVisualAnchor = async () => page.evaluate((token) => {
      const container = document.querySelector<HTMLElement>('.chat-thread')
      if (!container) throw new Error('chat thread missing')
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
      let node = walker.nextNode()
      while (node) {
        const start = (node.textContent || '').indexOf(token)
        if (start >= 0) {
          const range = document.createRange()
          range.setStart(node, start)
          range.setEnd(node, start + token.length)
          return range.getBoundingClientRect().top - container.getBoundingClientRect().top
        }
        node = walker.nextNode()
      }
      throw new Error(`scroll anchor ${token} was unmounted`)
    }, visualAnchor.token)

    await flood
    heapPhase = 'live-settle'
    // Let the shared rAF publisher render the final shallow snapshot.
    await page.waitForTimeout(250)
    const liveRichBlockCount = await page.locator('.streaming-rich-block').count()
    const liveParseReduction = 1 - ((liveRichBlockCount + 1) / 4_000)
    const scrollTopAfterStream = await thread.evaluate(element => element.scrollTop)
    const anchorAfterStream = await readVisualAnchor()

    history.push({
      role: 'assistant',
      text: markdownPayload,
      id: 'long-perf-final-assistant',
      message_id: 'long-perf-final-assistant',
      timestamp: now,
      turn_context: { turn_id: TASK_ID },
    })
    // Keep real input events in flight across the live -> canonical DOM
    // handoff. This makes terminal anchor capture/restore part of both the
    // long-task and input-to-next-paint release gates.
    const terminalInputSample = 'terminal-handoff '
    expectedDraft += terminalInputSample
    const terminalTyping = composer.pressSequentially(terminalInputSample, { delay: 1 })
    running = false
    heapPhase = 'terminal-handoff'
    send('session.event.done', {
      status: 'succeeded',
      reason: 'completed',
      text_snapshot: markdownPayload,
    })
    await terminalTyping
    await expect(composer).toHaveValue(expectedDraft)
    await expect(page.locator('.streaming-text-part')).toHaveCount(0, { timeout: 10_000 })
    await page.waitForTimeout(500)
    const scrollTopAfterTerminal = await thread.evaluate(element => element.scrollTop)
    const anchorAfterTerminal = await readVisualAnchor()

    sampleHeap = false
    await heapSampler
    const peakHeapSample = heapSamples.reduce((peak, sample) => (
      sample.usedSize > peak.usedSize ? sample : peak
    ))
    const peakHeap = peakHeapSample.usedSize
    const phasePeakHeapDeltaBytes = Object.fromEntries(
      [...new Set(heapSamples.map(sample => sample.phase))].map(phase => [
        phase,
        Math.max(...heapSamples
          .filter(sample => sample.phase === phase)
          .map(sample => sample.usedSize)) - beforeHeap.usedSize,
      ]),
    )
    const postGcHeapSamples: number[] = []
    for (let index = 0; index < 3; index += 1) {
      await cdp.send('HeapProfiler.collectGarbage')
      postGcHeapSamples.push((await cdp.send('Runtime.getHeapUsage')).usedSize)
      await page.waitForTimeout(100)
    }
    const afterHeap = postGcHeapSamples[postGcHeapSamples.length - 1]!
    const afterPerformance = await cdp.send('Performance.getMetrics')

    // Three equal settled turns exercise the real task.running -> delta -> done
    // lifecycle. Force GC after each turn and bound every adjacent retention
    // increase; repeated collections of one turn are not a substitute for this
    // leak-slope gate.
    const retentionTurnPostGcHeapSamples: number[] = []
    const retentionTurnChars = 32 * 1_024
    const retentionTurnDeltas = 512
    for (let turnIndex = 0; turnIndex < 3; turnIndex += 1) {
      const turnTaskId = `long-perf-retention-turn-${turnIndex + 1}`
      const turnPayload = (`Retention turn ${turnIndex + 1}. ` + 'x'.repeat(retentionTurnChars))
        .slice(0, retentionTurnChars)
      running = true
      send('task.running', { task_id: turnTaskId })
      for (let deltaIndex = 0; deltaIndex < retentionTurnDeltas; deltaIndex += 1) {
        send('session.event.text_delta', {
          task_id: turnTaskId,
          text: turnPayload.slice(
            Math.floor(deltaIndex * turnPayload.length / retentionTurnDeltas),
            Math.floor((deltaIndex + 1) * turnPayload.length / retentionTurnDeltas),
          ),
        })
        await pace(deltaIndex)
      }
      history.push({
        role: 'user',
        text: `Synthetic retention prompt ${turnIndex + 1}.`,
        id: `long-perf-retention-user-${turnIndex + 1}`,
        message_id: `long-perf-retention-user-${turnIndex + 1}`,
        timestamp: now + turnIndex + 1,
        turn_context: { turn_id: turnTaskId },
      }, {
        role: 'assistant',
        text: turnPayload,
        id: `long-perf-retention-assistant-${turnIndex + 1}`,
        message_id: `long-perf-retention-assistant-${turnIndex + 1}`,
        timestamp: now + turnIndex + 1,
        turn_context: { turn_id: turnTaskId },
      })
      running = false
      send('session.event.done', {
        task_id: turnTaskId,
        status: 'succeeded',
        reason: 'completed',
        text_snapshot: turnPayload,
      })
      await expect(page.locator('.streaming-text-part')).toHaveCount(0, { timeout: 10_000 })
      await page.waitForTimeout(250)
      await cdp.send('HeapProfiler.collectGarbage')
      retentionTurnPostGcHeapSamples.push(
        (await cdp.send('Runtime.getHeapUsage')).usedSize,
      )
    }
    const retentionTurnGrowthBytes = retentionTurnPostGcHeapSamples
      .slice(1)
      .map((value, index) => value - retentionTurnPostGcHeapSamples[index]!)
    const maxRetentionGrowthPerTurnBytes = Math.max(0, ...retentionTurnGrowthBytes)

    const metrics = await page.evaluate(() => {
      const stored = (window as unknown as {
        __longTaskUxMetrics: { inputPaintMs: number[]; longTasksMs: number[] }
      }).__longTaskUxMetrics
      const sorted = [...stored.inputPaintMs].sort((a, b) => a - b)
      const p95Index = Math.max(0, Math.ceil(sorted.length * 0.95) - 1)
      return {
        inputSamples: sorted.length,
        inputP95: sorted[p95Index] ?? Number.POSITIVE_INFINITY,
        inputMax: Math.max(0, ...sorted),
        longestTask: Math.max(0, ...stored.longTasksMs),
        domNodes: document.querySelectorAll('*').length,
        renderedRows: document.querySelectorAll('[data-testid="chat-message-row"]').length,
        forcedRows: document.querySelectorAll(
          '[data-testid="chat-message-row"][data-chat-message-forced="true"]',
        ).length,
        bodyScrollWidth: document.documentElement.scrollWidth,
        bodyClientWidth: document.documentElement.clientWidth,
      }
    })
    const peakHeapDeltaBytes = Math.max(0, peakHeap - beforeHeap.usedSize)
    const postGcHeapDeltaBytes = Math.max(0, afterHeap - beforeHeap.usedSize)
    const postGcStabilityBytes = Math.max(...postGcHeapSamples) - Math.min(...postGcHeapSamples)
    const recalcStyleCount = metricValue(afterPerformance, 'RecalcStyleCount')
      - metricValue(beforePerformance, 'RecalcStyleCount')
    const layoutCount = metricValue(afterPerformance, 'LayoutCount')
      - metricValue(beforePerformance, 'LayoutCount')
    const upscrollAnchorDrift = Math.max(
      Math.abs(anchorAfterStream - visualAnchor.offsetTop),
      Math.abs(anchorAfterTerminal - visualAnchor.offsetTop),
    )

    const performanceReport = {
        schemaVersion: 1,
        ...metrics,
        ordinaryRows: metrics.renderedRows - metrics.forcedRows,
        bottomGapWhileFollowing,
        anchorToken: visualAnchor.token,
        upscrollAnchor: visualAnchor.offsetTop,
        anchorAfterStream,
        anchorAfterTerminal,
        upscrollScrollTop,
        scrollTopAfterStream,
        scrollTopAfterTerminal,
        upscrollAnchorDrift,
        peakHeapDeltaBytes,
        peakHeapPhase: peakHeapSample.phase,
        phasePeakHeapDeltaBytes,
        postGcHeapDeltaBytes,
        postGcHeapSamples,
        postGcStabilityBytes,
        retentionTurnPostGcHeapSamples,
        retentionTurnGrowthBytes,
        maxRetentionGrowthPerTurnBytes,
        liveRichBlockCount,
        liveParseReduction,
        recalcStyleCount,
        layoutCount,
        fixture: {
          historyMessages: 200,
          textDeltas: 4_000,
          reasoningDeltas: 20_000,
          toolFragments: 10_000,
        },
      }
    const performanceReportPath = testInfo.outputPath('long-task-performance.json')
    await writeFile(performanceReportPath, JSON.stringify(performanceReport, null, 2), 'utf8')
    const externalReportPath = process.env.LONG_TASK_RESILIENCE_REPORT_PATH
    if (externalReportPath) {
      if (!isAbsolute(externalReportPath)) {
        throw new Error('LONG_TASK_RESILIENCE_REPORT_PATH must be an absolute temp path')
      }
      await writeFile(externalReportPath, JSON.stringify(performanceReport, null, 2), 'utf8')
    }
    await testInfo.attach('long-task-performance.json', {
      path: performanceReportPath,
      contentType: 'application/json',
    })

    expect(metrics.inputSamples).toBeGreaterThanOrEqual(samples.length)
    expect(metrics.inputP95).toBeLessThanOrEqual(100)
    expect(metrics.inputMax).toBeLessThanOrEqual(250)
    expect(metrics.longestTask).toBeLessThanOrEqual(200)
    expect(metrics.renderedRows - metrics.forcedRows).toBeLessThanOrEqual(30)
    expect(metrics.domNodes).toBeLessThanOrEqual(15_000)
    expect(metrics.bodyScrollWidth - metrics.bodyClientWidth).toBeLessThanOrEqual(2)
    expect(bottomGapWhileFollowing).toBeLessThanOrEqual(2)
    expect(upscrollAnchorDrift).toBeLessThanOrEqual(2)
    expect(peakHeapDeltaBytes).toBeLessThanOrEqual(48 * 1_024 * 1_024)
    expect(postGcHeapDeltaBytes).toBeLessThanOrEqual(16 * 1_024 * 1_024)
    expect(postGcStabilityBytes).toBeLessThanOrEqual(5 * 1_024 * 1_024)
    expect(maxRetentionGrowthPerTurnBytes).toBeLessThanOrEqual(5 * 1_024 * 1_024)
    expect(liveParseReduction).toBeGreaterThanOrEqual(0.95)
    await cdp.detach()
  })

  test('restores a staged follow-up after refresh and commits it once across two tabs', async ({
    page,
  }) => {
    test.setTimeout(45_000)
    const queuedText = 'Durable queued follow-up survives refresh exactly once.'
    const attachmentName = 'synthetic-follow-up.pdf'
    const syntheticAttachmentBytes = '%PDF-1.4\nsynthetic deterministic attachment\n%%EOF'
    const ephemeralUploadId = 'e2e-upload-capability-must-not-render'
    const durableAttachmentRef = 'a'.repeat(64)
    const generation = 'generation-durable-queue'
    const sockets = new Set<WebSocketRoute>()
    let row: PendingServerRow | null = null
    let running = true
    let streamSeq = 0
    let enqueueCalls = 0
    let pendingListCalls = 0
    let dispatchCommitCount = 0
    const dispatchRequests: Array<Record<string, unknown>> = []
    const transcript: Array<Record<string, unknown>> = [{
      role: 'user',
      text: 'Original long-running task.',
      id: 'durable-original-user',
      message_id: 'durable-original-user',
      timestamp: Math.floor(Date.now() / 1000) - 60,
      turn_context: { turn_id: TASK_ID },
    }]

    const listPayload = () => ({
      sessionKey: SESSION_KEY,
      maxPending: 5,
      items: row
        ? [{
            ...row,
            pending_input_id: row.pendingInputId,
            client_request_id: row.clientRequestId,
            client_message_id: row.clientMessageId,
            request_fingerprint: row.requestFingerprint,
            sessionKey: SESSION_KEY,
            session_key: SESSION_KEY,
            attachments: row.attachments.map(attachment => ({
              name: attachment.name,
              mime: attachment.mime,
              type: attachment.type,
              size: attachment.size,
            })),
            intent: null,
            position: 0,
            schemaVersion: 1,
          }]
        : [],
    })

    const installGateway = async (targetPage: Page) => {
      await preparePage(targetPage)
      await targetPage.routeWebSocket(/\/ws$/, ws => {
        sockets.add(ws)
        ws.onClose(() => sockets.delete(ws))
        ws.send(eventFrame('connect.challenge', {}))
        ws.onMessage(message => {
          let frame: RpcRequest
          try {
            frame = JSON.parse(String(message)) as RpcRequest
          } catch {
            return
          }
          if (frame.type !== 'req') return
          const method = String(frame.method || '')
          if (method === 'connect') {
            ws.send(hello(PENDING_METHODS))
            return
          }
          if (method === 'sessions.messages.subscribe') {
            ws.send(successResponse(frame.id, running
              ? runningSubscription(generation, streamSeq)
              : {
                  subscribed: true,
                  hydration_complete: true,
                  replay_complete: true,
                  current_stream_seq: streamSeq,
                  stream_generation: generation,
                  run_status: 'idle',
                  active_task: null,
                }))
            return
          }
          if (method === 'sessions.messages.snapshot') {
            ws.send(successResponse(frame.id, {
              key: SESSION_KEY,
              task_id: running ? TASK_ID : undefined,
              events: [],
              current_stream_seq: streamSeq,
              stream_generation: generation,
              run_status: running ? 'running' : 'idle',
              active_task: running ? { task_id: TASK_ID, status: 'running' } : null,
            }))
            return
          }
          if (method === 'sessions.messages.hydrate') {
            ws.send(successResponse(frame.id, {
              subscribed: true,
              hydration_complete: true,
              replay_complete: true,
              current_stream_seq: streamSeq,
              stream_generation: generation,
              workspaceId: null,
              run_status: running ? 'running' : 'idle',
              active_task: running ? { task_id: TASK_ID, status: 'running' } : null,
            }))
            return
          }
          if (method === 'chat.history') {
            ws.send(successResponse(frame.id, {
              messages: transcript,
              has_more: false,
              canonical_complete: true,
            }))
            return
          }
          if (method === 'sessions.pending_inputs.list') {
            pendingListCalls += 1
            ws.send(successResponse(frame.id, listPayload()))
            return
          }
          if (method === 'sessions.pending_inputs.enqueue') {
            enqueueCalls += 1
            const params = frame.params || {}
            const pendingInputId = String(params.pendingInputId || '')
            const clientRequestId = String(params.clientRequestId || '')
            const clientMessageId = String(params.clientMessageId || '')
            if (!pendingInputId || !clientRequestId || !clientMessageId) {
              ws.send(errorResponse(
                frame.id,
                'INVALID_PENDING_IDENTITY',
                'missing deterministic identity',
                { accepted: false, retryable: false },
              ))
              return
            }
            const nextFingerprint = `fingerprint:${pendingInputId}`
            const rawAttachments = Array.isArray(params.attachments)
              ? params.attachments as Array<Record<string, unknown>>
              : []
            if (row && (
              row.pendingInputId !== pendingInputId
              || row.clientRequestId !== clientRequestId
              || row.clientMessageId !== clientMessageId
              || row.message !== String(params.message || '')
            )) {
              ws.send(errorResponse(
                frame.id,
                'PENDING_INPUT_CONFLICT',
                'stable id was reused with a different payload',
                { accepted: false, retryable: false },
              ))
              return
            }
            row ||= {
              pendingInputId,
              clientRequestId,
              clientMessageId,
              requestFingerprint: nextFingerprint,
              message: String(params.message || ''),
              attachments: rawAttachments.map(attachment => ({
                name: String(attachment.name || attachment.filename || 'attachment'),
                mime: String(attachment.mime || attachment.type || 'application/octet-stream'),
                type: String(attachment.type || attachment.mime || 'application/octet-stream'),
                size: syntheticAttachmentBytes.length,
                sha256_ref: durableAttachmentRef,
              })),
              revision: 1,
              createdAt: Date.now(),
              updatedAt: Date.now(),
            }
            ws.send(successResponse(frame.id, {
              status: 'staged',
              ...listPayload().items[0],
              replayed: enqueueCalls > 1,
            }))
            return
          }
          if (method === 'sessions.pending_inputs.dispatch') {
            const params = { ...(frame.params || {}) }
            dispatchRequests.push(params)
            const matches = row
              && params.pendingInputId === row.pendingInputId
              && params.clientRequestId === row.clientRequestId
              && params.requestFingerprint === row.requestFingerprint
            if (row && !matches) {
              ws.send(errorResponse(
                frame.id,
                'PENDING_INPUT_CONFLICT',
                'dispatch identity mismatch',
                { accepted: false, retryable: false },
              ))
              return
            }
            if (row) {
              const committed = row
              row = null
              dispatchCommitCount += 1
              transcript.push({
                role: 'user',
                text: committed.message,
                id: committed.clientMessageId,
                message_id: committed.clientMessageId,
                timestamp: Math.floor(Date.now() / 1000),
                turn_context: {
                  turn_id: 'durable-followup-task',
                  client_request_id: committed.clientRequestId,
                },
                attachments: committed.attachments.map(attachment => ({
                  name: attachment.name,
                  mime: attachment.mime,
                  type: attachment.type,
                  size: attachment.size,
                  sha256_ref: attachment.sha256_ref,
                })),
              })
            }
            // A second tab receives the durable ingress receipt replay. Both
            // callers see accepted, while only the first transaction commits.
            ws.send(successResponse(frame.id, {
              accepted: true,
              replayed: dispatchCommitCount > 0 && dispatchRequests.length > 1,
              sessionKey: SESSION_KEY,
              task_id: 'durable-followup-task',
              message_id: transcript[transcript.length - 1]?.message_id,
            }))
            return
          }
          if (method === 'sessions.pending_inputs.cancel') {
            row = null
            ws.send(successResponse(frame.id, { cancelled: true, status: 'cancelled' }))
            return
          }
          ws.send(successResponse(frame.id, basePayload(method)))
        })
      })
    }

    await installGateway(page)
    await page.route('**/api/v1/files/upload', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        file_uuid: ephemeralUploadId,
        filename: attachmentName,
        mime: 'application/pdf',
        size: syntheticAttachmentBytes.length,
        expires_at: Math.floor(Date.now() / 1_000) + 600,
        ttl_seconds: 600,
      }),
    }))
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await dropSyntheticFile(page, {
      name: attachmentName,
      type: 'application/pdf',
      bytes: syntheticAttachmentBytes,
    })
    await expect(page.locator('.attachment-chip')).toContainText(attachmentName)
    await expect(page.locator('.attachment-chip--busy')).toHaveCount(0)
    const composer = page.locator('.chat-textarea')
    await composer.fill(queuedText)
    await composer.press('Enter')
    await expect(page.locator('.chat-pending-card').filter({ hasText: queuedText })).toBeVisible()
    await expect(composer).toHaveValue('')
    await expect.poll(() => enqueueCalls).toBe(1)
    await expect.poll(() => row?.message || '').toBe(queuedText)
    await expect.poll(() => row?.attachments.length || 0).toBe(1)
    await expect(page.locator('.chat-pending-save-status')).toHaveCount(0)
    await expect(page.locator('.chat-pending-attachments')).toContainText('1')
    await expect(page.locator('.chat-pending-attachment-status')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(ephemeralUploadId)
    await expect(page.locator('body')).not.toContainText(syntheticAttachmentBytes)
    await expect.poll(() => page.evaluate(async () => {
      const request = indexedDB.open('opensquilla-chat-pending-inputs')
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      })
      try {
        const transaction = database.transaction('pending_chat_inputs', 'readonly')
        const records = await new Promise<Array<{ state?: string }>>((resolve, reject) => {
          const getAll = transaction.objectStore('pending_chat_inputs').getAll()
          getAll.onsuccess = () => resolve(getAll.result)
          getAll.onerror = () => reject(getAll.error)
        })
        return records.map(record => record.state)
      } finally {
        database.close()
      }
    })).toContain('staged')
    const durableIdentity = row && {
      pendingInputId: row.pendingInputId,
      clientRequestId: row.clientRequestId,
      requestFingerprint: row.requestFingerprint,
    }
    expect(durableIdentity).not.toBeNull()

    const listCallsBeforeReload = pendingListCalls
    await page.reload()
    await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
    await expect.poll(() => pendingListCalls).toBeGreaterThan(listCallsBeforeReload)
    await expect(page.locator('.chat-pending-card').filter({ hasText: queuedText })).toBeVisible()
    await expect(page.locator('.chat-pending-attachments')).toContainText('1')
    await expect(page.locator('.chat-pending-attachment-status')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(ephemeralUploadId)

    const secondPage = await page.context().newPage()
    await installGateway(secondPage)
    try {
      await secondPage.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
      await expect(secondPage.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
      await expect(
        secondPage.locator('.chat-pending-card').filter({ hasText: queuedText }),
      ).toBeVisible()
      await expect(secondPage.locator('.chat-pending-attachments')).toContainText('1')
      await expect(secondPage.locator('.chat-pending-attachment-status')).toHaveCount(0)

      running = false
      const done = eventFrame('session.event.done', {
        key: SESSION_KEY,
        task_id: TASK_ID,
        stream_generation: generation,
        stream_seq: ++streamSeq,
        status: 'succeeded',
        reason: 'completed',
        text_snapshot: '',
      })
      for (const activeSocket of sockets) activeSocket.send(done)

      await expect.poll(() => dispatchRequests.length, { timeout: 10_000 })
        .toBeGreaterThanOrEqual(1)
      await expect.poll(() => dispatchCommitCount).toBe(1)
      await expect(page.locator('.chat-pending-card').filter({ hasText: queuedText }))
        .toHaveCount(0)
      await expect(secondPage.locator('.chat-pending-card').filter({ hasText: queuedText }))
        .toHaveCount(0)

      expect(dispatchRequests.every(request => (
        request.pendingInputId === durableIdentity?.pendingInputId
        && request.clientRequestId === durableIdentity?.clientRequestId
        && request.requestFingerprint === durableIdentity?.requestFingerprint
      ))).toBe(true)
      expect(transcript.filter(message => message.text === queuedText)).toHaveLength(1)
      expect(transcript.filter(message => (
        Array.isArray(message.attachments)
        && message.attachments.some(attachment => (
          (attachment as Record<string, unknown>).name === attachmentName
        ))
      ))).toHaveLength(1)
      expect(row).toBeNull()

      await page.reload()
      await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })
      await expect(page.locator('.msg-user').filter({ hasText: queuedText })).toHaveCount(1)
      await expect(page.locator('.msg-user .msg-attachments .msg-file-chip')).toHaveCount(1)
      await expect(page.locator('.msg-user .msg-file-chip')).toContainText(attachmentName)
      await expect(page.locator('.chat-pending-card').filter({ hasText: queuedText }))
        .toHaveCount(0)
      await expect(page.locator('body')).not.toContainText(ephemeralUploadId)
      await expect(page.locator('body')).not.toContainText(syntheticAttachmentBytes)
    } finally {
      await secondPage.close()
    }
  })
})
