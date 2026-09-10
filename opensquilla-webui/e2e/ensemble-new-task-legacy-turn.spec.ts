import { expect, type Page } from '@playwright/test'
import { test } from './support/native-desktop-fixture'
import { helloOkResponse } from './support/gateway-fixture'
import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const SESSION = 'agent:main:webchat:legacy-ensemble-new-task'
const TITLE = 'Synthetic legacy ensemble task'
const FIRST_PROMPT = 'Compare the first three synthetic candidates.'
const SECOND_PROMPT = 'Compare a second set in this same task.'
const FRESH_PROMPT = 'Compare three candidates in a fresh task.'

// Native Electron setup starts its isolated source Gateway before the body.
test.setTimeout(180_000)
type WireObject = Record<string, unknown>

async function readPresentation(page: Page) {
  return page.locator('.chat').evaluate(element => {
    // Observation only: UI actions and actual RPC frames own every mutation.
    const instance = (element as unknown as { __vueParentComponent?: {
      setupState?: Record<string, unknown>
    } }).__vueParentComponent
    const state = instance?.setupState
    const messages = state?.messages
    const rendered = state?.renderedMessages
    return {
      raw: Array.isArray(messages) ? {
        sessionKey: state?.sessionKey,
        intent: state?.pendingSessionIntent,
        streaming: state?.isStreaming,
        messages: messages.map((message: WireObject) => ({
          role: message.role, text: message.text, messageId: message.messageId,
          clientId: message.clientId, turnId: message.turnId,
          router: Boolean(message.routerDecision), routerDecision: message.routerDecision, routerState: message.routerState, routerModelCallId: message.routerModelCallId,
        })),
        rendered: Array.isArray(rendered) ? rendered.map((message: WireObject) => ({
          role: message.displayRole, text: message.text, messageId: message.messageId,
          turnKey: message.turnKey, routerKey: message.routerTurnKey,
          router: Boolean(message.isRouterStrip),
        })) : null,
      } : null,
      landing: element.classList.contains('chat--new-landing'),
      cards: Array.from(element.querySelectorAll('.router-fx')).map(card => ({
        text: card.textContent,
        rowKey: card.closest('[data-chat-message-key]')?.getAttribute('data-chat-message-key'),
      })),
      previewSession: element.querySelector('[data-preview-session]')?.getAttribute('data-preview-session') ?? null,
    }
  })
}

