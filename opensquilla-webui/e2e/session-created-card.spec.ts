import { expect, test, type Page } from '@playwright/test'

const CONTROL_URL = '/control/'
const PARENT_KEY = 'agent:main:webchat:e2e-session-created-parent'
const FIRST_CHILD_KEY = 'agent:main:subagent:first123'
const SECOND_CHILD_KEY = 'agent:main:subagent:second45'

function response(id: string | number | undefined, payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function errorResponse(id: string | number | undefined, code: string, message: string) {
  return JSON.stringify({
    type: 'res',
    id,
    ok: false,
    error: { code, message, retryable: false },
  })
}

async function mockSessionCreatedHistory(
  page: Page,
  options: { liveHandoff?: boolean, deletedFirstChild?: boolean } = {},
) {
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
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(message)) as Record<string, unknown>
      } catch {
        return
      }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      const params = frame.params && typeof frame.params === 'object'
        ? frame.params as Record<string, unknown>
        : {}
      if (method === 'connect') {
        ws.send(JSON.stringify({ protocol: 3, policy: { tick_interval_ms: 30_000 } }))
        return
      }
      if (method === 'chat.history') {
        const sessionKey = String(params.sessionKey || '')
        if (options.deletedFirstChild && sessionKey === FIRST_CHILD_KEY) {
          ws.send(errorResponse(
            frame.id as string | number | undefined,
            'NOT_FOUND',
            'Session not found',
          ))
          return
        }
        if (sessionKey === PARENT_KEY) {
          if (options.liveHandoff) {
            ws.send(response(frame.id as string | number | undefined, {
              messages: [{
                role: 'user',
                text: 'Create a child chat',
                id: 'live-session-created-user',
                timestamp: Math.floor(Date.now() / 1000) - 3,
                turn_context: { turn_id: 'creation-turn' },
              }, {
                role: 'router',
                text: '',
                id: 'live-resume-router',
                timestamp: Math.floor(Date.now() / 1000) - 2,
                turn_context: { turn_id: 'resume-turn' },
                provenance_kind: 'router_decision',
                router_decision: {
                  tier: 'c1',
                  model: 'deepseek-v4-pro',
                  source: 'heuristic',
                },
              }, {
                role: 'assistant',
                text: '',
                id: 'live-session-created-assistant',
                timestamp: Math.floor(Date.now() / 1000) - 1,
                turn_context: { turn_id: 'creation-turn' },
                tool_calls: [{
                  tool_use_id: 'spawn-first',
                  name: 'sessions_spawn',
                  result: JSON.stringify({
                    session_key: FIRST_CHILD_KEY,
                    status: 'queued',
                    title: 'Inspect first child',
                  }),
                  execution_status: { status: 'success' },
                }],
              }],
              has_more: false,
              canonical_available: true,
              canonical_complete: true,
            }))
            return
          }
          ws.send(response(frame.id as string | number | undefined, {
            messages: [{
              role: 'user',
              text: 'Create two child chats',
              id: 'session-created-user',
              timestamp: Math.floor(Date.now() / 1000) - 2,
              turn_context: { turn_id: 'creation-turn' },
            }, {
              role: 'assistant',
              text: 'Child chats created; waiting for completion.',
              id: 'session-created-assistant',
              timestamp: Math.floor(Date.now() / 1000) - 1,
              turn_context: { turn_id: 'creation-turn' },
              usage: {
                routed_tier: 'c0',
                routed_model: 'deepseek-v4-flash',
                routing_source: 'heuristic',
                routing_applied: true,
              },
              tool_calls: [{
                tool_use_id: 'spawn-first',
                name: 'sessions_spawn',
                result: JSON.stringify({
                  session_key: FIRST_CHILD_KEY,
                  status: 'queued',
                  title: 'Inspect first child',
                }),
                execution_status: { status: 'success' },
              }, {
                tool_use_id: 'spawn-second',
                name: 'sessions_spawn',
                result: JSON.stringify({
                  session_key: SECOND_CHILD_KEY,
                  status: 'queued',
                  title: 'Verify second child',
                }),
                execution_status: { status: 'success' },
              }],
            }, {
              role: 'system',
              text: JSON.stringify({
                type: 'subagent_completion',
                child_session_key: FIRST_CHILD_KEY,
                result: 'first child done',
              }),
              id: 'subagent-completion-first',
              timestamp: Math.floor(Date.now() / 1000),
              turn_context: { turn_id: 'creation-turn' },
              provenance_kind: 'internal_system',
              provenance_source_tool: 'subagent_completion',
              provenance_source_session_key: FIRST_CHILD_KEY,
            }, {
              role: 'system',
              text: JSON.stringify({
                type: 'subagent_completion',
                child_session_key: SECOND_CHILD_KEY,
                result: 'second child done',
              }),
              id: 'subagent-completion-second',
              timestamp: Math.floor(Date.now() / 1000),
              turn_context: { turn_id: 'creation-turn' },
              provenance_kind: 'internal_system',
              provenance_source_tool: 'subagent_completion',
              provenance_source_session_key: SECOND_CHILD_KEY,
            }, {
              role: 'assistant',
              text: 'Parent final reply',
              id: 'parent-final-assistant',
              timestamp: Math.floor(Date.now() / 1000) + 1,
              turn_context: { turn_id: 'resume-turn' },
              usage: {
                routed_tier: 'c1',
                routed_model: 'deepseek-v4-pro',
                routing_source: 'heuristic',
                routing_applied: true,
              },
            }, {
              role: 'user',
              text: 'A later separate question',
              id: 'later-user',
              timestamp: Math.floor(Date.now() / 1000) + 2,
              turn_context: { turn_id: 'later-turn' },
            }, {
              role: 'assistant',
              text: 'A later separate answer',
              id: 'later-assistant',
              timestamp: Math.floor(Date.now() / 1000) + 3,
              turn_context: { turn_id: 'later-turn' },
            }],
            has_more: false,
            canonical_available: true,
            canonical_complete: true,
          }))
          return
        }
        ws.send(response(frame.id as string | number | undefined, {
          messages: [{
            role: 'assistant',
            text: `Opened child session ${sessionKey}`,
            id: 'child-session-assistant',
            timestamp: Math.floor(Date.now() / 1000),
            usage: {
              model: 'deepseek-v4-pro',
              routed_model: 'deepseek-v4-pro',
              routing_source: 'none',
              routing_applied: true,
            },
          }],
          has_more: false,
          canonical_available: true,
          canonical_complete: true,
        }))
        return
      }
      if (method === 'sessions.messages.snapshot') {
        ws.send(response(frame.id as string | number | undefined, {
          key: String(params.key || ''),
          events: [],
          current_stream_seq: 0,
        }))
        return
      }
      if (method === 'sessions.resolve') {
        const key = String(params.key || '')
        if (options.deletedFirstChild && key === FIRST_CHILD_KEY) {
          ws.send(errorResponse(
            frame.id as string | number | undefined,
            'NOT_FOUND',
            'Session not found',
          ))
        } else {
          ws.send(response(frame.id as string | number | undefined, { session_key: key }))
        }
        return
      }
      if (
        method === 'sessions.messages.subscribe'
        && options.deletedFirstChild
        && String(params.key || '') === FIRST_CHILD_KEY
      ) {
        ws.send(errorResponse(
          frame.id as string | number | undefined,
          'SESSION_NOT_FOUND',
          'Session was deleted or does not exist.',
        ))
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': {
          squilla_router: {
            enabled: true,
            rollout_phase: 'full',
            tiers: {
              c0: { model: 'deepseek-v4-flash' },
              c1: { model: 'deepseek-v4-pro' },
            },
          },
          permissions: {},
          skills: {},
        },
        'models.routing.get': { mode: 'router' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], has_more: false },
        'sessions.messages.subscribe': {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: options.liveHandoff ? 'running' : 'idle',
          active_task: options.liveHandoff
            ? { task_id: 'resume-turn', status: 'running' }
            : null,
        },
        'sessions.messages.hydrate': {
          subscribed: true,
          replay_complete: true,
          current_stream_seq: 0,
          run_status: options.liveHandoff ? 'running' : 'idle',
          active_task: options.liveHandoff
            ? { task_id: 'resume-turn', status: 'running' }
            : null,
        },
        'usage.status': { sessions: [] },
      }
      ws.send(response(frame.id as string | number | undefined, payloads[method] ?? {}))
    })
  })
}

