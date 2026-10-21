import { type Page, type WebSocketRoute } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { helloOkResponse } from './gateway-fixture'
import { chatHistoryPayload, sessionMessagesHydratePayload, sessionMessagesSnapshotPayload, sessionMessagesSubscribePayload } from './session-read-fixtures'

export const MAIN = 'agent:main:webchat:host-parity'
export const SIDE = 'agent:main:sidechat:host-parity'
export const OTHER = 'agent:main:webchat:other-parity'
export const NOW = Date.now()
type Wire = Record<string, unknown>
type Request = { type: string; id: string; method: string; params?: Wire; nonce?: string }
type Stream = { sequence: number; task: string | null; lastTask: Wire | null; history: Wire[]; events: Array<{ event: string; payload: Wire }> }
export const logs = new WeakMap<Page, { requests: Request[]; errors: string[]; consoleWarnings: string[] }>()

export async function installGateway(page: Page, eventBeforeAck = false, routingConflict = false, delayCreate = false, modernSnapshots = false,
  options: { workspace?: boolean; terminal?: boolean; locale?: string; modelName?: string; draftModelName?: string; audioConfigured?: boolean } = {}) {
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
  let connection = 0
  const snapshotInstalls: Array<{ key: string; connection: number; id: string }> = []
  let taskCounter = 0
  let pendingApprovals: Wire[] = []
  const approvalResolutions: Wire[] = []
  const failedApprovals = new Set<string>()
  const pendingInputs = new Map<string, Wire[]>()
  const sessionRouting = new Map<string, Wire>()
  const routingSnapshot = (key: string) => sessionRouting.get(key) ?? {
    key, mode: 'direct', revision: 1, source: 'session', initialized: true, appliesTo: 'next_accepted_turn',
    ...(options.modelName ? { modelSelection: { model: options.modelName, provider: 'synthetic' } } : {}),
  }
  const heldClarifications = new Map<string, { key: string; reply: (accepted: boolean) => void }>()
  const deniedFiles = new Set<string>()
  const binaryReferences = new Set<string>()
  const documents = new Map<string, { resource: Wire; document: Wire; revision: Wire; artifact: Wire; revisions: Wire[] }>()
  const deliveries = new Map<string, { resource: Wire; artifact: Wire }>()
  let resourceCatalogueEnabled = false
  const workspaceDocuments = new Map<string, string>()
  const deferredResources = new Set<string>()
  const heldResourceResolves = new Map<string, () => void>()
  let includeOtherSession = false
  const fileRequests: Array<{ sessionKey: string; path: string; action: string }> = []
  const heldAcks: Array<() => void> = []
  let heldCreate: (() => void) | null = null
  let failNextCreate = false
  const rejectedNextSends = new Set<string>()
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
      ...(options.workspace ? { workspaceId: 'fixture-workspace', projectWorkspace: {
        id: 'fixture-workspace', name: 'Layout workspace', path: 'C:/fixtures/layout', available: true, removed: false,
      } } : {}),
      run_status: stream.task ? 'running' : 'idle', epoch: 1,
      active_task: stream.task ? { task_id: stream.task, status: 'running' } : null,
      tasks: stream.task ? [{ task_id: stream.task, status: 'running' }] : [], last_task: stream.lastTask,
      pendingUserInputs: pendingInputs.get(key) ?? [] }
  }
  const finish = (key: string, text = 'Final answer.', status = 'succeeded', tools: Wire[] = [], usageOverride: Wire = {}, artifacts: Wire[] = []) => {
    const stream = get(key)
    const task = stream.task
    if (!task) throw new Error(`No accepted turn for ${key}`)
    const usage = { input_tokens: 120, output_tokens: 30, total_tokens: 150, cost_usd: 0.0015,
      model: 'synthetic/test-model', ...usageOverride }
    stream.history.push({ role: 'assistant', id: `answer-${task}`, message_id: `answer-${task}`,
      turn_id: task, timestamp: NOW + 5_000, text, tool_calls: tools,
      usage, model: usage.model, artifacts })
    emit(key, 'session.event.done', { text_snapshot: text, reason: status === 'cancelled' ? 'cancelled' : 'completed',
      usage })
    emit(key, `task.${status}`, { status, terminal_reason: status === 'cancelled' ? 'user_abort' : status })
    stream.task = null
    stream.lastTask = { task_id: task, status }
    emit(key, 'sessions.changed', { reason: 'task_terminal', ...metadata(key) })
  }
  await page.addInitScript(locale => {
    // Preferences belong to the app, not opaque-origin artifact iframes.
    if (window !== window.top) return
    localStorage.setItem('opensquilla-locale', locale)
    localStorage.setItem('opensquilla-composer-floating', 'false')
  }, options.locale ?? 'en')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/api/**', route => route.fulfill({ json: {} }))
  // Production asks Gateway for this static URL. Vite preview otherwise
  // proxies it to an absent default Gateway; serve the actual built bytes.
  await page.route('**/control/static/dist/opensquilla-mark.png', route => route.fulfill({
    contentType: 'image/png', path: fileURLToPath(new URL('../../dist/opensquilla-mark.png', import.meta.url)),
  }))
  await page.route('**/api/approvals', route => route.fulfill({ json: {
    pending: pendingApprovals, mode: 'prompt', allowPatterns: [], denyPatterns: [],
  } }))
  await page.route('**/api/approvals/resolve', async route => {
    const body = route.request().postDataJSON() as Wire
    approvalResolutions.push(body)
    if (failedApprovals.has(String(body.id))) {
      await route.fulfill({ status: 503, json: { error: 'Approval service temporarily unavailable' } })
      return
    }
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
  if (options.workspace) {
    await page.route('**/api/v1/workspace-files/list?**', route => route.fulfill({ json: {
      workspaceBinding: 'fixture-workspace', relativePath: '', nextCursor: null, hasMore: false, truncated: false,
      entries: ['demo.html', 'demo.ts', 'report.md'].map(name => ({ name, path: name, type: 'file', kind: 'text',
        mime: name.endsWith('.html') ? 'text/html' : 'text/plain', size: 100, mtimeNs: '1234567890' })),
    } }))
    await page.route('**/api/v1/workspace-files/root', route => route.fulfill({ json: {
      kind: 'workspace-root', name: 'Layout workspace', path: 'C:/fixtures/layout', workspaceBinding: 'a'.repeat(64),
    } }))
    await page.route('**/api/v1/workspace-files/apps', route => route.fulfill({ json: {
      host: 'gateway', available: true, apps: [{ id: 'file-manager', label: 'File manager' }],
    } }))
  }
  await page.route('**/api/v1/workspace-files/content?**', route => {
    const sessionKey = route.request().headers()['x-opensquilla-session-key'] || ''
    const path = new URL(route.request().url()).searchParams.get('path') || ''
    fileRequests.push({ sessionKey, path, action: 'content' })
    if (deniedFiles.has(`${sessionKey}:${path}`)) return route.fulfill({ status: 403, json: { error: 'Child resource access denied' } })
    const authority = sessionKey === SIDE ? 'Side resource authority' : 'Main resource authority'
    return route.fulfill({ contentType: 'text/plain', body: path.endsWith('.html')
      ? `<h1>Parity HTML document</h1><p>${authority}</p><button onclick="this.textContent = 'Clicked once'">Try interaction</button>`
      : `export const parity = true;\n// ${authority}` })
  })
  await page.route('**/api/v1/artifact-documents/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Canonical child preview</h1>' }))
  await page.route('**/api/v1/artifacts/*/preview-leases', route => {
    const artifactId = new URL(route.request().url()).pathname.split('/').at(-2)!
    return route.fulfill({ status: 201, json: { version: 1, lease_id: `lease-${artifactId}`, effective_mode: 'offline',
      launch_url: `/api/v1/artifact-preview/${artifactId}/index.html`, entrypoint: 'index.html',
      expires_at: new Date(Date.now() + 3_600_000).toISOString(), preview_origin: null, idle_timeout_seconds: 3600,
      source: { kind: 'single_file', collection_status: 'not_applicable', file_count: 1, total_bytes: 32, warning_codes: [] } } })
  })
  await page.route('**/api/v1/artifact-preview/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Canonical child preview</h1>' }))
  await page.route('**/api/v1/artifact-preview-leases/**', route => route.fulfill({ status: 204 }))
  await page.routeWebSocket(/\/ws$/, ws => {
    socket = ws
    const connectionId = ++connection
    const deliveryEpoch = `host-parity-delivery-${connectionId}`
    let nextDelivery = 0
    let acknowledged = 0
    const snapshots = new Map<string, { id: string; key: string; bytes: Buffer; sequence: number }>()
    ws.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    ws.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as Request
      if (frame.type === 'ping') { ws.send(JSON.stringify({ type: 'pong', nonce: frame.nonce })); return }
      if (frame.type !== 'req') return
      requests.push(frame)
      const key = String(frame.params?.key ?? frame.params?.sessionKey ?? frame.params?.session_key ?? MAIN)
      const stream = get(key)
      const respond = (payload: unknown) => ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: true, payload }))
      if (frame.method === 'connect') {
        ws.send(helloOkResponse({
          ...(modernSnapshots ? { server: { conn_id: `host-parity-${connectionId}` }, policy: {
            transport_probe_nonce: true,
            transport_flow: { delivery_epoch: deliveryEpoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 },
            ...(options.modelName || options.draftModelName ? { sessions_routing_model_selection: true } : {}),
            ...(options.draftModelName ? { chat_send_initial_model: true } : {}),
          } } : {}),
          ...(!modernSnapshots && (options.modelName || options.draftModelName) ? { policy: {
            sessions_routing_model_selection: true,
            ...(options.draftModelName ? { chat_send_initial_model: true } : {}),
          } } : {}),
          features: { methods: [
          'sessions.messages.subscribe', 'sessions.messages.hydrate', 'sessions.messages.snapshot',
          'sessions.messages.unsubscribe', 'sessions.sidechat.create', 'sessions.sidechat.close',
          'sessions.routing.get', 'sessions.routing.set', 'chat.send', 'chat.abort', 'chat.clarify_submit',
          'artifacts.get', 'workbench.resources.list', 'workbench.resources.get', 'workbench.resources.open',
          'artifacts.documents.get', 'artifacts.revisions.list', 'artifacts.changes.list',
          'workspaces.references.read',
          'sandbox.setup.status', 'sandbox.capability.status',
          ...(options.draftModelName ? ['models.list'] : []),
          ...(options.workspace ? ['workspaces.list'] : []),
          ...(options.terminal ? ['terminal.create', 'terminal.write', 'terminal.resize', 'terminal.close'] : []),
          ...(modernSnapshots ? ['sessions.messages.snapshot.read', 'sessions.messages.resume',
            'sessions.messages.snapshot.release', 'transport.flow.update'] : []),
        ] }, auth: { runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          ...(options.terminal ? { principal: { capabilities: ['chat.read', 'chat.write', 'host.execute'] } } : {}),
        } }))
        return
      }
      if (options.terminal && frame.method.startsWith('terminal.')) {
        if (frame.method === 'terminal.create') respond({ terminalId: 'layout-terminal', sessionKey: key,
          target: frame.params?.target, cwdPath: 'C:/fixtures/layout', cols: frame.params?.cols ?? 100,
          rows: frame.params?.rows ?? 30, shell: 'Synthetic PTY' })
        else if (frame.method === 'terminal.resize') respond({ terminalId: 'layout-terminal', cols: frame.params?.cols, rows: frame.params?.rows })
        else if (frame.method === 'terminal.close') respond({ terminalId: 'layout-terminal', closed: true })
        else respond({ terminalId: 'layout-terminal', accepted: true, bytes: String(frame.params?.data ?? '').length })
        return
      }
      if (modernSnapshots && frame.method === 'sessions.messages.snapshot.read') {
        const revision = String(frame.params?.sync_revision)
        const owner = `${key}:${revision}`
        let snapshot = snapshots.get(owner)
        if (!snapshot) {
          const value = sessionMessagesSnapshotPayload(key, { stream_generation: `generation-${key}`,
            current_stream_seq: stream.sequence, task_id: stream.task,
            events: stream.task ? stream.events.filter(event => event.event.startsWith('session.event.')) : [] })
          snapshot = { id: `host-snapshot-${connectionId}-${snapshots.size + 1}`, key,
            bytes: Buffer.from(JSON.stringify(value)), sequence: stream.sequence }
          snapshots.set(owner, snapshot)
        }
        const index = Number(frame.params?.segment_index ?? 0), size = 192 * 1024
        respond({ key, sync_revision: revision, snapshot_id: snapshot.id, segment_index: index,
          segment_count: Math.ceil(snapshot.bytes.length / size), byte_length: snapshot.bytes.length,
          encoding: 'base64-json-utf8', data: snapshot.bytes.subarray(index * size, (index + 1) * size).toString('base64'),
          stream_generation: `generation-${key}`, current_stream_seq: snapshot.sequence,
          task_id: stream.task, session_id: null, session_epoch: 1,
          delivery: { delivery_epoch: deliveryEpoch, delivery_id: ++nextDelivery } })
        return
      }
      if (modernSnapshots && frame.method === 'transport.flow.update') {
        acknowledged = Math.max(acknowledged, Number(frame.params?.ack_delivery_id ?? 0))
        respond({ delivery_epoch: deliveryEpoch, ack_delivery_id: acknowledged, dirty_keys: [], global_dirty: false })
        return
      }
      if (modernSnapshots && frame.method === 'sessions.messages.resume') {
        const snapshot = snapshots.get(`${key}:${frame.params?.sync_revision}`)
        if (!snapshot || snapshot.id !== frame.params?.snapshot_id) {
          ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: {
            code: 'SNAPSHOT_STALE', message: 'Snapshot installation is no longer available', retryable: false } }))
          return
        }
        snapshotInstalls.push({ key, connection: connectionId, id: snapshot.id })
        respond({ ...frame.params, session_id: null, session_epoch: 1, replay_to_seq: frame.params?.stream_seq })
        return
      }
      if (modernSnapshots && frame.method === 'sessions.messages.snapshot.release') {
        snapshots.delete(`${key}:${frame.params?.sync_revision}`)
        respond({ ...frame.params, retired: true })
        return
      }
      if (frame.method === 'sessions.sidechat.create') {
        if (failNextCreate) {
          failNextCreate = false
          ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: false,
            error: { code: 'INTERNAL_ERROR', message: 'Synthetic side creation failure' } }))
          return
        }
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
      if (frame.method === 'sessions.routing.set') {
        const previous = routingSnapshot(key)
        const snapshot = { ...previous, mode: frame.params?.mode ?? previous.mode,
          revision: Number(previous.revision) + 1,
          ...('modelSelection' in (frame.params ?? {}) ? { modelSelection: frame.params?.modelSelection } : {}) }
        sessionRouting.set(key, snapshot)
        respond(snapshot)
        return
      }
      if (frame.method === 'chat.send') {
        if (rejectedNextSends.delete(key)) {
          ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: false,
            error: { code: 'SYNTHETIC_ADMISSION_FAILURE', message: 'Synthetic send was not admitted',
              accepted: false, retryable: true } }))
          return
        }
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
      if (frame.method === 'chat.clarify_submit') {
        const requestId = String(frame.params?.requestId ?? frame.params?.request_id)
        heldClarifications.set(requestId, { key, reply: accepted => {
          pendingInputs.set(key, [])
          ws.send(JSON.stringify(accepted
            ? { type: 'res', id: frame.id, ok: true, payload: { resolved: true, request_id: requestId } }
            : { type: 'res', id: frame.id, ok: false, error: { code: 'USER_INPUT_EXPIRED', message: 'The user-input request is no longer available.' } }))
        } })
        return
      }
      if (['workbench.resources.get', 'workbench.resources.open'].includes(frame.method)) {
        const ref = frame.params?.resourceRef as Wire
        const workspaceFile = frame.params?.workspaceFile as Wire | undefined
        const id = workspaceFile
          ? workspaceDocuments.get(`${key}:${workspaceFile.path}`) || ''
          : String(ref?.documentId ?? ref?.artifactId ?? ref?.id)
        const delivery = deliveries.get(`${key}:${id}`)
        const document = documents.get(`${key}:${id}`)
          || documents.get(`${key}:${delivery?.artifact.documentId}`)
        if (!document) { respond({ resource: null }); return }
        const fixed = ref?.type === 'deliverable' ? { ...(delivery?.resource ?? document.resource),
          resource: { type: 'deliverable', artifactId: id },
          downloadUrl: `/api/v1/artifacts/${id}`,
          relations: document.resource.relations,
        } : document.resource
        const reply = () => respond(frame.method === 'workbench.resources.get' ? { resource: fixed }
          : { disposition: 'document', resolution: { status: 'current' }, ...document, materialized: false,
              ...(frame.params?.intent === 'annotate' ? { baselineUpdated: false } : {}) })
        if (frame.method === 'workbench.resources.get' && deferredResources.has(`${key}:${id}`)) heldResourceResolves.set(`${key}:${id}`, reply)
        else reply()
        return
      }
      const canonicalDocument = documents.get(`${key}:${frame.params?.documentId}`)
      if (canonicalDocument && frame.method === 'artifacts.documents.get') { respond({ document: canonicalDocument.document }); return }
      if (canonicalDocument && frame.method === 'artifacts.revisions.list') { respond({ revisions: canonicalDocument.revisions }); return }
      if (frame.method === 'artifacts.changes.list') { respond({ changeSets: [] }); return }
      if (frame.method === 'workspaces.references.read') {
        const reference = frame.params?.reference as Wire
        const locator = reference.locator as Wire
        if (binaryReferences.has(`${key}:${locator.relativePath}`)) {
          ws.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: {
            code: 'FILE_UNAVAILABLE', message: 'Only UTF-8 source files can be previewed.',
          } }))
          return
        }
        respond({ reference, relativePath: locator.relativePath, revision: (reference.state as Wire).revision,
          content: key === SIDE ? 'const sideReference = 1;' : 'const mainReference = 1;', totalLines: 1,
          startLine: locator.startLine, endLine: locator.endLine })
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
        'workspaces.list': { workspaces: [{ id: 'fixture-workspace', name: 'Layout workspace', path: 'C:/fixtures/layout',
          taskCount: 1, pinned: false, available: true }] },
        'commands.list_for_surface': { commands: ['/new', '/plan', '/goal', '/usage', '/compact'].map(name => ({ name })) },
        'config.get': configuration,
        'models.routing.get': { mode: 'direct' },
        'models.list': { models: options.draftModelName ? [{ id: options.draftModelName, name: options.draftModelName,
          provider: 'synthetic', contextWindow: 128000, maxOutputTokens: 4096, capabilities: ['text'],
          pricing: { inputPer1k: 0, outputPer1k: 0 }, source: 'configured', reasoningFormat: 'none', metadata: null }] : [], errors: [] },
        'sessions.routing.get': routingSnapshot(key),
        'onboarding.status': { audioConfigured: options.audioConfigured ?? false, configured: true },
        'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
        'sandbox.setup.status': { state: 'ready', platform: 'linux', message: 'Synthetic sandbox ready', requiresAdmin: false },
        'sandbox.capability.status': { available: true, backend: 'bubblewrap', platform: 'linux', code: 'ready',
          reason: 'Synthetic sandbox ready', setupSupported: false, restartRequired: false, probeVersion: 1,
          capabilities: ['filesystem', 'network', 'process'] },
        'sessions.list': { sessions: [{ key: MAIN, title: 'Main parity conversation', sessionKind: 'chat',
          surface: 'webchat', conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: NOW,
          messageCount: get(MAIN).history.length, status: 'ok', runStatus: get(MAIN).task ? 'running' : 'idle' },
          ...(includeOtherSession ? [{ key: OTHER, title: 'Other parity conversation', sessionKind: 'chat', surface: 'webchat',
            conversationKind: 'direct', effectiveAgentId: 'main', updatedAt: NOW, messageCount: get(OTHER).history.length, status: 'ok', runStatus: 'idle' }] : [])], count: includeOtherSession ? 2 : 1, ts: NOW, has_more: false },
        'sessions.resolve': { session_key: key, session_id: key === MAIN ? 'main-id' : key === SIDE ? 'side-session-id' : 'other-session-id' },
        'chat.history': chatHistoryPayload(stream.history),
        'sessions.messages.subscribe': sessionMessagesSubscribePayload(key, metadata(key)),
        'sessions.messages.hydrate': sessionMessagesHydratePayload(key, metadata(key)),
        'sessions.messages.snapshot': sessionMessagesSnapshotPayload(key, { ...metadata(key), task_id: stream.task,
          events: stream.task ? stream.events.filter(event => event.event.startsWith('session.event.')) : [] }),
        'sessions.messages.unsubscribe': { subscribed: false },
        'sessions.subscribe': { subscribed: true },
        'usage.status': { sessions: [] },
        'workbench.resources.list': (() => {
          const resources = resourceCatalogueEnabled ? [...documents.values()]
            .filter(item => item.document.sessionKey === key).map(item => item.resource) : []
          return { resources, totalCount: resources.length, pageSize: 200, returnedCount: resources.length, hasMore: false, nextCursor: null }
        })(),
      }
      respond(payloads[frame.method] ?? {})
    })
  })
  return { requests, errors, consoleWarnings, emit, finish, approvalResolutions, fileRequests, snapshotInstalls,
    failApproval: (id: string, fail: boolean) => fail ? failedApprovals.add(id) : failedApprovals.delete(id),
    denyFile: (key: string, path: string, deny: boolean) => deny ? deniedFiles.add(`${key}:${path}`) : deniedFiles.delete(`${key}:${path}`),
    markBinaryReference: (key: string, path: string) => binaryReferences.add(`${key}:${path}`),
    enableOtherSession: () => {
      includeOtherSession = true
      get(OTHER).history = [{ role: 'assistant', text: '[Other resource](other.ts)', id: 'other-answer', timestamp: NOW }]
    },
    enableResourceCatalogue: () => { resourceCatalogueEnabled = true },
    registerDocument: (key: string, documentId: string, name: string,
      metadata: { format?: string; mime?: string; sha256?: string; size?: number; annotation?: boolean; workspacePath?: string;
        revisionId?: string; artifactId?: string } = {}) => {
      const revisionId = metadata.revisionId ?? `rev-${documentId}`, artifactId = metadata.artifactId ?? `art-${documentId}`
      const revisions = documents.get(`${key}:${documentId}`)?.revisions ?? []
      const mime = metadata.mime ?? 'text/html', size = metadata.size ?? 4096, sha256 = metadata.sha256 ?? 'b'.repeat(64)
      const document = { documentId, headRevisionId: revisionId, name, sessionKey: key, kind: metadata.format ?? 'html',
        latestDownloadUrl: `/api/v1/artifact-documents/${documentId}`, capabilities: { download: true, preview: true, revisions: true, source: false,
          ...(metadata.annotation ? { selectionContext: true, agentEdit: true } : {}) } }
      const revision = { revisionId, documentId, artifactId, filename: name, mediaType: mime, byteSize: size,
        artifactSha256: sha256, generation: revisions.length + 1,
        downloadUrl: `/api/v1/artifact-documents/${documentId}?revisionId=${revisionId}` }
      const resource = { resource: { type: 'document', documentId }, name, mime, sha256, size,
        ...(metadata.workspacePath ? { sourcePath: metadata.workspacePath } : {}),
        downloadUrl: document.latestDownloadUrl, capabilities: { preview: true, download: true, edit: metadata.annotation === true,
          ...(metadata.annotation ? { selectionContext: true, agentEdit: true } : {}) },
        relations: { documentId, headRevisionId: revisionId, headArtifactId: artifactId } }
      const artifact = { id: artifactId, documentId, name, mime, size, sha256, download_url: revision.downloadUrl }
      documents.set(`${key}:${documentId}`, { document, revision, resource, artifact,
        revisions: [revision, ...revisions.filter(item => item.revisionId !== revisionId)] })
      deliveries.set(`${key}:${artifactId}`, { resource, artifact })
      if (metadata.workspacePath) workspaceDocuments.set(`${key}:${metadata.workspacePath}`, documentId)
      return artifact
    },
    deferResource: (key: string, id: string) => deferredResources.add(`${key}:${id}`),
    releaseResource: (key: string, id: string) => {
      const identity = `${key}:${id}`
      const release = heldResourceResolves.get(identity)
      if (!release) throw new Error(`No pending resource resolve: ${identity}`)
      heldResourceResolves.delete(identity)
      deferredResources.delete(identity)
      release()
    },
    requestInput: (key: string) => {
      const request = { kind: 'user_input', paused: true, request_id: `clarify-${key}`,
        run_id: get(key).task, step: 'clarify', clarify_schema: { intro: 'One detail is required.',
          fields: [{ name: 'topic', prompt: 'Project topic', type: 'string', required: true }] } }
      pendingInputs.set(key, [request])
      emit(key, 'session.event.tool_result', { tool_use_id: 'call-questionnaire', name: 'request_user_input', result: JSON.stringify(request) })
      return request
    },
    answerInput: (key: string, accepted: boolean) => {
      const pending = heldClarifications.get(`clarify-${key}`)
      if (!pending || pending.key !== key) throw new Error(`No submitted clarification for ${key}`)
      pending.reply(accepted)
      heldClarifications.delete(`clarify-${key}`)
    },
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
    setRoutingConflict: (conflict: boolean) => { routingConflict = conflict },
    failNextSideCreate: () => { failNextCreate = true },
    rejectNextSend: (key: string) => { rejectedNextSends.add(key) },
    releaseAcks: () => heldAcks.splice(0).forEach(release => release()),
    pendingApprovals: (items: Wire[]) => { pendingApprovals = items },
    reconnect: () => socket.close({ code: 1012, reason: 'Synthetic reconnect' }) }
}
