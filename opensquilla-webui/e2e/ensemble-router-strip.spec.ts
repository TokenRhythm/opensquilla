import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-ensemble-router'
const STREAM_SESSION_KEY = 'agent:main:webchat:e2e-ensemble-router-streaming'

type Complexity = 'simple' | 'medium' | 'complex'

function wsResponse(id: string, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function wsEvent(event: string, payload: unknown) {
  return JSON.stringify({ type: 'event', event, payload })
}

function messagesFor(complexity: Complexity): Array<Record<string, unknown>> {
  const prompts: Record<Complexity, string> = {
    simple: 'Summarize why model routing matters in one sentence.',
    medium: 'Search today’s AI news and summarize the two most important updates.',
    complex: 'Compare recent AI policy, product, and research changes, then give an action plan.',
  }
  const answers: Record<Complexity, string> = {
    simple: 'Model routing chooses the right model path for a turn.',
    medium: 'The latest AI updates point to faster model releases and more safety scrutiny.',
    complex: 'Across policy, product, and research, the strongest action is to monitor regulation while testing model quality with staged rollout gates.',
  }
  return [
    {
      role: 'user',
      text: prompts[complexity],
      id: `${complexity}-user`,
      message_id: `${complexity}-user`,
      timestamp: Math.floor(Date.now() / 1000) - 90,
    },
    {
      role: 'assistant',
      text: answers[complexity],
      id: `${complexity}-assistant`,
      message_id: `${complexity}-assistant`,
      timestamp: Math.floor(Date.now() / 1000) - 30,
      tool_calls: complexity === 'simple'
        ? []
        : [
            {
              type: 'tool_use',
              tool_use_id: `${complexity}-search`,
              name: 'web_search',
              input: { query: prompts[complexity] },
            },
            {
              type: 'tool_result',
              tool_use_id: `${complexity}-search`,
              name: 'web_search',
              result: JSON.stringify({ results: [{ title: 'AI update', url: 'https://example.com/ai' }] }),
            },
          ],
      usage: {
        model: 'z-ai/glm-5.2',
        cost_usd: complexity === 'complex' ? 0.42 : 0.12,
        model_usage_breakdown: [
          {
            role: 'anchor',
            label: 'Anchor',
            provider: 'openrouter',
            model: 'qwen/qwen3.7-plus',
            input_tokens: 120,
            output_tokens: 30,
          },
          {
            role: 'research',
            label: 'Research',
            provider: 'openrouter',
            model: 'moonshotai/kimi-k2.6',
            input_tokens: 140,
            output_tokens: 42,
          },
          {
            role: 'critic',
            label: 'Critic',
            provider: 'openrouter',
            model: 'z-ai/glm-5.2',
            input_tokens: 90,
            output_tokens: 28,
          },
        ],
        ensemble_trace: {
          profile: 'default',
          mode: 'router_dynamic',
          llm_request_count: 3,
          total_candidates: 8,
          fallback_used: false,
        },
      },
    },
  ]
}

async function mockEnsembleHistory(page: Page, complexity: Complexity) {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
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
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: messagesFor(complexity), has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} },
            llm_ensemble: { enabled: true },
            permissions: {},
            skills: {},
          },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          },
          'usage.status': { sessions: [] },
        }

        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })
}

async function mockStreamingEnsembleRun(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
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
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        if (method === 'chat.send') {
          ws.send(wsResponse(String(frame.id), {
            accepted: true,
            session: STREAM_SESSION_KEY,
            task_id: 'ensemble-stream-task',
            stream_seq: 1,
          }))
          ws.send(wsEvent('task.running', {
            key: STREAM_SESSION_KEY,
            task_id: 'ensemble-stream-task',
            stream_seq: 1,
          }))
          ws.send(wsEvent('session.event.state_change', {
            key: STREAM_SESSION_KEY,
            task_id: 'ensemble-stream-task',
            stream_seq: 2,
            to_state: 'thinking',
          }))
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} },
            llm_ensemble: { enabled: true },
            permissions: {},
            skills: {},
          },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': {
            subscribed: true,
            replay_complete: true,
            current_stream_seq: 0,
            run_status: 'idle',
          },
          'usage.status': { sessions: [] },
        }

        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })
}

