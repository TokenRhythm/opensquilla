import { expect, test, type Locator, type Page, type WebSocketRoute } from '@playwright/test'
import { helloOkResponse } from './support/gateway-fixture'
import { writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import {
  chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

// These tests enter the real routed ChatView and open the real workbench panel.
// Only the external Gateway is replaced. Neither conversation, its composables,
// nor its renderer is stubbed or manually reconstructed for a policy comparison.
const MAIN = 'agent:main:webchat:host-parity'
const SIDE = 'agent:main:sidechat:host-parity'
const NOW = Date.now()
type Wire = Record<string, unknown>
type Request = { type: string; id: string; method: string; params?: Wire }
type Stream = { sequence: number; task: string | null; lastTask: Wire | null; history: Wire[]; events: Array<{ event: string; payload: Wire }> }
const logs = new WeakMap<Page, { requests: Request[]; errors: string[]; consoleWarnings: string[] }>()

test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    const path = info.outputPath('gateway-and-render-errors.json')
    await writeFile(path, JSON.stringify(logs.get(page), null, 2))
    await info.attach('gateway-and-render-errors', { contentType: 'application/json', path })
  }
})

async function installGateway(page: Page, eventBeforeAck = false, routingConflict = false, delayCreate = false) {
  const requests: Request[] = []
  const errors: string[] = []
  const consoleWarnings: string[] = []
  let configuration: Wire = { squilla_router: { enabled: false, tiers: {} }, permissions: {}, skills: {} }
  page.on('console', message => { if (message.type() === 'warning' || message.type() === 'error') consoleWarnings.push(message.text()) })
  page.on('pageerror', error => errors.push(error.message))
  logs.set(page, { requests, errors, consoleWarnings })
  const streams = new Map<string, Stream>()
  const get = (key: string) => {
    let stream = streams.get(key)
    if (!stream) {
      stream = { sequence: 0, task: null, lastTask: null, events: [], history: key === MAIN ? [
        { role: 'user', text: 'Earlier main question', id: 'main-user', timestamp: NOW - 2_000 },
        { role: 'assistant', text: 'Earlier main answer', id: 'main-answer', timestamp: NOW - 1_000 },
      ] : [] }
      streams.set(key, stream)
    }
    return stream
  }
  let socket: WebSocketRoute
  let taskCounter = 0
  let pendingApprovals: Wire[] = []
  const approvalResolutions: Wire[] = []
  const fileRequests: Array<{ sessionKey: string; path: string; action: string }> = []
  const heldAcks: Array<() => void> = []
  let heldCreate: (() => void) | null = null
  const emit = (key: string, event: string, payload: Wire = {}) => {
    const stream = get(key)
    const envelope = { event, payload: { key, session_key: key, task_id: stream.task,
      turn_id: stream.task, assistant_message_id: `answer-${stream.task}`,
      stream_generation: `generation-${key}`, stream_seq: ++stream.sequence,
      emitted_at: NOW + stream.sequence * 100, ...payload } }
    stream.events.push(envelope)
    socket.send(JSON.stringify({ type: 'event', ...envelope }))
  }
  const metadata = (key: string) => {
    const stream = get(key)
    return { stream_generation: `generation-${key}`, current_stream_seq: stream.sequence,
      run_status: stream.task ? 'running' : 'idle', epoch: 1,
      active_task: stream.task ? { task_id: stream.task, status: 'running' } : null,
      tasks: stream.task ? [{ task_id: stream.task, status: 'running' }] : [], last_task: stream.lastTask }
  }
  const finish = (key: string, text = 'Final answer.', status = 'succeeded', tools: Wire[] = [], usageOverride: Wire = {}) => {
    const stream = get(key)
    const task = stream.task
    if (!task) throw new Error(`No accepted turn for ${key}`)
    const usage = { input_tokens: 120, output_tokens: 30, total_tokens: 150, cost_usd: 0.0015,
      model: 'synthetic/test-model', ...usageOverride }
    stream.history.push({ role: 'assistant', id: `answer-${task}`, message_id: `answer-${task}`,
      turn_id: task, timestamp: NOW + 5_000, text, tool_calls: tools,
      usage, model: usage.model })
    emit(key, 'session.event.done', { text_snapshot: text, reason: status === 'cancelled' ? 'cancelled' : 'completed',
      usage })
    emit(key, `task.${status}`, { status, terminal_reason: status === 'cancelled' ? 'user_abort' : status })
    stream.task = null
    stream.lastTask = { task_id: task, status }
    emit(key, 'sessions.changed', { reason: 'task_terminal', ...metadata(key) })
  }
  await page.addInitScript(() => {
    // Preferences belong to the app, not opaque-origin artifact iframes.
    if (window !== window.top) return
    localStorage.setItem('opensquilla-locale', 'en')
    localStorage.setItem('opensquilla-composer-floating', 'false')
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  await page.route('**/api/approvals', route => route.fulfill({ json: {
    pending: pendingApprovals, mode: 'prompt', allowPatterns: [], denyPatterns: [],
  } }))
  await page.route('**/api/approvals/resolve', async route => {
    const body = route.request().postDataJSON() as Wire
    approvalResolutions.push(body)
    pendingApprovals = pendingApprovals.filter(item => item.id !== body.id)
    await route.fulfill({ json: { id: body.id, mode: 'prompt', resolved: true, approved: body.approved,
      resolution: body.approved ? 'approved' : 'denied', deadline: null, consumed: false, pending: false } })
  })
  await page.route('**/api/v1/workspace-files/resolve', route => {
    const sessionKey = route.request().headers()['x-opensquilla-session-key'] || ''
    const paths = (route.request().postDataJSON() as { paths: string[] }).paths
    fileRequests.push(...paths.map(path => ({ sessionKey, path, action: 'resolve' })))
    return route.fulfill({ json: { workspaceBinding: 'fixture-workspace', files: paths.map(path => ({
      requestedPath: path, path, name: path.split('/').pop(), mime: path.endsWith('.html') ? 'text/html' : 'text/plain',
      size: 50, kind: 'text', revision: 'fixture-revision', textPaging: false,
    })) } })
  })
  await page.route('**/api/v1/workspace-files/content?**', route => {
    const sessionKey = route.request().headers()['x-opensquilla-session-key'] || ''
    const path = new URL(route.request().url()).searchParams.get('path') || ''
    fileRequests.push({ sessionKey, path, action: 'content' })
    return route.fulfill({ contentType: 'text/plain', body: path.endsWith('.html')
      ? '<h1>Parity HTML document</h1>' : 'export const parity = true;' })
  })
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      requests.push(frame)
      const key = String(frame.params?.key ?? frame.params?.sessionKey ?? frame.params?.session_key ?? MAIN)
      const stream = get(key)
      const respond = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({ features: { methods: [
          'sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot',
          'sessions.messages.unsubscribe', 'sessions.sidechat.create', 'sessions.sidechat.close',
          'sessions.routing.get', 'sessions.routing.set', 'chat.send', 'chat.abort',
          'sandbox.setup.status', 'sandbox.capability.status',
        ] }, auth: { runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } } }))
        return
      }
      if (frame.method === 'sessions.sidechat.create') {
        const completeCreate = () => respond({ key: SIDE, parentKey: frame.params?.parentKey, sessionId: 'side-session-id',
          inherited: { model: null, provider: null, reasoningLevel: null, modelRoutingMode: 'direct',
            modelRoutingRevision: 1, runMode: 'safe' } })
        if (delayCreate) heldCreate = completeCreate
        else completeCreate()
        return
      }
      if (frame.method === 'sessions.sidechat.close') { respond({ key, closed: true }); return }
      if (frame.method === 'sessions.routing.set' && routingConflict) {
        ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: false,
          error: { code: 'SESSION_ROUTING_CHANGED', message: 'Routing changed in another window.' } }))
        return
      }
      if (frame.method === 'chat.send') {
        stream.task = `task-${++taskCounter}`
        stream.events = []
        stream.history.push({ role: 'user', text: frame.params?.message, id: `user-${stream.task}`,
          client_message_id: frame.params?.clientMessageId, turn_id: stream.task, timestamp: NOW })
        const task = stream.task
        const ack = () => respond({ accepted: true, session: key, task_id: task })
        if (eventBeforeAck) heldAcks.push(ack)
        else ack()
        emit(key, 'task.running', { status: 'running', active_task: { task_id: task, status: 'running' } })
        return
      }
      if (frame.method === 'chat.abort') {
        respond({ aborted: true, key })
        if (stream.task) finish(key, 'Interrupted answer.', 'cancelled')
        return
      }
      if (frame.method === 'approvals.resolve') {
        pendingApprovals = pendingApprovals.filter(item => item.id !== frame.params?.id)
        respond({ ok: true })
        emit(key, 'approvals.changed', { pending: pendingApprovals })
        return
      }
      const payloads: Record<string, unknown> = {
        'agents.list': { agents: [] },
        'commands.list_for_surface': { commands: ['/new', '/plan', '/goal', '/usage', '/compact'].map(name => ({ name })) },
        'config.get': configuration,
        'models.routing.get': { mode: 'direct' },
        'models.list': { models: [] },
        'sessions.routing.get': { key, mode: 'direct', revision: 1, source: 'session', initialized: true, appliesTo: 'next_accepted_turn' },
        'sessions.routing.set': { key, mode: frame.params?.mode ?? 'direct', revision: 2, source: 'session', initialized: true, appliesTo: 'next_accepted_turn' },
        'onboarding.status': { audioConfigured: false, configured: true },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sandbox.setup.status': { state: 'ready', platform: 'linux', message: 'Synthetic sandbox ready', requiresAdmin: false },
        'sandbox.capability.status': { available: true, backend: 'bubblewrap', platform: 'linux', code: 'ready',
          reason: 'Synthetic sandbox ready', setupSupported: false, restartRequired: false, probeVersion: 1,
          capabilities: ['filesystem', 'network', 'process'] },
        'sessions.list': { sessions: [{ key: MAIN, title: 'Main parity conversation', sessionKind: 'chat',
          surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: NOW,
          messageCount: get(MAIN).history.length, status: 'ok', runStatus: get(MAIN).task ? 'running' : 'idle' }], count: 1, ts: NOW, has_more: false },
        'sessions.resolve': { key, sessionId: key === MAIN ? 'main-id' : 'side-session-id' },
        'chat.history': chatHistoryPayload(stream.history),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, metadata(key)),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata(key)),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key, { ...metadata(key), task_id: stream.task,
          events: stream.task ? stream.events.filter(event => event.event.startsWith('session.event.')) : [] }),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
      }
      respond(payloads[frame.method] ?? {})
    })
  })
  return { requests, errors, emit, finish, approvalResolutions, fileRequests,
    appendHistory: (key: string, message: Wire) => get(key).history.push({
      turn_id: get(key).task, timestamp: NOW + 2_000, ...message,
    }),
    changeRouterConfig: (tiers: Wire) => {
      configuration = { ...configuration, squilla_router: { enabled: true, tiers } }
      const imageInput = { admission: 'allowed', reason: 'Configured image-capable route' }
      socket.send(JSON.stringify({ type: 'event', event: 'models.routing.changed', payload: {
        mode: 'router', router_enabled: true, ensemble_enabled: false, rollout_phase: 'live',
        selection_mode: 'tiers', selection_configured: true, router_required_by_ensemble: false,
        activation_preview: { selection_mode: 'tiers', selection_configured: true,
          proposer_count: 0, member_providers: [], candidates: [], blocked_reason: null },
        image_input: imageInput, applies_to: 'next_accepted_turn', source: 'config',
        capabilities_by_mode: { direct: { image_input: imageInput }, router: { image_input: imageInput }, ensemble: { image_input: imageInput } },
      } }))
    },
    releaseCreate: () => { heldCreate?.(); heldCreate = null },
    releaseAcks: () => heldAcks.splice(0).forEach(release => release()),
    pendingApprovals: (items: Wire[]) => { pendingApprovals = items },
    reconnect: () => socket.close({ code: 1012, reason: 'Synthetic reconnect' }) }
}

