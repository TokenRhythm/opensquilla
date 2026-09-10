import { expect, test, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:e2e-questionnaire'
const TASK = 'task-questionnaire'
const REQUEST = 'request-questionnaire'
const GENERATION = 'questionnaire-generation'

async function installGateway(page: Page) {
  let socket: WebSocketRoute | undefined
  let seq = 0
  let submitId: string | undefined
  let pending = true
  let restoreTerminalHistory = false
  let terminalStatus = 'cancelled'
  const submissions: Record<string, unknown>[] = []
  const questionnaire = {
    kind: 'user_input', paused: true, request_id: REQUEST, run_id: TASK, step: 'clarify',
    clarify_schema: {
      presentation: 'plan_questionnaire_v1', intro: 'Choose a scope.',
      fields: [{ name: 'scope', type: 'enum', required: true,
        prompt: 'Which scope?', choices: ['Focused', 'Complete'] }],
    },
  }
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/approvals', route => route.fulfill({ json: { mode: 'prompt', pending: [] } }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({ status: 204, body: '' }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    const reply = (id: string, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(message => {
      const frame = JSON.parse(String(message))
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({
          server: { version: 'e2e', conn_id: 'questionnaire-fixture' },
          features: { methods: ['chat.clarify_submit'], events: [] },
          snapshot: {}, policy: { tick_interval_ms: 30_000 },
          auth: { principal: { isOwner: true } },
        }))
        return
      }
      if (frame.method === 'chat.clarify_submit') {
        submitId = frame.id
        submissions.push(frame.params)
        return
      }
      const metadata = {
        epoch: 1,
        pendingUserInputs: pending ? [questionnaire] : [],
        run_status: pending ? 'running' : 'idle',
        active_task: pending ? { task_id: TASK, status: 'running' } : null,
        collaboration: { mode: 'plan', revision: 1 },
      }
      const payloads: Record<string, unknown> = {
        'chat.history': restoreTerminalHistory ? chatHistoryPayload([{
          role: 'assistant', text: '', message_id: 'message-questionnaire',
          timestamp: 1_768_464_010, turn_context: { turn_id: 'turn-questionnaire' },
          tool_calls: [{ type: 'tool_result', tool_use_id: 'call-questionnaire',
            name: 'request_user_input', result: JSON.stringify(questionnaire) }],
        }], { turn_outcomes: [{
          turn_id: 'turn-questionnaire', task_id: TASK, status: terminalStatus,
          started_at: 1_768_464_000, finished_at: 1_768_464_010,
          outcome: { kind: terminalStatus },
        }] }) : chatHistoryPayload(),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(SESSION, {
          ...metadata, current_stream_seq: seq, stream_generation: GENERATION,
        }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(SESSION, metadata),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(SESSION, {
          current_stream_seq: seq, stream_generation: GENERATION,
        }),
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'direct' },
        'onboarding.status': { audioConfigured: false },
        'sessions.list': { sessions: [], count: 0, ts: 1_800_000_000, has_more: false },
        'usage.status': { sessions: [] },
      }
      reply(frame.id, payloads[frame.method] ?? {})
    })
  })
  const emit = (event: string, payload: Record<string, unknown> = {}) => {
    if (!socket) throw new Error('Questionnaire fixture has no connection')
    socket.send(JSON.stringify({ type: 'event', event, payload: {
      key: SESSION, session_key: SESSION, task_id: TASK, epoch: 1,
      stream_generation: GENERATION, stream_seq: ++seq, ...payload,
    } }))
  }
  return {
    submissions,
    restoreTerminalHistory() { restoreTerminalHistory = true },
    settle(event: string) {
      pending = false
      if (event === 'task.timeout') {
        // Failed tasks immediately synchronize their durable transcript.
        // An authoritative empty history would correctly remove the live row.
        terminalStatus = 'timeout'
        restoreTerminalHistory = true
      }
      emit(event, { reason: 'aborted' })
    },
    replay() {
      emit('session.event.tool_result', {
        tool_use_id: 'call-questionnaire', name: 'request_user_input',
        result: JSON.stringify(questionnaire),
      })
    },
    answer(accepted: boolean) {
      if (!socket || !submitId) throw new Error('No submitted questionnaire')
      pending = false
      socket.send(JSON.stringify(accepted
        ? { type: 'res', id: submitId, ok: true, payload: { resolved: true, request_id: REQUEST } }
        : { type: 'res', id: submitId, ok: false,
            error: { code: 'USER_INPUT_EXPIRED', message: 'The user-input request is no longer available.' } }))
    },
  }
}

test('refresh keeps a cancelled questionnaire unavailable from persisted history', async ({ page }) => {
  const { gateway, dock } = await openQuestionnaire(page)
  gateway.settle('task.cancelled')
  await expect(dock).toHaveCount(0)
  gateway.restoreTerminalHistory()
  await page.reload()
  await expect(page.getByText('Question expired', { exact: true })).toBeVisible()
  await expect(page.getByTestId('clarify-card')).toHaveCount(0)
  await expect(page.locator('.chat-textarea')).toBeEnabled()
})

async function openQuestionnaire(page: Page) {
  const gateway = await installGateway(page)
  await page.goto(`/control/chat?session=${encodeURIComponent(SESSION)}`)
  const dock = page.locator('.plan-questionnaire-dock')
  await expect(dock).toBeVisible()
  await expect(page.locator('.chat-textarea')).toBeDisabled()
  return { gateway, dock }
}

async function expectReceipt(page: Page, label: string) {
  const receipt = page.getByText(label, { exact: true })
  await expect(receipt).toBeAttached()
  // Resolved timeline interrupts move into the collapsed Activity disclosure.
  if (!await receipt.isVisible()) {
    await page.getByRole('button', { name: /^Activity/ }).click()
  }
  await expect(receipt).toBeVisible()
}

for (const terminal of ['task.cancelled', 'task.timeout', 'session.event.done']) {
  test(`questionnaire releases the composer on ${terminal} and ignores late replay`, async ({ page }) => {
    const { gateway, dock } = await openQuestionnaire(page)
    gateway.settle(terminal)
    await expect(dock).toHaveCount(0)
    await expect(page.locator('.chat-textarea')).toBeEnabled()
    await expectReceipt(page, 'Question expired')
    gateway.replay()
    await expect(page.getByTestId('clarify-card')).toHaveCount(0)
    await page.locator('.chat-textarea').fill('Continue with a new request.')
    expect(gateway.submissions).toHaveLength(0)
  })
}

for (const accepted of [true, false]) {
  test(`questionnaire waits for ${accepted ? 'accepted' : 'expired'} submission acknowledgement`, async ({ page }) => {
    const { gateway, dock } = await openQuestionnaire(page)
    await dock.getByRole('radio', { name: /Focused/ }).check()
    await dock.getByRole('button', { name: 'Send reply', exact: true }).click()
    await expect.poll(() => gateway.submissions.length).toBe(1)
    await expect(dock.getByRole('button', { name: 'Sending reply…', exact: true })).toBeDisabled()
    await expect(page.getByText('Reply received', { exact: true })).toHaveCount(0)
    gateway.answer(accepted)
    await expect(dock).toHaveCount(0)
    await expect(page.locator('.chat-textarea')).toBeEnabled()
    await expectReceipt(page, accepted ? 'Reply received' : 'Question expired')
  })
}