const PROGRESS_SESSION_KEY = 'agent:main:webchat:e2e-ensemble-router-progress'

async function mockStreamingEnsembleWithProgress(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
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
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        if (method === 'chat.send') {
          ws.send(wsResponse(String(frame.id), {
            accepted: true,
            session: PROGRESS_SESSION_KEY,
            task_id: 'ensemble-progress-task',
            stream_seq: 1,
          }))
          ws.send(wsEvent('task.running', { key: PROGRESS_SESSION_KEY, task_id: 'ensemble-progress-task', stream_seq: 1 }))
          ws.send(wsEvent('session.event.state_change', { key: PROGRESS_SESSION_KEY, task_id: 'ensemble-progress-task', stream_seq: 2, to_state: 'thinking' }))
          // Two proposers finish; a third stays running — the progressive reveal.
          const prog = (seq: number, p: Record<string, unknown>) =>
            ws.send(wsEvent('session.event.ensemble_progress', { key: PROGRESS_SESSION_KEY, task_id: 'ensemble-progress-task', stream_seq: seq, ...p }))
          prog(3, { event_type: 'proposer_start', proposer_label: 'anchor', proposer_provider: 'openrouter', proposer_model: 'qwen/qwen3.7-plus' })
          prog(4, { event_type: 'proposer_start', proposer_label: 'research', proposer_provider: 'openrouter', proposer_model: 'moonshotai/kimi-k2.6' })
          prog(5, { event_type: 'proposer_finish', proposer_label: 'anchor', proposer_provider: 'openrouter', proposer_model: 'qwen/qwen3.7-plus', input_tokens: 120, output_tokens: 30 })
          prog(6, { event_type: 'proposer_finish', proposer_label: 'research', proposer_provider: 'openrouter', proposer_model: 'moonshotai/kimi-k2.6', input_tokens: 140, output_tokens: 42 })
          prog(7, { event_type: 'proposer_start', proposer_label: 'critic', proposer_provider: 'openrouter', proposer_model: 'z-ai/glm-5.2' })
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} },
            llm_ensemble: { enabled: true },
            permissions: {},
            skills: {},
          },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': { subscribed: true, replay_complete: true, current_stream_seq: 0, run_status: 'idle' },
          'usage.status': { sessions: [] },
        }
        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })
}

const LIFECYCLE_SESSION_KEY = 'agent:main:webchat:e2e-ensemble-lifecycle'

async function mockControlledEnsembleLifecycle(page: Page) {
  let sendFrame: ((frame: string) => void) | null = null
  let streamSeq = 3
  const taskId = 'ensemble-lifecycle-task'

  const emit = (event: string, payload: Record<string, unknown>) => {
    if (!sendFrame) throw new Error('ensemble lifecycle websocket is not connected')
    sendFrame(wsEvent(event, {
      key: LIFECYCLE_SESSION_KEY,
      task_id: taskId,
      stream_seq: streamSeq++,
      ...payload,
    }))
  }

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    sendFrame = frame => ws.send(frame)
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({
            protocol: 3,
            policy: { tick_interval_ms: 30000, webui_stream_idle_grace_ms: 1_260_000 },
          }))
          return
        }
        if (method === 'chat.send') {
          ws.send(wsResponse(String(frame.id), {
            accepted: true,
            session: LIFECYCLE_SESSION_KEY,
            task_id: taskId,
            stream_seq: 1,
          }))
          ws.send(wsEvent('task.running', {
            key: LIFECYCLE_SESSION_KEY,
            task_id: taskId,
            stream_seq: 1,
          }))
          ws.send(wsEvent('session.event.state_change', {
            key: LIFECYCLE_SESSION_KEY,
            task_id: taskId,
            stream_seq: 2,
            to_state: 'thinking',
          }))
          emit('session.event.ensemble_progress', {
            event_type: 'proposer_start', proposer_index: 0, proposer_label: 'anchor',
            proposer_provider: 'openrouter', proposer_model: 'qwen/qwen3.7-plus',
          })
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} },
            llm_ensemble: { enabled: true },
            permissions: {},
            skills: {},
          },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': {
            subscribed: true, replay_complete: true, current_stream_seq: 0, run_status: 'idle',
          },
          'usage.status': { sessions: [] },
        }
        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })

  return { emit }
}