async function openHosts(page: Page) {
  await page.goto(`/control/chat?session=${encodeURIComponent(MAIN)}`)
  const main = page.locator('#app-main .chat').first()
  await expect(main).toContainText('Earlier main answer')
  await page.getByTestId('topbar-workbench-toggle').click()
  await page.getByRole('button', { name: 'Assistant chat', exact: true }).click()
  const side = page.getByTestId('assistant-chat-panel')
  await expect(composer(side)).toBeVisible()
  for (const host of [main, side]) await expect(composer(host)).toHaveAttribute('contenteditable', 'true')
  return { main, side }
}

function composer(host: Locator) {
  return host.getByRole('textbox', { name: 'Message to send', exact: true })
}

async function send(host: Locator, text: string) {
  await composer(host).fill(text)
  await host.getByRole('button', { name: 'Send', exact: true }).click()
}

async function assertNoSettledExtras(host: Locator) {
  await expect(host.locator('.sources-row')).toHaveCount(0)
  await expect(host.locator('.conversation-live-edge .msg-ai-footer')).toHaveCount(0)
  await expect(host.locator('.chat-stop-btn')).toBeVisible()
}

test.describe('real routed main and workbench side conversation hosts', () => {
  test('preserves each accepted router card when an earlier call receipt settles after a later route', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    const firstRoute = { tier: 'c1', model: 'shared-model', provider: 'profile-a', source: 'classifier',
      accepted_routing_mode: 'router', routing_applied: true,
      router_tier_snapshot: { version: 1, request_kind: 'image', tiers: [
        { tier: 'c0', model: 'shared-model', provider: 'profile-z', execution_kind: 'single_model' },
        { tier: 'c1', model: 'shared-model', provider: 'profile-a', execution_kind: 'single_model' },
      ] } }
    const laterRoute = { tier: 'c1', model: 'later/alpha', source: 'classifier', accepted_routing_mode: 'router',
      router_tier_snapshot: { version: 1, request_kind: 'text', tiers: [
        { tier: 'c0', model: 'later/zeta', execution_kind: 'single_model' },
        { tier: 'c1', model: 'later/alpha', execution_kind: 'single_model' },
      ] } }
    for (const host of [main, side]) {
      await host.getByRole('button', { name: 'Models & routing', exact: true }).click()
      await page.getByRole('menuitemradio', { name: /Intelligent model routing/ }).click()
      await send(host, 'Compare the accepted routes')
    }
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.router_decision', { ...firstRoute, model_call_id: '1.0', iteration: 1 })
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking the first route.\n\n', model_call_id: '1.0', iteration: 1, presentation: 'intermediate' })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.router-fx')).toHaveCount(1)
      await expect(host.locator('.router-fx .nm-base')).toHaveText(['shared-model · profile-z', 'shared-model · profile-a'])
    }
    const tool = { type: 'tool_use', tool_use_id: 'route-tool', name: 'web_fetch',
      input: { url: 'https://example.com/routes' },
      result: JSON.stringify({ url: 'https://example.com/routes', title: 'Route evidence', content: 'Evidence retained' }),
      execution_status: { status: 'success' } }
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.router_control_replay')
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking the later route.\n\n', model_call_id: '2.0', iteration: 2, presentation: 'intermediate' })
      gateway.emit(key, 'session.event.router_decision', { ...laterRoute, model_call_id: '2.0', iteration: 2 })
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: tool.tool_use_id, tool_name: tool.name, input: tool.input })
      gateway.emit(key, 'session.event.tool_result', { ...tool, tool_name: tool.name })
      gateway.emit(key, 'session.event.text_delta', { text: 'Both accepted routes retained.', model_call_id: '2.0', iteration: 2, presentation: 'answer' })
      for (const [index, route] of [firstRoute, laterRoute].entries()) gateway.appendHistory(key, {
        role: 'router', id: `router-${key}-${index}`, message_id: `router-${key}-${index}`, text: '',
        provenance_kind: 'router_decision', router_decision: route,
        usage: { router_model_call_id: `${index + 1}.0`, router_iteration: index + 1 },
      })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.router-fx')).toHaveCount(2)
      await expect(host.locator('.conversation-live-edge .tool-row')).toHaveCount(1)
      await expect(host.locator('.conversation-live-edge')).toContainText('Both accepted routes retained.')
    }
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'Both accepted routes retained.', 'succeeded', [tool], {
      // A late call-1 receipt must reconcile with its own accepted snapshot,
      // while the assistant body, tools and billing remain ordinary content.
      router_model_call_id: '1.0', model: 'shared-model', input_tokens: 321, output_tokens: 45,
      total_tokens: 366, cost_usd: 0.009,
      route_plan: { ...firstRoute, router_tier_snapshot: null },
    })
    const checkCards = async (host: Locator) => {
      await expect(host.locator('.router-fx')).toHaveCount(2)
      await expect(host.locator('.router-fx').nth(0).locator('.nm-base')).toHaveText(['shared-model · profile-z', 'shared-model · profile-a'])
      await expect(host.locator('.router-fx').nth(0).locator('.win .nm-base')).toHaveText('shared-model · profile-a')
      await expect(host.locator('.router-fx').nth(1).locator('.nm-base')).toHaveText(['zeta', 'alpha'])
      await expect(host.locator('.assistant-answer').last()).toHaveText('Both accepted routes retained.')
      await expect(host.locator('.sources-row')).toHaveCount(1)
    }
    for (const host of [main, side]) {
      await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
      await checkCards(host)
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
      await expect(host.locator('.msg-meta-popover').last()).toContainText('shared-model')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('321')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('45')
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
    }
    const configReads = gateway.requests.filter(request => request.method === 'config.get').length
    gateway.changeRouterConfig({ c0: { model: 'current/replacement-z', image_only: false },
      c1: { model: 'current/replacement-a', image_only: false } })
    await expect.poll(() => gateway.requests.filter(request => request.method === 'config.get').length).toBeGreaterThan(configReads)
    for (const host of [main, side]) await checkCards(host)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('keeps simultaneous main and side slash palettes and their editor ARIA references independent', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await composer(main).fill('/')
    await expect(main.locator('.chat-slash')).toBeVisible()
    await composer(side).fill('/')
    const paletteIds: string[] = []
    for (const host of [main, side]) {
      const editor = composer(host)
      await expect(editor).toHaveAttribute('aria-expanded', 'true')
      const paletteId = await editor.getAttribute('aria-controls')
      const activeOptionId = await editor.getAttribute('aria-activedescendant')
      expect(paletteId).toBeTruthy()
      expect(activeOptionId).toBeTruthy()
      paletteIds.push(paletteId!)
      const palette = page.locator(`[id="${paletteId}"]`)
      await expect(palette).toHaveCount(1)
      await expect(palette).toHaveAttribute('role', 'listbox')
      await expect(palette).toBeVisible()
      await expect(page.locator(`[id="${activeOptionId}"]`)).toHaveCount(1)
      await expect(palette.locator(`[id="${activeOptionId}"]`)).toHaveAttribute('role', 'option')
    }
    expect(new Set(paletteIds).size).toBe(2)
    await composer(side).press('Escape')
    await expect(composer(side)).toHaveAttribute('aria-expanded', 'false')
    await expect(main.locator('.chat-slash')).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    expect(gateway.errors).toEqual([])
  })

  test('carries immutable visualize receipts through both real live edges into interactive canvases', async ({ page }) => {
    const gateway = await installGateway(page)
    const documents = [MAIN, SIDE].map((key, index) => {
      const label = index === 0 ? 'Main live canvas' : 'Side live canvas'
      const html = `<!doctype html><html><body><h1>${label}</h1><button onclick="this.textContent='Clicked'">Try canvas</button></body></html>`
      const documentId = `doc_host_${index}`
      const revisionId = 'rev_live'
      const receipt = { documentId, revisionId, artifactId: documentId, widgetId: `host_${index}`,
        version: 1, name: `${label}.html`, mime: 'text/html', size: Buffer.byteLength(html),
        sha256: createHash('sha256').update(html).digest('hex'), marker: `visualize:${documentId}:${revisionId}` }
      return { key, label, html, receipt, callId: `widget-${index}` }
    })
    const reads: Array<{ documentId: string; revisionId: string | null; sessionKey: string }> = []
    await page.route('**/api/v1/artifact-documents/*?**', route => {
      const url = new URL(route.request().url())
      const documentId = url.pathname.split('/').pop()!
      const document = documents.find(item => item.receipt.documentId === documentId)
      if (!document) return route.fulfill({ status: 404 })
      reads.push({ documentId, revisionId: url.searchParams.get('revisionId'),
        sessionKey: route.request().headers()['x-opensquilla-session-key'] || '' })
      return route.fulfill({ contentType: 'text/html', body: document.html })
    })
    const { main, side } = await openHosts(page)
    await send(main, 'Show an interactive canvas')
    await send(side, 'Show an interactive canvas')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const document of documents) {
      gateway.emit(document.key, 'session.event.tool_use_start', {
        tool_use_id: document.callId, tool_name: 'show_widget', input: { title: document.label },
      })
      gateway.emit(document.key, 'session.event.tool_result', {
        tool_use_id: document.callId, tool_name: 'show_widget', name: 'show_widget',
        result: JSON.stringify(document.receipt), execution_status: { status: 'success' },
      })
      gateway.emit(document.key, 'session.event.text_delta', {
        text: `Before canvas.\n\n\`${document.receipt.marker}\`\n\nAfter canvas.\n\n`, presentation: 'answer',
      })
    }
    for (const [index, host] of [main, side].entries()) {
      const document = documents[index]!
      const live = host.locator('.conversation-live-edge')
      await expect(host.locator('.chat-stop-btn')).toBeVisible()
      await expect(live.locator('[data-visualize-canvas]')).toHaveCount(1)
      await expect(live).not.toContainText(document.receipt.marker)
      const frame = live.frameLocator('[data-visualize-canvas] iframe')
      await expect(frame.getByRole('heading', { name: document.label, exact: true })).toBeVisible()
      await frame.getByRole('button', { name: 'Try canvas', exact: true }).click()
      await expect(frame.getByRole('button', { name: 'Clicked', exact: true })).toBeVisible()
      expect(reads).toContainEqual({ documentId: document.receipt.documentId,
        revisionId: document.receipt.revisionId, sessionKey: document.key })
    }
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('keeps an unsent side draft across panel changes and filters only policy actions', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await side.getByRole('button', { name: 'Execution mode: Full Access', exact: true }).click()
    await page.getByRole('radio', { name: /^Safe / }).click()
    await composer(side).fill('An unsent side draft')
    await send(main, 'Open a code file')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.finish(MAIN, '[Code](draft.ts)')
    await main.getByRole('link', { name: 'Open draft.ts', exact: true }).click()
    await expect(side).not.toBeVisible()
    await page.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
    await expect(composer(side)).toHaveText('An unsent side draft')
    await expect(side.getByRole('button', { name: 'Execution mode: Safe', exact: true })).toBeVisible()
    await expect(main.getByRole('button', { name: 'Execution mode: Full Access', exact: true })).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    await composer(main).fill('/')
    for (const name of ['/plan', '/goal']) await expect(page.getByRole('option', { name: new RegExp(`^${name} `) })).toBeVisible()
    await page.keyboard.press('Escape')
    await composer(side).fill('/')
    await expect(side.locator('.chat-slash')).toBeVisible()
    for (const name of ['/fork', '/plan', '/goal', '/new']) await expect(page.getByRole('option', { name: new RegExp(`^${name} `) })).toHaveCount(0)
    await composer(side).fill('An unsent side draft')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('restores the same side child and draft after New task and return to the parent route', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(side, 'Side question before navigation')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    gateway.finish(SIDE, 'Retained side answer')
    await expect(side).toContainText('Retained side answer')
    await composer(side).fill('Retained side draft')
    await page.getByRole('button', { name: 'New task', exact: true }).click()
    await expect(page).toHaveURL(/\/chat\/new/)
    await expect(side).not.toBeVisible()
    await page.getByRole('button', { name: 'Main parity conversation', exact: true }).click()
    await expect(main).toContainText('Earlier main answer')
    await expect(side).toContainText('Retained side answer')
    await expect(composer(side)).toHaveText('Retained side draft')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.close')).toHaveLength(0)
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('applies a side routing choice before its first turn without changing the parent route setting', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await side.getByRole('button', { name: 'Models & routing', exact: true }).click()
    await page.getByRole('menuitemradio', { name: /Intelligent model routing/ }).click()
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    await send(side, 'Use the selected routing mode')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    expect(gateway.requests.filter(request => request.method === 'sessions.routing.set')).toMatchObject([
      { params: { sessionKey: SIDE, mode: 'router', expectedRevision: 1 } },
    ])
    await expect(main.getByRole('button', { name: 'Models & routing', exact: true })).toContainText('Fixed model')
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    expect(gateway.errors).toEqual([])
  })

  test('keeps the side draft and does not dispatch when its first routing update conflicts', async ({ page }) => {
    const gateway = await installGateway(page, false, true)
    const { main, side } = await openHosts(page)
    await side.getByRole('button', { name: 'Models & routing', exact: true }).click()
    await page.getByRole('menuitemradio', { name: /Intelligent model routing/ }).click()
    await send(side, 'Keep this draft after a routing conflict')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.routing.set').length).toBe(1)
    await expect(composer(side)).toHaveText('Keep this draft after a routing conflict')
    await expect(page.getByText('Routing changed in another window.', { exact: true })).toBeVisible()
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    await expect(main.getByRole('button', { name: 'Models & routing', exact: true })).toContainText('Fixed model')
    expect(gateway.errors).toEqual([])
  })

  test('freezes only the preparing side settings until its child is ready', async ({ page }) => {
    const gateway = await installGateway(page, false, false, true)
    const { main, side } = await openHosts(page)
    await send(side, 'Wait for child preparation')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.create').length).toBe(1)
    await expect(side.getByRole('button', { name: 'Models & routing', exact: true })).toBeDisabled()
    await expect(side.getByRole('button', { name: /^Execution mode:/ })).toBeDisabled()
    await expect(main.getByRole('button', { name: 'Models & routing', exact: true })).toBeEnabled()
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(0)
    gateway.releaseCreate()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'chat.send')?.params?._source).toMatchObject({ runMode: 'safe' })
    gateway.finish(SIDE)
    await expect(side.getByRole('button', { name: 'Models & routing', exact: true })).toBeEnabled()
    expect(gateway.errors).toEqual([])
  })

  test('shares the complete live-to-settled web-tool rendering path and isolates session ownership', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    for (const frame of [main, side.locator('.chat')]) {
      await expect.poll(() => frame.evaluate(element => {
        const style = getComputedStyle(element)
        return { border: style.borderWidth, radius: style.borderRadius, shadow: style.boxShadow }
      })).toEqual({ border: '0px', radius: '0px', shadow: 'none' })
    }
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(0)
    const initialUrl = page.url()
    const initialHeader = await page.locator('.topbar').textContent()
    const initialStoredSession = await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))
    await send(main, 'Search and compare')
    await expect(main.locator('.chat-stop-btn')).toBeVisible()
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await send(side, 'Search and compare')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    expect(gateway.requests.filter(request => request.method === 'chat.send').map(request => request.params?.sessionKey ?? request.params?.session_key ?? request.params?.key)).toEqual([MAIN, SIDE])
    expect(gateway.requests.filter(request => request.method === 'chat.send')[1]?.params?._source).toMatchObject({ runMode: 'safe' })
    expect(page.url()).toBe(initialUrl)
    expect(await page.locator('.topbar').textContent()).toBe(initialHeader)
    expect(await page.evaluate(() => localStorage.getItem('opensquilla_active_session'))).toBe(initialStoredSession)
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.text_delta', { text: 'Checking sources.', presentation: 'intermediate' })
      gateway.emit(key, 'session.event.tool_use_start', { tool_use_id: 'read-web', tool_name: 'web_fetch', input: { url: 'https://example.com/news' } })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Checking sources.')
      await expect(host.locator('.conversation-live-edge .tool-row')).toHaveCount(1)
      await assertNoSettledExtras(host)
    }
    const tool = { type: 'tool_use', tool_use_id: 'read-web', name: 'web_fetch', input: { url: 'https://example.com/news' },
      result: JSON.stringify({ url: 'https://example.com/news', title: 'Synthetic news', content: 'News body' }), execution_status: { status: 'success' } }
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.tool_result', { ...tool, tool_name: tool.name })
      gateway.emit(key, 'session.event.text_delta', { text: 'Final answer.', presentation: 'answer' })
    }
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Final answer.')
      await assertNoSettledExtras(host)
    }
    for (const key of [MAIN, SIDE]) gateway.finish(key, 'Final answer.', 'succeeded', [
      { type: 'text', text: 'Checking sources.' }, tool, { type: 'text', text: 'Final answer.' },
    ])
    for (const host of [main, side]) {
      await expect(host.locator('.chat-stop-btn')).toHaveCount(0)
      await expect(host.locator('.conversation-live-edge')).toHaveCount(0)
      await expect(host.locator('.sources-row')).toHaveCount(1)
      await expect(host.locator('.assistant-answer').last()).toHaveText('Final answer.')
      await expect(host.locator('.msg-ai-footer').last()).toBeVisible()
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
      await expect(host.locator('.msg-meta-popover').last()).toContainText('test-model')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('120')
      await expect(host.locator('.msg-meta-popover').last()).toContainText('30')
      await host.getByRole('button', { name: 'Usage details', exact: true }).last().click()
      await host.getByRole('button', { name: 'Models & routing', exact: true }).click()
      await expect(page.getByRole('menuitemradio', { name: /Intelligent model routing/ })).toBeVisible()
      await page.keyboard.press('Escape')
    }
    expect(await main.locator('.sources-row').textContent()).toBe(await side.locator('.sources-row').textContent())
    for (const name of ['Fork from here', 'Edit', 'Regenerate']) {
      await expect(main.getByRole('button', { name, exact: true }).first()).toBeAttached()
      await expect(side.getByRole('button', { name, exact: true })).toHaveCount(0)
    }
    await side.getByRole('button', { name: 'Add', exact: true }).click()
    await expect(page.getByRole('menu')).toBeVisible()
    await expect(page.getByRole('menuitem', { name: /fork|plan mode|goal mode/i })).toHaveCount(0)
    await page.keyboard.press('Escape')
    expect(page.url()).toBe(initialUrl)
    expect(gateway.errors).toEqual([])
  })

  test('preserves both live readers on reconnect and closes only the side owner', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(main, 'Main recovery turn')
    await send(side, 'Side recovery turn')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) gateway.emit(key, 'session.event.text_delta', { text: 'Retained across reconnect.' })
    for (const host of [main, side]) await expect(host.locator('.conversation-live-edge')).toContainText('Retained across reconnect.')
    const readsBefore = gateway.requests.filter(request => request.method === 'sessions.messages.subscribe').length
    gateway.reconnect()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.messages.subscribe').length,
      { timeout: 15_000 }).toBeGreaterThan(readsBefore)
    for (const host of [main, side]) {
      await expect(host.locator('.conversation-live-edge')).toContainText('Retained across reconnect.')
      await expect(host.locator('.chat-stop-btn')).toBeVisible()
      await expect(host.locator('.chat-stop-btn')).toBeEnabled()
      await expect(host.locator('.conversation-live-edge')).not.toContainText('Retained across reconnect.Retained across reconnect.')
    }
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side).not.toBeVisible()
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(side.locator('.conversation-live-edge')).toContainText('Retained across reconnect.')
    expect(gateway.requests.filter(request => request.method === 'sessions.sidechat.create')).toHaveLength(1)
    await side.locator('.chat-stop-btn').click()
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await page.getByRole('button', { name: 'Close tab: Assistant chat', exact: true }).click()
    await expect(side).toHaveCount(0)
    await expect.poll(() => gateway.requests.filter(request => request.method === 'sessions.sidechat.close').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'sessions.sidechat.close')?.params?.key).toBe(SIDE)
    await expect(main.locator('.chat-stop-btn')).toBeVisible()
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    gateway.finish(MAIN)
    await expect(main.locator('.chat-stop-btn')).toHaveCount(0)
    expect(gateway.errors).toEqual([])
  })

  test('uses the same approval UI with session-isolated resolution', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    await send(main, 'Main approval')
    await send(side, 'Side approval')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    const approvals = [MAIN, SIDE].map((key, index) => ({ id: `approval-${index}`, namespace: 'exec',
      sessionKey: key, toolName: 'shell', command: 'echo approval-check',
      args: { command: 'echo approval-check' }, displayKind: 'run_command', displayTarget: 'echo approval-check' }))
    gateway.pendingApprovals(approvals)
    for (const approval of approvals) gateway.emit(approval.sessionKey, 'exec.approval.requested', {
      ...approval, approval_id: approval.id, tool_name: approval.toolName,
    })
    for (const host of [main, side]) {
      await expect(host.getByTestId('approval-card')).toHaveCount(1)
      await expect(host.getByTestId('approval-card')).toContainText('echo approval-check')
      await expect(host.getByRole('button', { name: 'Allow once', exact: true })).toBeVisible()
    }
    expect(await main.getByTestId('approval-card').textContent()).toBe(await side.getByTestId('approval-card').textContent())
    await side.getByRole('button', { name: 'Allow once', exact: true }).click()
    await expect.poll(() => gateway.approvalResolutions.length).toBe(1)
    expect(gateway.approvalResolutions[0]).toMatchObject({ id: 'approval-1', approved: true })
    await expect(side.getByTestId('approval-card')).toHaveCount(0)
    await expect(side.getByRole('button', { name: 'Allow once', exact: true })).toHaveCount(0)
    await expect(main.getByTestId('approval-card')).toHaveCount(1)
    expect(gateway.requests.filter(request => request.method === 'chat.send')).toHaveLength(2)
    expect(gateway.errors).toEqual([])
  })

  test('opens HTML and code links from both actual transcripts under their own read authority', async ({ page }) => {
    const gateway = await installGateway(page)
    const { main, side } = await openHosts(page)
    const text = '[HTML document](demo.html), [Code file](demo.ts) and [Markdown](report.md)'
    await send(main, 'Show files')
    await send(side, 'Show files')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) {
      gateway.emit(key, 'session.event.text_delta', { text })
      gateway.finish(key, text)
    }
    for (const [host, key] of [[main, MAIN], [side, SIDE]] as const) {
      for (const [path, text] of [['demo.html', 'Parity HTML document'], ['demo.ts', 'export const parity'], ['report.md', 'export const parity']] as const) {
        await page.getByRole('tab', { name: 'Assistant chat', exact: true }).click()
        const link = host.getByRole('link', { name: `Open ${path}`, exact: true })
        await expect(link).toBeVisible()
        await link.click()
        await expect(page.getByTestId('workspace-file-panel')).toContainText(text)
        expect(gateway.fileRequests.some(request => request.sessionKey === key && request.path === path && request.action === 'content')).toBe(true)
        expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
        await expect(page.getByRole('tab', { name: 'Assistant chat', exact: true })).toBeVisible()
      }
    }
    expect(gateway.errors).toEqual([])
  })

  test('accepts events before send ACK and cancelling side leaves the main execution running', async ({ page }) => {
    const gateway = await installGateway(page, true)
    const { main, side } = await openHosts(page)
    await send(main, 'Main keeps running')
    await send(side, 'Side can stop')
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.send').length).toBe(2)
    for (const key of [MAIN, SIDE]) gateway.emit(key, 'session.event.text_delta', { text: 'Working before acknowledgement.' })
    gateway.releaseAcks()
    for (const host of [main, side]) await expect(host.locator('.conversation-live-edge')).toContainText('Working before acknowledgement.')
    await side.locator('.chat-stop-btn').click()
    await expect.poll(() => gateway.requests.filter(request => request.method === 'chat.abort').length).toBe(1)
    expect(gateway.requests.find(request => request.method === 'chat.abort')?.params?.sessionKey).toBe(SIDE)
    await expect(side.locator('.chat-stop-btn')).toHaveCount(0)
    await expect(main.locator('.chat-stop-btn')).toBeVisible()
    await expect(main.locator('.conversation-live-edge')).toContainText('Working before acknowledgement.')
    expect(new URL(page.url()).searchParams.get('session')).toBe(MAIN)
    gateway.finish(MAIN)
    await expect(main.locator('.chat-stop-btn')).toHaveCount(0)
    expect(gateway.errors).toEqual([])
  })
})
