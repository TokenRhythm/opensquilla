import { expect, test, type Locator, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const SESSION_KEY = 'agent:main:webchat:e2e-activity-refresh'
const TURN_ID = 'turn-e2e-activity-refresh'
const BASE_TIME = 1_800_000_000_000
const FIXED_NOW = BASE_TIME + 120_000

function response(id: unknown, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function event(name: string, payload: unknown) {
  return JSON.stringify({ type: 'event', event: name, payload })
}

function streamEvent(name: string, order: number, payload: Record<string, unknown>) {
  return {
    event: `session.event.${name}`,
    payload: {
      session_key: SESSION_KEY,
      task_id: TURN_ID,
      turn_id: TURN_ID,
      stream_seq: order,
      emitted_at: BASE_TIME + order * 100,
      ...payload,
    },
  }
}

const liveEvents = [
  streamEvent('provider_activity', 4, {
    activity_id: 'provider-leg-1', phase: 'requesting', reason: 'initial',
  }),
  streamEvent('provider_activity', 5, {
    activity_id: 'provider-leg-1', phase: 'reasoning', reason: 'initial',
  }),
  streamEvent('thinking_start', 6, {
    block_id: 'reasoning-1', block_index: 0, started_at: BASE_TIME + 600,
  }),
  streamEvent('thinking', 7, {
    block_id: 'reasoning-1', block_index: 0, text: 'Think one',
    started_at: BASE_TIME + 600,
  }),
  streamEvent('thinking_end', 8, {
    block_id: 'reasoning-1', block_index: 0, status: 'completed',
    ended_at: BASE_TIME + 800,
  }),
  streamEvent('text_delta', 31, {
    text: 'Inspect.', presentation: 'intermediate', generation_epoch: 0,
  }),
  streamEvent('tool_use_start', 41, {
    tool_use_id: 'tool-1', tool_name: 'skill_view', generation_epoch: 0,
  }),
  streamEvent('tool_result', 47, {
    tool_use_id: 'tool-1', tool_name: 'skill_view', result: 'ok',
    is_error: false, generation_epoch: 0,
  }),
  streamEvent('provider_activity', 50, {
    activity_id: 'provider-leg-2', phase: 'requesting', reason: 'initial',
  }),
  streamEvent('provider_activity', 51, {
    activity_id: 'provider-leg-2', phase: 'reasoning', reason: 'initial',
  }),
  streamEvent('thinking_start', 52, {
    block_id: 'reasoning-2', block_index: 1, started_at: BASE_TIME + 5_200,
  }),
  streamEvent('thinking', 53, {
    block_id: 'reasoning-2', block_index: 1, text: 'Think two',
    started_at: BASE_TIME + 5_200,
  }),
  streamEvent('thinking_end', 54, {
    block_id: 'reasoning-2', block_index: 1, status: 'completed',
    ended_at: BASE_TIME + 5_400,
  }),
  streamEvent('text_delta', 60, {
    text: 'Final answer.', presentation: 'answer', generation_epoch: 0,
  }),
]

const snapshotEntries = [
  {
    type: 'phase', id: 'provider:requesting:4', order: 4,
    kind: 'provider', phase: 'requesting', at: BASE_TIME + 400,
    ended_at: BASE_TIME + 500,
  },
  {
    type: 'phase', id: 'provider:reasoning:5', order: 5,
    kind: 'provider', phase: 'reasoning', at: BASE_TIME + 500,
    ended_at: BASE_TIME + 5_000,
  },
  {
    type: 'reasoning', id: 'reasoning-1', order: 6, block_index: 0,
    started_at: BASE_TIME + 600, ended_at: BASE_TIME + 800,
    status: 'completed', content_kind: 'reasoning',
    text_start_utf16: 0, text_end_utf16: 9,
  },
  {
    type: 'phase', id: 'write:1:31', order: 31,
    kind: 'write', phase: 'writing', round: 1,
    at: BASE_TIME + 3_100, ended_at: BASE_TIME + 5_000,
  },
  {
    type: 'segment', id: 'text:0', order: 31, segment_type: 'text',
    text_index: 0, text_utf16_length: 8,
    at: BASE_TIME + 3_100, ended_at: BASE_TIME + 3_100,
  },
  {
    type: 'segment', id: 'tool:tool-1', order: 41, segment_type: 'tool',
    tool_use_id: 'tool-1', name: 'skill_view',
    started_at: BASE_TIME + 4_100, ended_at: BASE_TIME + 4_700,
    is_error: false,
  },
  {
    type: 'phase', id: 'provider:requesting:50', order: 50,
    kind: 'provider', phase: 'requesting', at: BASE_TIME + 5_000,
    ended_at: BASE_TIME + 5_100,
  },
  {
    type: 'phase', id: 'provider:reasoning:51', order: 51,
    kind: 'provider', phase: 'reasoning', at: BASE_TIME + 5_100,
    ended_at: BASE_TIME + 6_000,
  },
  {
    type: 'reasoning', id: 'reasoning-2', order: 52, block_index: 1,
    started_at: BASE_TIME + 5_200, ended_at: BASE_TIME + 5_400,
    status: 'completed', content_kind: 'reasoning',
    text_start_utf16: 9, text_end_utf16: 18,
  },
  {
    type: 'phase', id: 'write:2:60', order: 60,
    kind: 'write', phase: 'writing', round: 2,
    at: BASE_TIME + 6_000, ended_at: BASE_TIME + 6_100,
  },
  {
    type: 'segment', id: 'text:1', order: 60, segment_type: 'text',
    text_index: 1, text_utf16_length: 13,
    at: BASE_TIME + 6_000, ended_at: BASE_TIME + 6_100,
  },
]

function historyPayload(settled: boolean) {
  const user = {
    role: 'user', text: 'Inspect and answer.', id: 'user-activity-refresh',
    message_id: 'user-activity-refresh', timestamp: BASE_TIME - 1_000,
    turn_context: { turn_id: TURN_ID },
  }
  if (!settled) return { messages: [user], has_more: false, canonical_complete: true }
  return {
    messages: [user, {
      role: 'assistant', text: 'Final answer.', id: 'assistant-activity-refresh',
      message_id: 'assistant-activity-refresh', timestamp: BASE_TIME + 6_100,
      turn_context: { turn_id: TURN_ID },
      reasoning_content: 'Think oneThink two',
      tool_calls: [
        { type: 'text', text: 'Inspect.' },
        {
          type: 'tool_use', tool_use_id: 'tool-1', name: 'skill_view',
          input: {}, result: 'ok', execution_status: { status: 'success' },
        },
        { type: 'text', text: 'Final answer.' },
      ],
    }],
    turn_outcomes: [{
      turn_id: TURN_ID,
      task_id: TURN_ID,
      status: 'succeeded',
      started_at: BASE_TIME,
      finished_at: BASE_TIME + 6_100,
      outcome: { kind: 'completed' },
      activity_snapshot: {
        version: 2,
        task_id: TURN_ID,
        turn_id: TURN_ID,
        complete: true,
        reasoning_utf16_length: 18,
        entries: snapshotEntries,
      },
    }],
    has_more: false,
    canonical_complete: true,
  }
}

async function installGatewayFixture(page: Page) {
  let settled = false
  let generation = 'gateway-activity-a'

  await page.addInitScript(({ fixedNow }) => {
    Date.now = () => fixedNow
    window.localStorage.setItem('opensquilla-locale', 'en')
    window.localStorage.setItem('opensquilla.chat.foldLiveTurn', '1')
  }, { fixedNow: FIXED_NOW })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [] }),
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    ws.send(event('connect.challenge', {}))
    ws.onMessage(raw => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(raw)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(JSON.stringify({
          protocol: 3,
          policy: { tick_interval_ms: 30_000, concurrent_history_reads: true },
        }))
        return
      }
      const method = String(frame.method || '')
      if (method === 'chat.history') {
        ws.send(response(frame.id, historyPayload(settled)))
        return
      }
      if (method === 'sessions.messages.snapshot') {
        ws.send(response(frame.id, {
          key: SESSION_KEY,
          task_id: settled ? null : TURN_ID,
          stream_generation: generation,
          current_stream_seq: settled ? 0 : 60,
          events: settled ? [] : liveEvents,
        }))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: { enabled: false, rollout_phase: 'observe', tiers: {} },
          permissions: {}, skills: {},
        },
        'models.routing.get': { mode: 'direct' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': {
          subscribed: true,
          replay_complete: true,
          stream_generation: generation,
          current_stream_seq: settled ? 0 : 60,
          run_status: settled ? 'idle' : 'running',
        },
        'sessions.messages.hydrate': {
          hydration_complete: true,
          stream_generation: generation,
          current_stream_seq: settled ? 0 : 60,
          run_status: settled ? 'idle' : 'running',
        },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id, payloads[method] ?? {}))
    })
  })

  return {
    settle() { settled = true },
    restart() { generation = 'gateway-activity-b' },
  }
}