const SCROLL_SESSION_KEY = 'agent:main:webchat:e2e-ensemble-scroll-retention'
const SCROLL_TASK_ID = 'ensemble-scroll-retention-task'
const LATE_TEXT = 'Issue 549 late text delta'
const LATE_MODEL = 'openai/gpt-5.4-mini'

async function mockEnsembleScrollRetention(page: Page) {
  let sendFrame: ((frame: string) => void) | null = null
  let streamSeq = 4

  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    sendFrame = frame => ws.send(frame)
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') {
          ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } }))
          return
        }
        if (method === 'chat.send') {
          ws.send(wsResponse(String(frame.id), {
            accepted: true,
            session: SCROLL_SESSION_KEY,
            task_id: SCROLL_TASK_ID,
            stream_seq: 1,
          }))
          ws.send(wsEvent('task.running', { key: SCROLL_SESSION_KEY, task_id: SCROLL_TASK_ID, stream_seq: 1 }))
          ws.send(wsEvent('session.event.state_change', {
            key: SCROLL_SESSION_KEY,
            task_id: SCROLL_TASK_ID,
            stream_seq: 2,
            to_state: 'streaming',
          }))
          ws.send(wsEvent('session.event.text_delta', {
            key: SCROLL_SESSION_KEY,
            task_id: SCROLL_TASK_ID,
            stream_seq: 3,
            text: Array.from(
              { length: 80 },
              (_, index) => `Initial streaming paragraph ${index + 1}: this fills the conversation so the reader can leave the live edge.`,
            ).join('\n\n'),
          }))
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} },
            llm_ensemble: { enabled: true },
            permissions: {},
            skills: {},
          },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': { subscribed: true, replay_complete: true, current_stream_seq: 0, run_status: 'idle' },
          'usage.status': { sessions: [] },
        }
        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })

  function emit(event: string, payload: Record<string, unknown>) {
    if (!sendFrame) throw new Error('WebSocket fixture is not connected')
    sendFrame(wsEvent(event, {
      key: SCROLL_SESSION_KEY,
      task_id: SCROLL_TASK_ID,
      stream_seq: streamSeq++,
      ...payload,
    }))
  }

  return {
    emitLateText: () => emit('session.event.text_delta', { text: LATE_TEXT }),
    emitLateProgress: () => emit('session.event.ensemble_progress', {
      event_type: 'proposer_start',
      proposer_label: 'late',
      proposer_provider: 'openai',
      proposer_model: LATE_MODEL,
    }),
  }
}

function threadMetrics(page: Page) {
  return page.locator('.chat-thread').evaluate(el => {
    const thread = el as HTMLElement
    return {
      scrollTop: Math.round(thread.scrollTop),
      gap: Math.round(thread.scrollHeight - thread.scrollTop - thread.clientHeight),
      scrollHeight: Math.round(thread.scrollHeight),
    }
  })
}

