import { expect, type Page, type WebSocketRoute } from '@playwright/test'
import { test } from './support/native-desktop-fixture'
import { helloOkResponse } from './support/gateway-fixture'
import { chatHistoryPayload, sessionMessagesSubscribePayload, sessionMessagesSnapshotPayload, sessionMessagesHydratePayload } from './support/session-read-fixtures'

const OLD = 'agent:main:webchat:ensemble-before-draft'
const TITLE = 'Synthetic ensemble task'
test.setTimeout(180_000)

async function readChat(page: Page) {
  return page.locator('.chat').evaluate(element => {
    // Read-only diagnostics: drive all state changes through UI and RPC frames.
    const state = (element as unknown as { __vueParentComponent: { setupState: {
      sessionKey: string
      pendingSessionIntent: string | null
      isStreaming: boolean
      messages: Array<Record<string, unknown>>
    } } }).__vueParentComponent.setupState
    return {
      key: state.sessionKey,
      intent: state.pendingSessionIntent,
      streaming: state.isStreaming,
      messages: state.messages.map((message: Record<string, unknown>) => ({
        id: message.id, role: message.role, text: message.text,
        router: Boolean(message.routerDecision),
        models: (message.ensemble as { models?: Array<{ model: string }> } | undefined)
          ?.models?.map(model => model.model),
      })),
    }
  })
}