type SemanticRow = { id: string; order: number; type: string; text: string }

async function ensureActivityOpen(activity: Locator) {
  const toggle = activity.locator('button').first()
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
}

async function semanticActivity(page: Page): Promise<SemanticRow[]> {
  const activity = page.getByTestId('assistant-activity').last()
  await expect(activity).toBeVisible()
  await ensureActivityOpen(activity)
  const timeline = activity.getByTestId('assistant-unified-activity-timeline')
  await expect(timeline).toBeVisible()
  return timeline.locator('[data-activity-entry="true"]').evaluateAll(nodes => nodes.map(node => {
    const clone = node.cloneNode(true) as HTMLElement
    return {
      id: node.getAttribute('data-activity-id') || '',
      order: Number(node.getAttribute('data-activity-order')),
      type: node.getAttribute('data-activity-type') || '',
      text: (clone.textContent || '').replace(/\s+/g, ' ').trim(),
    }
  }))
}

test('replays Activity exactly across active reload, terminal reloads, and restart', async ({ page }) => {
  const gateway = await installGatewayFixture(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(SESSION_KEY))
  await expect(page.locator('.conn-pill.connected')).toBeVisible({ timeout: 10_000 })

  const activeBefore = await semanticActivity(page)
  // Live mode intentionally retains only the current routine phase; settled
  // history restores every completed provider/write phase.
  expect(activeBefore.map(row => row.order)).toEqual([6, 31, 41, 52, 60])
  await page.reload()
  const activeAfter = await semanticActivity(page)
  expect(activeAfter).toEqual(activeBefore)

  gateway.settle()
  await page.reload()
  const settled = await semanticActivity(page)
  expect(settled.map(row => row.order)).toEqual([4, 6, 31, 31, 41, 50, 52, 60])
  await expect(page.locator('.assistant-answer')).toHaveText('Final answer.')
  await expect(page.getByTestId('assistant-unified-activity-timeline')).not.toContainText(
    'Final answer.',
  )

  await page.reload()
  expect(await semanticActivity(page)).toEqual(settled)
  gateway.restart()
  await page.reload()
  expect(await semanticActivity(page)).toEqual(settled)
  await expect(page.locator('.assistant-answer')).toHaveCount(1)
})