test('ensemble events do not re-pin a reader who left the live edge', async ({ page }) => {
  const stream = await mockEnsembleScrollRetention(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SCROLL_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.locator('.chat-textarea').fill('Keep streaming while I read earlier content.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  await expect.poll(() => threadMetrics(page).then(metrics => metrics.scrollHeight > 1600)).toBe(true)
  const beforeLateEvents = await page.locator('.chat-thread').evaluate(el => {
    const thread = el as HTMLElement
    thread.scrollTop = Math.max(0, thread.scrollHeight - thread.clientHeight - 300)
    thread.dispatchEvent(new Event('scroll'))
    return {
      scrollTop: Math.round(thread.scrollTop),
      gap: Math.round(thread.scrollHeight - thread.scrollTop - thread.clientHeight),
    }
  })
  expect(beforeLateEvents.gap).toBeGreaterThan(60)
  await expect(page.locator('.chat-jump-latest')).toBeVisible()

  stream.emitLateText()
  await expect(page.locator('.chat-thread')).toContainText(LATE_TEXT)
  const afterLateText = await threadMetrics(page)
  expect(afterLateText.gap).toBeGreaterThan(60)

  stream.emitLateProgress()
  const ensembleStrip = page.locator('.router-fx[data-panel="llm-ensemble"]')
  await ensembleStrip.locator('[data-testid="router-ensemble-toggle"]').click()
  await expect(ensembleStrip.locator('[data-testid="router-ensemble-inspector"]')).toContainText('gpt-5.4-mini')
  const afterLateProgress = await threadMetrics(page)
  expect(afterLateProgress.gap).toBeGreaterThan(60)
  await expect(page.locator('.chat-jump-latest')).toBeVisible()
})

test('ensemble routing reveals members incrementally from ensemble_progress events', async ({ page }) => {
  await mockStreamingEnsembleWithProgress(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(PROGRESS_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })

  await page.locator('.chat-textarea').fill('Compare three model families and synthesize.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  // The live ensemble strip surfaces once members start arriving.
  const strip = page.locator('.router-fx[data-panel="llm-ensemble"]')
  await expect(strip).toBeVisible({ timeout: 10000 })
  // It is live (not settled) — the running animation state.
  await expect(page.locator('.router-fx[data-panel="llm-ensemble"][data-settled="true"]')).toHaveCount(0)

  // Open the candidate trace and confirm every member is revealed.
  await strip.locator('[data-testid="router-ensemble-toggle"]').click()
  const inspector = strip.locator('[data-testid="router-ensemble-inspector"]')
  await expect(inspector).toBeVisible()
  await expect(inspector).toContainText('qwen3.7-plus')
  await expect(inspector).toContainText('kimi-k2.6')
  await expect(inspector).toContainText('glm-5.2')

  // Finished proposers report token usage; the still-running one shows a spinner.
  await expect(inspector.locator('.router-fx-inspector__row[data-status="done"]')).toHaveCount(2)
  await expect(inspector.locator('.router-fx-inspector__row[data-status="running"] .router-fx-inspector__spin')).toHaveCount(1)
})

test('ensemble lifecycle replaces telemetry pending immediately and completes after aggregation', async ({ page }) => {
  const lifecycle = await mockControlledEnsembleLifecycle(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(LIFECYCLE_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })

  await page.locator('.chat-textarea').fill('Compare three slow candidates and synthesize.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  const strip = page.locator('.router-fx[data-panel="llm-ensemble"]')
  await expect(strip).toBeVisible({ timeout: 10000 })
  await strip.locator('[data-testid="router-ensemble-toggle"]').click()
  const inspector = strip.locator('[data-testid="router-ensemble-inspector"]')

  // The first lifecycle frame must replace the empty telemetry state by itself.
  await expect(inspector.locator('.router-fx-inspector__row[data-status="running"]')).toHaveCount(1)
  await expect(inspector).toContainText('qwen3.7-plus')
  await expect(inspector).not.toContainText('telemetry pending')

  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'proposer_start', proposer_index: 1, proposer_label: 'research',
    proposer_provider: 'openrouter', proposer_model: 'moonshotai/kimi-k2.6',
  })
  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'proposer_start', proposer_index: 2, proposer_label: 'critic',
    proposer_provider: 'openrouter', proposer_model: 'z-ai/glm-5.2',
  })
  await expect(inspector.locator('.router-fx-inspector__row[data-status="running"]')).toHaveCount(3)

  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'proposer_finish', proposer_index: 0, proposer_label: 'anchor',
    proposer_provider: 'openrouter', proposer_model: 'qwen/qwen3.7-plus',
    input_tokens: 120, output_tokens: 30, elapsed_ms: 105_000,
  })
  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'proposer_finish', proposer_index: 1, proposer_label: 'research',
    proposer_provider: 'openrouter', proposer_model: 'moonshotai/kimi-k2.6',
    input_tokens: 140, output_tokens: 42, elapsed_ms: 118_000,
  })
  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'proposer_finish', proposer_index: 2, proposer_label: 'critic',
    proposer_provider: 'openrouter', proposer_model: 'z-ai/glm-5.2',
    input_tokens: 160, output_tokens: 48, elapsed_ms: 130_000,
  })
  await expect(inspector.locator('.router-fx-inspector__row[data-status="done"]')).toHaveCount(3)
  await expect(inspector).toContainText('105s')
  await expect(inspector).toContainText('118s')
  await expect(inspector).toContainText('130s')
  await expect(strip.locator('[data-testid="router-ensemble-toggle"]')).toHaveAttribute('aria-busy', 'true')
  await expect(strip.locator('.router-fx-ensemble__scan')).toBeVisible()

  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'aggregator_start', proposer_index: -1, proposer_label: 'aggregator',
    proposer_provider: 'openrouter', proposer_model: 'anthropic/claude-sonnet',
  })
  await expect(inspector.locator('.router-fx-inspector__row[data-status="running"]')).toContainText('claude-sonnet')

  lifecycle.emit('session.event.ensemble_progress', {
    event_type: 'aggregator_finish', proposer_index: -1, proposer_label: 'aggregator',
    proposer_provider: 'openrouter', proposer_model: 'anthropic/claude-sonnet',
    input_tokens: 500, output_tokens: 80, elapsed_ms: 12_000,
  })
  await expect(inspector.locator('.router-fx-inspector__row[data-status="done"]')).toHaveCount(4)
  await expect(inspector).toContainText('12s')
  await expect(strip.locator('[data-testid="router-ensemble-toggle"]')).toHaveAttribute('aria-busy', 'false')
  await expect(strip.locator('.router-fx-ensemble__scan')).toHaveCount(0)

  lifecycle.emit('session.event.text_delta', { text: 'Lifecycle answer complete.' })
  lifecycle.emit('session.event.done', {
    text: 'Lifecycle answer complete.', model: 'ensemble/default', input_tokens: 920, output_tokens: 200,
    model_usage_breakdown: [
      { role: 'proposer', label: 'anchor', provider: 'openrouter', model: 'qwen/qwen3.7-plus', input_tokens: 120, output_tokens: 30, elapsed_ms: 105_000 },
      { role: 'proposer', label: 'research', provider: 'openrouter', model: 'moonshotai/kimi-k2.6', input_tokens: 140, output_tokens: 42, elapsed_ms: 118_000 },
      { role: 'proposer', label: 'critic', provider: 'openrouter', model: 'z-ai/glm-5.2', input_tokens: 160, output_tokens: 48, elapsed_ms: 130_000 },
      { role: 'aggregator', label: 'aggregator', provider: 'openrouter', model: 'anthropic/claude-sonnet', input_tokens: 500, output_tokens: 80, elapsed_ms: 12_000 },
    ],
    ensemble_trace: {
      profile: 'default', total_candidates: 3, llm_request_count: 4, fallback_used: false,
    },
  })
  await expect(page.locator('.chat-thread')).toContainText('Lifecycle answer complete.')
  await expect(strip.locator('[data-testid="router-ensemble-toggle"]')).toContainText('3 candidates synthesized')
  await expect(strip.locator('[data-testid="router-ensemble-toggle"]'))
    .toHaveAttribute('aria-expanded', 'true')
  await expect(inspector).toContainText('105s')
  await expect(inspector).toContainText('118s')
  await expect(inspector).toContainText('130s')
  await expect(inspector).toContainText('12s')
})