test('New Task removes ensemble DOM after a late legacy progress from an earlier turn', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  page.setDefaultTimeout(10_000)
  const native = process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP === '1'
  const inspectRawState = !native && process.env.OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI === '1'
  const root = native ? 'opensquilla-app://desktop/' : '/control/'
  const timestamp = Math.floor(Date.now() / 1000)
  const consoleMessages: Array<{ type: string; text: string }> = []
  const wireEvents: Array<{ event: string; payload: WireObject }> = []
  const sends: Array<{ key: string; taskId: string; turnId: string; text: string }> = []
  const heldHistory: Array<() => void> = []
  const subscriptions: string[] = []
  let seq = 0
  let running = false
  let releaseHistory = false
  let emit: (event: string, payload: WireObject) => void = () => { throw new Error('not connected') }
  page.on('console', message => {
    if (['warning', 'error'].includes(message.type())) {
      consoleMessages.push({ type: message.type(), text: message.text() })
    }
  })
  page.on('pageerror', error => consoleMessages.push({ type: 'pageerror', text: error.message }))

  const ensembleUsage = {
    model: 'synthetic/fusion', input_tokens: 12, output_tokens: 8,
    model_usage_breakdown: ['anchor', 'research', 'critic', 'aggregator'].map(role => ({
      role, provider: 'synthetic', model: role === 'aggregator' ? 'fusion' : role,
      input_tokens: 3, output_tokens: 2,
    })),
    ensemble_trace: { profile: 'default', mode: 'router_dynamic',
      llm_request_count: 4, total_candidates: 3, fallback_used: false },
  }
  const identity = (index: number) => {
    const send = sends[index]!
    return { key: send.key, task_id: send.taskId, turn_id: send.turnId }
  }
  const complete = (index: number) => {
    const text = `Synthetic completed answer ${index + 1}.`
    emit('session.event.text_delta', { ...identity(index), text })
    emit('session.event.state_change', { ...identity(index), to_state: 'completed', final_text: text })
    emit('session.event.done', { ...identity(index), final_text: text,
      ...(index === 0 ? { usage: ensembleUsage } : {}) })
    running = false
  }
  const historyPrefix = Array.from({ length: 100 }, (_, index) => ({
    role: index % 2 ? 'assistant' : 'user', text: `Synthetic earlier history row ${index}.`,
    id: `earlier-${index}`, message_id: `earlier-${index}`,
    turn_context: { turn_id: `earlier-turn-${Math.floor(index / 2)}` },
    timestamp: timestamp - 200 + index,
  }))
  const history = () => chatHistoryPayload([...historyPrefix, ...sends.filter(send => send.key === SESSION).flatMap((send, index) => [
    { role: 'user', text: send.text, id: `history-user-${index}`, message_id: `history-user-${index}`, turn_context: { turn_id: send.turnId }, timestamp: timestamp + index * 2 },
    { role: 'assistant', text: `Synthetic completed answer ${index + 1}.`, id: `history-answer-${index}`, message_id: `history-answer-${index}`, turn_context: { turn_id: send.turnId }, timestamp: timestamp + index * 2 + 1,
      ...(index === 0 ? { usage: ensembleUsage } : {}) },
  ])])
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({
    json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] },
  }))
  await page.routeWebSocket(/\/ws$/, ws => {
    const respond = (id: string, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    emit = (event, payload) => {
      const frame = { event, payload: { stream_seq: ++seq, ...payload } }
      wireEvents.push(frame)
      ws.send(JSON.stringify({ type: 'event', ...frame }))
    }
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ auth: { principal: { isOwner: true } } }))
        return
      }
      if (frame.method === 'chat.send') {
        const index = sends.length
        sends.push({ key: frame.params.sessionKey || frame.params.key,
          taskId: `legacy-turn-${index + 1}`, turnId: `legacy-turn-${index + 1}`,
          text: index === 0 ? FIRST_PROMPT : index === 1 ? SECOND_PROMPT : FRESH_PROMPT })
        running = true
        respond(frame.id, { accepted: true, key: sends[index]!.key, sessionKey: sends[index]!.key,
          message_id: `history-user-${index}`, task_id: sends[index]!.taskId,
          client_request_id: frame.params.clientRequestId, stream_seq: ++seq })
        emit('task.running', identity(index))
        emit('session.event.state_change', { ...identity(index), to_state: 'thinking' })
        // The second send deliberately pauses before any routing event. Its
        // real user row separates two appearances of the older turn identity.
        if (index === 1) return
        for (const model of ['anchor', 'research', 'critic']) {
          emit('session.event.ensemble_progress', { ...identity(index), event_type: 'proposer_finish',
            proposer_label: model, proposer_provider: 'synthetic', proposer_model: model })
        }
        emit('session.event.router_decision', { ...identity(index), tier: 'c1', model: 'synthetic/fusion',
          source: 'squilla_router', routing_applied: true })
        emit('session.event.ensemble_progress', { ...identity(index), event_type: 'aggregator_finish',
          proposer_label: 'aggregator', proposer_model: 'fusion' })
        // Keep the fresh task live so its own card is observable before any
        // terminal history synchronization.
        if (index === 0) complete(index)
        return
      }
      const key = frame.params?.key || frame.params?.sessionKey
      if (frame.method === 'chat.history' && key === SESSION && sends.length > 0 && !releaseHistory) {
        heldHistory.push(() => respond(frame.id, history()))
        return
      }
      const active = running && key === sends.at(-1)?.key
        ? { task_id: sends.at(-1)!.taskId, status: 'running' } : null
      const metadata = { run_status: active ? 'running' : 'idle', active_task: active, tasks: active ? [active] : [] }
      if (frame.method === 'sessions.messages.subscribe') subscriptions.push(key)
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': key === SESSION ? history() : chatHistoryPayload(),
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} }, llm_ensemble: { enabled: true }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'ensemble' },
        'sessions.routing.get': { key, mode: 'ensemble', revision: 0 },
        'sessions.list': { sessions: [{ key: SESSION, title: TITLE, sessionKind: 'chat', surface: 'webchat',
          conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 200, messageCount: 0,
          status: 'ok', runStatus: running ? 'running' : 'idle' }], count: 1, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, { ...metadata, routing: { key, mode: 'ensemble', revision: 0 } }),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key, { task_id: active?.task_id ?? null }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata),
        'usage.status': { sessions: [] },
      }
      respond(frame.id, payloads[frame.method] ?? {})
    })
  })

  const capture = async (name: string) => {
    const presentation = await readPresentation(page)
    await testInfo.attach(name, { body: JSON.stringify({ url: page.url(), presentation,
      sends, wireEvents, subscriptions, consoleMessages }, null, 2), contentType: 'application/json' })
    await page.screenshot({ path: testInfo.outputPath(`${name}.png`) })
    return presentation
  }
  try {
    await page.goto(root + `chat?session=${encodeURIComponent(SESSION)}`)
    await page.locator('.chat-textarea').fill(FIRST_PROMPT)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect(page.locator('.chat-thread')).toContainText('Synthetic completed answer 1.')
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible()
    await expect.poll(() => page.locator('.router-fx').count()).toBeGreaterThan(0)
    await capture('first-turn-complete')

    await page.locator('.chat-textarea').fill(SECOND_PROMPT)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => sends.length).toBe(2)
    expect(sends[0]!.key).toBe(sends[1]!.key)
    await expect(page.locator('.chat-thread')).toContainText(SECOND_PROMPT)
    await capture('second-turn-before-late-progress')
    // This is an accepted legacy envelope, not a fabricated current-task
    // identity: retain T1's turn_id and omit task/assistant/generation fields.
    for (const model of ['anchor', 'research', 'critic']) {
      emit('session.event.ensemble_progress', { key: sends[0]!.key, turn_id: sends[0]!.turnId,
        event_type: 'proposer_finish', proposer_label: model,
        proposer_provider: 'synthetic', proposer_model: model })
    }
    // Rendering the late frame before completion exposes keyed-list changes;
    // requestAnimationFrame observes the actual browser paint boundary.
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    await capture('late-legacy-progress')
    complete(1)
    await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible()
    await capture('before-virtual-scroll')
    await expect(page.locator('.chat-message-list[data-virtualized="true"]')).toBeVisible()
    await page.locator('.chat-thread').hover()
    // Moving both ends of the mounted window exercises Vue's keyed diff.
    // Before #1598, two prior-turn provisional rows shared a key; this update
    // could leave a router element outside the VNode list that New Task clears.
    for (let index = 0; index < 12; index += 1) {
      await page.mouse.wheel(0, -250)
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    }
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    await capture('before-new-task')

    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(page.locator('.chat--new-landing')).toBeVisible()
    const draft = await capture('new-task')
    if (inspectRawState) {
      expect(draft.raw).not.toBeNull()
      expect(draft.raw!.messages).toEqual([])
      expect(draft.raw!.sessionKey).not.toBe(sends[0]!.key)
      expect(draft.raw!.streaming).toBe(false)
    }
    await expect(page.locator('.router-fx')).toHaveCount(0)
    await expect(page.locator('.chat-textarea')).toHaveValue('')

    // Release the previous task's actual outstanding history replies, then
    // deliver more old-session events after the draft has taken ownership.
    releaseHistory = true
    heldHistory.splice(0).forEach(release => release())
    emit('session.event.ensemble_progress', { ...identity(0), event_type: 'proposer_finish',
      proposer_label: 'late', proposer_model: 'old-late' })
    emit('session.event.router_decision', { ...identity(0), tier: 'c1',
      model: 'synthetic/old-late', source: 'squilla_router', routing_applied: true })
    emit('session.event.text_delta', { ...identity(0), text: 'OLD LATE TEXT' })
    emit('session.event.thinking', { ...identity(0), text: 'OLD LATE THINKING' })
    await expect(page.locator('.chat--new-landing')).toBeVisible()
    await expect(page.locator('.router-fx')).toHaveCount(0)
    if (inspectRawState) expect((await readPresentation(page)).raw!.messages).toEqual([])

    await page.locator('.chat-textarea').fill(FRESH_PROMPT)
    await page.locator('.chat-send-btn[aria-label="Send"]').click()
    await expect.poll(() => sends.length).toBe(3)
    expect(sends[2]!.key).not.toBe(SESSION)
    await expect(page.locator('.router-fx')).toHaveCount(1)
    await page.getByRole('button', { name: TITLE, exact: true }).click()
    await expect(page.locator('.chat-thread')).toContainText('Synthetic completed answer 2.')
    await expect(page.locator('.router-fx')).toHaveCount(1)
    await expect(page.locator('.router-fx')).toContainText('3 candidates')
    expect(consoleMessages.filter(message => message.type === 'pageerror')).toEqual([])
    expect(consoleMessages.filter(message => message.text.includes('Duplicate keys'))).toEqual([])
  } finally {
    releaseHistory = true
    heldHistory.forEach(release => release())
    await testInfo.attach('console-and-wire', { body: JSON.stringify({ consoleMessages, wireEvents, sends }, null, 2), contentType: 'application/json' })
  }
})