test('New Task owns the view before delayed project hydration and old events', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  const native = process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP === '1'
  page.setDefaultTimeout(10_000)
  const inspectRawState = !native && process.env.OPENSQUILLA_PLAYWRIGHT_MANAGE_WEBUI === '1'
  const root = native ? 'opensquilla-app://desktop/' : '/control/'
  let holdProjects = false
  const projects: Array<() => void> = []
  let emit: (event: string, payload?: Record<string, unknown>) => void = () => { throw new Error('not connected') }
  let seq = 0
  let sentKey = OLD
  let completed = false
  let historyAvailable = false
  const terminalHistoryReads: Array<() => void> = []
  let sendCount = 0
  let pauseCompletion = false
  let abortCount = 0
  let socket: WebSocketRoute
  let holdOldReads = false
  let heldLiveMethod = 'sessions.messages.subscribe'
  const oldReads: Array<() => void> = []
  const seenOldReads = new Set<string>()
  const subscriptions: string[] = []
  const history = () => chatHistoryPayload(completed ? [
    { role: 'user', text: 'Compare three synthetic candidates.', id: 'old-user', timestamp: 1_800_000_000 },
    { role: 'assistant', text: 'Synthetic completed answer.', id: 'old-answer', timestamp: 1_800_000_001 },
  ] : [])
  await page.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: { pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] } }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    const respond = (id: string, payload: unknown) => ws.send(JSON.stringify({ type: 'res', id, ok: true, payload }))
    emit = (event, payload = {}) => ws.send(JSON.stringify({ type: 'event', event, payload: {
      key: sentKey, task_id: `task-${sendCount}`, turn_id: `turn-${sendCount}`, stream_seq: ++seq, ...payload,
    } }))
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ auth: { principal: { isOwner: true } }, features: { methods: ['workspaces.list', 'workspaces.open'] } }))
        return
      }
      if (frame.method === 'workspaces.list') {
        const respondProjects = () => respond(frame.id, { workspaces: [] })
        if (holdProjects) projects.push(respondProjects)
        else respondProjects()
        return
      }
      if (frame.method === 'chat.send') {
        sendCount += 1
        sentKey = frame.params.sessionKey || frame.params.key
        respond(frame.id, { accepted: true, session: sentKey, task_id: `task-${sendCount}`, stream_seq: ++seq })
        emit('task.running')
        emit('session.event.state_change', { to_state: 'thinking' })
        for (const label of ['anchor', 'research', 'critic']) {
          emit('session.event.ensemble_progress', { event_type: 'proposer_finish', proposer_label: label, proposer_provider: 'synthetic', proposer_model: label })
        }
        emit('session.event.router_decision', { tier: 'c1', model: 'synthetic/fusion', source: 'squilla_router', routing_applied: true })
        emit('session.event.ensemble_progress', { event_type: 'aggregator_finish', proposer_label: 'aggregator', proposer_model: 'fusion' })
        emit('session.event.text_delta', { text: 'Synthetic completed answer.' })
        if (pauseCompletion) return
        emit('session.event.state_change', { to_state: 'completed', final_text: 'Synthetic completed answer.' })
        emit('session.event.done', { final_text: 'Synthetic completed answer.' })
        completed = true
        return
      }
      if (frame.method === 'chat.abort') {
        abortCount += 1
        respond(frame.id, { aborted: true, key: sentKey })
        emit('session.event.done', { aborted: true })
        return
      }
      const key = frame.params?.key || frame.params?.sessionKey
      if (frame.method === 'chat.history' && key === OLD && completed && !historyAvailable) {
        terminalHistoryReads.push(() => respond(frame.id, history()))
        return
      }
      const liveTask = pauseCompletion && key === sentKey && sendCount > 1
        ? { task_id: `task-${sendCount}`, status: 'running' }
        : null
      const metadata = { run_status: liveTask ? 'running' : 'idle', active_task: liveTask, tasks: liveTask ? [liveTask] : [] }
      if (frame.method === 'sessions.messages.subscribe') subscriptions.push(key)
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'chat.history': key === OLD ? history() : chatHistoryPayload(),
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'commands.list_for_surface': { commands: [] },
        'config.get': { squilla_router: { enabled: true, rollout_phase: 'full', tiers: {} }, llm_ensemble: { enabled: true }, permissions: {}, skills: {} },
        'models.routing.get': { mode: 'ensemble' },
        'sessions.routing.get': { key, mode: 'ensemble', revision: 0 },
        'sessions.list': { sessions: [{ key: OLD, title: TITLE, sessionKind: 'chat', surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: 200, messageCount: 0, status: 'ok', runStatus: 'idle' }], count: 1, ts: 1_800_000_000, has_more: false },
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, { ...metadata, routing: { key, mode: 'ensemble', revision: 0 } }),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key, {
          task_id: liveTask?.task_id ?? null,
          events: holdOldReads && key === OLD ? [{
            event: 'session.event.router_decision',
            payload: { key: OLD, task_id: 'old-snapshot-task', turn_id: 'old-snapshot-turn', stream_seq: 90,
              tier: 'c1', model: 'synthetic/late-snapshot', routing_applied: true },
          }] : [],
        }),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata),
        'usage.status': { sessions: [] },
      }
      if (holdOldReads && key === OLD && ['chat.history', heldLiveMethod].includes(frame.method)) {
        seenOldReads.add(frame.method)
        oldReads.push(() => respond(frame.id, payloads[frame.method]))
        return
      }
      respond(frame.id, payloads[frame.method] ?? {})
    })
  })
  await page.goto(root + 'chat/new?agent=main&project=uncached-project')
  await expect(page.locator('.chat--new-landing')).toBeVisible()
  await page.locator('.sidebar-history-row[data-family="chats"]').filter({ hasText: TITLE }).locator('.sidebar-history-item').click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(OLD)
  await page.locator('.chat-textarea').fill('Compare three synthetic candidates.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect(page.locator('.router-fx')).toHaveCount(1)
  await expect(page.locator('.chat-send-btn[aria-label="Send"]')).toBeVisible()
  if (inspectRawState) await expect.poll(async () => (await readChat(page)).streaming).toBe(false)
  if (inspectRawState) {
    const cards = (await readChat(page)).messages.filter(message => message.role === 'router')
    expect(cards).toHaveLength(1)
    expect(cards[0]!.models?.slice().sort()).toEqual(['anchor', 'critic', 'fusion', 'research'])
  }
  holdProjects = true
  // Browser Back is a real SPA route entry and deliberately bypasses the
  // sidebar's separate fresh-task request signal.
  await page.goBack()
  await expect.poll(() => new URL(page.url()).pathname.endsWith('/chat/new')).toBe(true)
  await expect.poll(() => projects.length).toBeGreaterThan(0)
  emit('session.event.ensemble_progress', { event_type: 'proposer_finish', proposer_label: 'late', proposer_model: 'old-late' })
  emit('session.event.router_decision', { tier: 'c1', model: 'synthetic/late', routing_applied: true })
  emit('session.event.text_delta', { text: 'OLD LATE TEXT' })
  emit('session.event.thinking', { text: 'OLD LATE THINKING' })
  if (inspectRawState) await testInfo.attach('draft-state', { body: JSON.stringify(await readChat(page)), contentType: 'application/json' })
  await page.screenshot({ path: testInfo.outputPath('draft.png') })
  try {
    await expect(page.locator('.router-fx')).toHaveCount(0, { timeout: 1000 })
    // The development renderer additionally exposes raw state. The native
    // production renderer is validated through DOM, without adding debug hooks.
    if (inspectRawState) {
      expect((await readChat(page)).messages).toEqual([])
      expect((await readChat(page)).key).not.toBe(OLD)
      expect((await readChat(page)).streaming).toBe(false)
    }
    await expect(page.locator('.chat--new-landing')).toBeVisible()
  } finally {
    holdProjects = false
    historyAvailable = true
    projects.forEach(release => release())
    terminalHistoryReads.forEach(release => release())
  }
  await expect(page.locator('.chat--new-landing')).toBeVisible()

  // Old history remains intact, including when navigation supersedes a
  // second project hydration before its reply arrives.
  await page.getByRole('button', { name: TITLE, exact: true }).click()
  await expect(page.locator('.chat-thread')).toContainText('Synthetic completed answer.')
  holdProjects = true
  projects.length = 0
  await page.goBack()
  await expect.poll(() => projects.length).toBeGreaterThan(0)
  await page.getByRole('button', { name: TITLE, exact: true }).click()
  holdProjects = false
  projects.forEach(release => release())
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(OLD)
  await expect(page.locator('.chat-thread')).toContainText('Synthetic completed answer.')
  await page.getByRole('button', { name: 'New task', exact: true }).click()
  await expect(page.locator('.chat--new-landing')).toBeVisible()
  emit('session.event.ensemble_progress', { event_type: 'proposer_finish', proposer_label: 'late' })
  await expect(page.locator('.router-fx')).toHaveCount(0)

  // Hold real request responses from the previous session, then enter a new
  // draft using the operator's button and release them out of order.
  for (const method of ['sessions.messages.subscribe', 'sessions.messages.snapshot']) {
    holdOldReads = true
    heldLiveMethod = method
    seenOldReads.clear()
    oldReads.length = 0
    await page.getByRole('button', { name: TITLE, exact: true }).click()
    await expect.poll(() => seenOldReads.has('chat.history')).toBe(true)
    await expect.poll(() => seenOldReads.has(method)).toBe(true)
    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(page.locator('.chat--new-landing')).toBeVisible()
    holdOldReads = false
    oldReads.reverse().forEach(release => release())
    emit('session.event.text_delta', { text: 'OLD LATE TEXT' })
    await expect(page.locator('.chat--new-landing')).toBeVisible()
    await expect(page.locator('.router-fx')).toHaveCount(0)
    if (inspectRawState) expect((await readChat(page)).messages).toEqual([])
  }

  // Reconnect the actual transport while on the draft; the resumed lease must
  // bind to that draft, and a current send must still produce a router card.
  const beforeReconnect = subscriptions.length
  socket!.close()
  await expect.poll(() => subscriptions.length, { timeout: 15_000 }).toBeGreaterThan(beforeReconnect)
  expect(subscriptions[subscriptions.length - 1]).not.toBe(OLD)
  await expect(page.locator('.chat--new-landing')).toBeVisible()
  await page.locator('.chat-textarea').fill('A fresh synthetic ensemble task.')
  pauseCompletion = true
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect(page.locator('.router-fx')).toHaveCount(1)
  expect(sentKey).not.toBe(OLD)
  await page.screenshot({ path: testInfo.outputPath('before-stop.png') })
  await page.getByRole('button', { name: 'Stop current response' }).click()
  await expect.poll(() => abortCount).toBe(1)
  await page.getByRole('button', { name: 'New task', exact: true }).click()
  await expect(page.locator('.chat--new-landing')).toBeVisible()
  emit('session.event.ensemble_progress', { event_type: 'proposer_finish', proposer_label: 'cancelled-late' })
  await expect(page.locator('.router-fx')).toHaveCount(0)
  pauseCompletion = false
  await page.locator('.chat-textarea').fill('Retry the synthetic task in a fresh draft.')
  await page.locator('.chat-send-btn[aria-label="Send"]').click()
  await expect(page.locator('.router-fx')).toHaveCount(1)
  await page.getByRole('button', { name: TITLE, exact: true }).click()
  await expect(page.locator('.chat-thread')).toContainText('Synthetic completed answer.')
})