const TIER_SESSION_KEY = 'agent:main:webchat:e2e-ensemble-router-tier'

async function mockEnsembleModeWithTierDecision(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') { ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } })); return }
        if (method === 'chat.send') {
          ws.send(wsResponse(String(frame.id), { accepted: true, session: TIER_SESSION_KEY, task_id: 'tier-task', stream_seq: 1 }))
          ws.send(wsEvent('task.running', { key: TIER_SESSION_KEY, task_id: 'tier-task', stream_seq: 1 }))
          ws.send(wsEvent('session.event.state_change', { key: TIER_SESSION_KEY, task_id: 'tier-task', stream_seq: 2, to_state: 'thinking' }))
          // The SquillaRouter tier decision still fires in ensemble mode — it must
          // NOT surface as the candidate grid while ensemble mode is active.
          ws.send(wsEvent('session.event.router_decision', {
            key: TIER_SESSION_KEY, task_id: 'tier-task', stream_seq: 3,
            tier: 'c1', model: 'deepseek/deepseek-v4-pro', source: 'squilla_router', routing_applied: true,
          }))
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': {
            squilla_router: {
              enabled: true, rollout_phase: 'full',
              tiers: {
                c0: { model: 'openai/gpt-5.4-mini' },
                c1: { model: 'deepseek/deepseek-v4-pro' },
                c2: { model: 'z-ai/glm-5.2' },
              },
            },
            llm_ensemble: { enabled: true },
            permissions: {}, skills: {},
          },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': { subscribed: true, replay_complete: true, current_stream_seq: 0, run_status: 'idle' },
          'usage.status': { sessions: [] },
        }
        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })
}