test('restores ordered created-chat cards and opens the selected child session', async ({ page }) => {
  await mockSessionCreatedHistory(page)
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(PARENT_KEY))

  const cards = page.getByTestId('session-created-card')
  await expect(cards).toHaveCount(2)
  await expect(cards.nth(0)).toHaveAttribute('data-session-key', FIRST_CHILD_KEY)
  await expect(cards.nth(1)).toHaveAttribute('data-session-key', SECOND_CHILD_KEY)
  await expect(cards.nth(0)).toContainText('Inspect first child')
  await expect(cards.nth(1)).toContainText('Verify second child')
  await expect(page.getByText('session_key')).toHaveCount(0)
  await expect(page.getByText('Sub-agent', { exact: true })).toHaveCount(0)
  await expect(page.getByText('subagent_completion')).toHaveCount(0)
  await expect(page.getByRole('group', { name: 'Router selected deepseek-v4-flash' })).toHaveCount(1)
  await expect(page.locator('.chat-message-surface .router-fx')).toHaveCount(1)
  const finalReply = page.locator('.msg-ai').filter({ hasText: 'Parent final reply' })
  await expect(finalReply.getByTestId('session-created-card')).toHaveCount(2)
  await expect.poll(async () => {
    const text = await finalReply.innerText()
    return text.indexOf('Parent final reply') < text.indexOf('Inspect first child')
  }).toBe(true)

  await page.reload()
  await expect(page.locator('.msg-ai').filter({ hasText: 'Parent final reply' })
    .getByTestId('session-created-card')).toHaveCount(2)

  await page.getByTestId('session-created-card').nth(1).getByRole('button', {
    name: 'Open chat',
  }).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SECOND_CHILD_KEY)
  await expect(page.getByText(`Opened child session ${SECOND_CHILD_KEY}`)).toBeVisible()
  await expect(page.getByRole('group', { name: 'Router selected deepseek-v4-pro' })).toBeVisible()
})