test('ensemble mode does not render the tier candidate grid for the live turn', async ({ page }) => {
  await mockEnsembleModeWithTierDecision(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(TIER_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.locator('.chat-textarea').fill('Synthesize an answer from several models.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  await expect(page.locator('.assistant-activity--live')).toBeVisible({ timeout: 10000 })
  // The regression: a squilla_router tier decision must NOT paint the grid strip
  // while ensemble mode is active — it belongs to the ensemble surface instead.
  await expect(page.locator('.router-fx[data-panel="real-candidates"]')).toHaveCount(0)
  await expect(page.locator('.router-fx[data-panel="legacy-grid"]')).toHaveCount(0)
})

test('ensemble inspector stays open while the answer keeps streaming', async ({ page }) => {
  const KEY = 'agent:main:webchat:e2e-ensemble-inspector-persist'
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.routerVisualEffects', '1')
  })
  await page.route('**/api/approvals', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ pending: [] }) }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      try {
        const frame = JSON.parse(String(message))
        if (frame?.type !== 'req') return
        const method = String(frame.method || '')
        if (method === 'connect') { ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30000 } })); return }
        if (method === 'chat.send') {
          ws.send(wsResponse(String(frame.id), { accepted: true, session: KEY, task_id: 'persist-task', stream_seq: 1 }))
          ws.send(wsEvent('task.running', { key: KEY, task_id: 'persist-task', stream_seq: 1 }))
          ws.send(wsEvent('session.event.state_change', { key: KEY, task_id: 'persist-task', stream_seq: 2, to_state: 'thinking' }))
          const prog = (seq: number, p: Record<string, unknown>) => ws.send(wsEvent('session.event.ensemble_progress', { key: KEY, task_id: 'persist-task', stream_seq: seq, ...p }))
          prog(3, { event_type: 'proposer_finish', proposer_label: 'anchor', proposer_provider: 'openrouter', proposer_model: 'qwen/qwen3.7-plus', input_tokens: 120, output_tokens: 30 })
          prog(4, { event_type: 'proposer_start', proposer_label: 'critic', proposer_provider: 'openrouter', proposer_model: 'z-ai/glm-5.2' })
          // Keep the turn alive: stream answer text deltas AFTER the inspector opens.
          let seq = 5
          const timer = setInterval(() => {
            ws.send(wsEvent('session.event.text_delta', { key: KEY, task_id: 'persist-task', stream_seq: seq++, text: 'streaming ' }))
            if (seq > 12) clearInterval(timer)
          }, 150)
          return
        }
        const payloads: Record<string, unknown> = {
          'agents.list': { agents: [] },
          'chat.history': { messages: [], has_more: false },
          'commands.list_for_surface': { commands: [] },
          'config.get': { squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} }, llm_ensemble: { enabled: true }, permissions: {}, skills: {} },
          'sessions.list': { sessions: [], has_more: false },
          'sessions.messages.subscribe': { subscribed: true, replay_complete: true, current_stream_seq: 0, run_status: 'idle' },
          'usage.status': { sessions: [] },
        }
        ws.send(wsResponse(String(frame.id), payloads[method] ?? {}))
      } catch {}
    })
  })
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  await page.locator('.chat-textarea').fill('Compare and synthesize.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  const strip = page.locator('.router-fx[data-panel="llm-ensemble"]')
  await expect(strip).toBeVisible({ timeout: 10000 })
  await strip.locator('[data-testid="router-ensemble-toggle"]').click()
  const inspector = strip.locator('[data-testid="router-ensemble-inspector"]')
  await expect(inspector).toBeVisible()

  // While answer text keeps streaming (recomputing the message list), the opened
  // inspector must NOT collapse — this is the "detail keeps disappearing" bug.
  await page.waitForTimeout(1000)
  await expect(inspector).toBeVisible()
})

async function openLiveEnsembleInspector(page: Page) {
  await page.locator('.chat-textarea').fill('Compare three model families and synthesize.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  const strip = page.locator('.router-fx[data-panel="llm-ensemble"]')
  await expect(strip).toBeVisible({ timeout: 10000 })
  await strip.locator('[data-testid="router-ensemble-toggle"]').click()
  await expect(strip.locator('[data-testid="router-ensemble-inspector"]')).toBeVisible()
  return strip
}

test('live ensemble strip animates the scan line, pulse dot, and running spinner', async ({ page }) => {
  await mockStreamingEnsembleWithProgress(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(PROGRESS_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  const strip = await openLiveEnsembleInspector(page)

  const scanAnim = await strip.locator('.router-fx-ensemble__scan').evaluate(el => getComputedStyle(el).animationName)
  expect(scanAnim).not.toBe('none')
  const dotAnim = await strip.locator('.router-fx-ensemble__dot').evaluate(el => getComputedStyle(el).animationName)
  expect(dotAnim).not.toBe('none')
  const spinAnim = await strip.locator('.router-fx-inspector__spin').first().evaluate(el => getComputedStyle(el).animationName)
  expect(spinAnim).not.toBe('none')
})

test('live ensemble strip disables all animation under prefers-reduced-motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await mockStreamingEnsembleWithProgress(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(PROGRESS_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })
  const strip = await openLiveEnsembleInspector(page)

  const scanAnim = await strip.locator('.router-fx-ensemble__scan').evaluate(el => getComputedStyle(el).animationName)
  expect(scanAnim).toBe('none')
  const dotAnim = await strip.locator('.router-fx-ensemble__dot').evaluate(el => getComputedStyle(el).animationName)
  expect(dotAnim).toBe('none')
  const spinAnim = await strip.locator('.router-fx-inspector__spin').first().evaluate(el => getComputedStyle(el).animationName)
  expect(spinAnim).toBe('none')
})

for (const complexity of ['simple', 'medium', 'complex'] as const) {
  test(`completed ensemble trace stays in assistant meta for ${complexity} prompts`, async ({ page }) => {
    await mockEnsembleHistory(page, complexity)
    await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(`${SESSION_KEY}-${complexity}`))
    await page.waitForSelector('.conn-pill', { timeout: 10000 })

    await expect(page.locator('.router-fx[data-panel="llm-ensemble"]')).toHaveCount(0)

    const assistant = page.locator('.msg-ai').first()
    await expect(assistant.locator('.msg-meta__ensemble')).toContainText('Ensemble · 3 models')
    await expect(assistant).not.toContainText('qwen3.7-plus')
    await expect(assistant).not.toContainText('kimi-k2.6')

    await assistant.locator('.msg-meta__more-btn').click()

    const details = assistant.locator('.msg-meta-popover')
    await expect(details).toBeVisible()
    await expect(details).toContainText('qwen3.7-plus')
    await expect(details).toContainText('kimi-k2.6')
    await expect(details).toContainText('glm-5.2')
  })
}

test('live ensemble routing shows the strip alongside flat activity', async ({ page }) => {
  await mockStreamingEnsembleRun(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(STREAM_SESSION_KEY))
  await page.waitForSelector('.conn-pill', { timeout: 10000 })

  await page.locator('.chat-textarea').fill('Build a tiny team collaboration app.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()

  // The flat activity surface runs its normal execution phase...
  await expect(page.locator('.assistant-activity--live')).toBeVisible({ timeout: 10000 })
  // ...and the ensemble strip is surfaced independently — here as the pre-decision
  // reserve, before any member arrives — instead of being hidden behind it.
  await expect(page.locator('.router-fx[data-panel="llm-ensemble"]')).toHaveCount(1)
  // It is live, not settled.
  await expect(page.locator('.router-fx[data-panel="llm-ensemble"][data-settled="true"]')).toHaveCount(0)
})