test('disables a deleted child card and keeps the sibling openable', async ({ page }) => {
  await mockSessionCreatedHistory(page, { deletedFirstChild: true })
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(PARENT_KEY))

  const cards = page.getByTestId('session-created-card')
  await expect(cards).toHaveCount(2)
  await expect(cards.nth(0)).toHaveAttribute('data-session-state', 'available')
  await expect(cards.nth(0).getByRole('button', { name: 'Open chat' })).toBeEnabled()
  await cards.nth(0).getByRole('button', { name: 'Open chat' }).click()
  await expect(cards.nth(0)).toHaveAttribute('data-session-state', 'missing')
  await expect(cards.nth(0)).toContainText('Inspect first child')
  await expect(cards.nth(0)).toContainText('Deleted')
  await expect(cards.nth(0).getByRole('button', { name: 'Deleted' })).toBeDisabled()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(PARENT_KEY)
  await expect(cards.nth(1)).toHaveAttribute('data-session-state', 'available')

  await cards.nth(1).getByRole('button', { name: 'Open chat' }).click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SECOND_CHILD_KEY)
})

test('shows one terminal state for a missing child deep link', async ({ page }) => {
  await mockSessionCreatedHistory(page, { deletedFirstChild: true })
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(FIRST_CHILD_KEY))

  await expect(page.getByText('Session was deleted or does not exist')).toBeVisible()
  await expect(page.getByText('Conversation history temporarily unavailable')).toHaveCount(0)
  await expect(page.getByText('Turn failed')).toHaveCount(0)
})

test('keeps only the router above a created-chat card during the parent handoff', async ({ page }) => {
  await mockSessionCreatedHistory(page, { liveHandoff: true })
  await page.goto(CONTROL_URL + 'chat?session=' + encodeURIComponent(PARENT_KEY))

  await expect(page.getByTestId('session-created-card')).toHaveCount(1)
  await expect(page.locator('.router-fx')).toHaveCount(1)
  await expect(page.locator('.router-fx-reserve')).toHaveCount(0)
  await expect(page.locator('.chat-message-surface .router-fx')).toHaveCount(1)
  await expect.poll(async () => page.evaluate(() => {
    const router = document.querySelector('.chat-message-surface .router-fx')
    const card = document.querySelector('[data-testid="session-created-card"]')
    return Boolean(router && card && router.getBoundingClientRect().top < card.getBoundingClientRect().top)
  })).toBe(true)
})
