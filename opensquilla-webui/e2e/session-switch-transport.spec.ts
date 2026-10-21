import {
  expect,
  test,
  type Page,
  type WebSocketRoute,
} from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { helloOkResponse } from './support/gateway-fixture'
import { expectComposerValue } from './utils'

import {
  chatHistoryPayload,
  sessionMessagesHydratePayload,
  sessionMessagesSnapshotPayload,
  sessionMessagesSubscribePayload,
} from './support/session-read-fixtures'

const CONTROL_URL = '/control/'
const SESSION_A = 'agent:main:webchat:e2e-workspace-a'
const SESSION_B = 'agent:main:webchat:e2e-workspace-b'
const WORKSPACE_A = 'workspace-e2e-a'
const WORKSPACE_B = 'workspace-e2e-b'
const STALE_A_TEXT = 'This late Workspace A event must never render in Workspace B.'
const FRESH_B_TEXT = 'Workspace B continues on the original transport.'
const PRESERVED_DRAFT = 'Keep this Workspace B draft while live updates recover.'

type RpcRequest = {
  id?: string | number
  method?: string
  params?: Record<string, unknown>
  type?: string
}

type HeldRequest = {
  frame: RpcRequest
  socket: WebSocketRoute
}

type WireRequest = {
  key: string
  method: string
  socketIndex: number
}

function response(id: RpcRequest['id'], payload: unknown) {
  return JSON.stringify({ type: 'res', id, ok: true, payload })
}

function eventFrame(event: string, payload: Record<string, unknown>) {
  return JSON.stringify({ type: 'event', event, payload })
}

function workspace(id: string, name: string, path: string) {
  return {
    id,
    name,
    path,
    taskCount: 1,
    pinned: false,
    available: true,
    removed: false,
  }
}

function session(key: string, title: string, workspaceId: string, path: string, updatedAt: number) {
  return {
    key,
    title,
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    effectiveAgentId: 'main',
    updatedAt,
    messageCount: 1,
    status: 'ok',
    runStatus: 'idle',
    workspaceId,
    workspace: path,
  }
}

function subscriptionPayload(key: string) {
  const isA = key === SESSION_A
  return sessionMessagesSubscribePayload(key, {
    hydration_complete: true,
    current_stream_seq: isA ? 10 : 20,
    stream_generation: 'workspace-switch-generation',
    run_status: 'idle',
    workspaceId: isA ? WORKSPACE_A : WORKSPACE_B,
    projectWorkspace: isA
      ? workspace(WORKSPACE_A, 'Workspace A', '/fixtures/workspace-a')
      : workspace(WORKSPACE_B, 'Workspace B', '/fixtures/workspace-b'),
  })
}

function snapshotPayload(key: string) {
  return sessionMessagesSnapshotPayload(key, {
    current_stream_seq: key === SESSION_A ? 10 : 20,
    stream_generation: 'workspace-switch-generation',
    run_status: 'idle',
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
    'sandbox.run_mode.preference.get': { runMode: 'full', source: 'config' },
    'onboarding.status': { audioConfigured: false },
    'sessions.subscribe': { subscribed: true },
    'usage.status': { sessions: [] },
  }
  return payloads[method] ?? {}
}

async function preparePage(page: Page) {
  await page.route('**/api/system/update', route => route.fulfill({ json: {} }))
  await page.route('**/api/elevated-mode', route => route.fulfill({ json: { enabled: false } }))
  await page.addInitScript(() => {
    window.localStorage.setItem('opensquilla-locale', 'en')
    const surfaceOk = async () => ({ ok: true })
    // Keep the Desktop bridge available while session-scoped annotation drafts
    // stay local and the ordinary chat transport changes subscriptions.
    window.opensquillaDesktop = {
      getOsLocale: async () => 'en',
      isAutoUpdateEnabled: async () => false,
      getGatewayStatus: async () => ({
        url: '', port: 0, owned: true, status: 'ready', logPath: '',
      }),
      revealGatewayLog: async () => false,
      getDesktopSettings: async () => ({
        provider: '', model: '', baseUrl: '', apiKeyConfigured: false,
        searchProvider: '', searchApiKeyEnv: '', searchApiKeyConfigured: false,
        disableNetworkObservability: false,
        gateway: { url: '', port: 0, owned: true, status: 'ready', logPath: '' },
      }),
      saveDesktopSettings: async () => { throw new Error('not used') },
      resetDesktopSettings: async () => ({ ok: true }),
      getOnboardingDefaults: async () => ({}),
      saveOnboarding: async () => ({}),
      cancelOnboarding: async () => ({}),
      retryStartup: async () => ({ ok: true }),
      quitApp: async () => ({}),
      getBootState: async () => ({}),
      onBootStatus: () => () => undefined,
      onBootError: () => () => undefined,
      openArtifact: async () => ({ ok: false }),
      chooseProjectDirectory: async () => null,
      createArtifactPreviewLease: surfaceOk,
      renewArtifactPreviewLease: surfaceOk,
      revokeArtifactPreviewLease: surfaceOk,
      createWorkbenchSurface: surfaceOk,
      setWorkbenchSurfaceRect: surfaceOk,
      activateWorkbenchSurface: surfaceOk,
      destroyWorkbenchSurface: surfaceOk,
      onWorkbenchSurfaceEvent: () => () => undefined,
      getArtifactAnnotationCapabilities: async () => ({
        version: 3, available: true, picker: true, trustedOverlay: true,
      }),
      setArtifactAnnotationMode: surfaceOk,
      showArtifactAnnotationOverlay: surfaceOk,
      closeArtifactAnnotationOverlay: surfaceOk,
      screenshot: async () => ({}),
    } as unknown as OpenSquillaDesktopApi
  })
  await page.route('**/api/approvals', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ pending: [], mode: 'prompt', allowPatterns: [], denyPatterns: [] }),
  }))
}

async function prepareWarmLiveRace(page: Page, modern = false, longHistoryText = '', activeB = false,
  historyAfterAdvance: 'canonical' | 'fallback' | 'read-error' = 'canonical', router = false,
  initialHistory: 'canonical' | 'fallback' = 'canonical') {
  await preparePage(page)
  if (router) {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.addInitScript(() => window.localStorage.setItem('opensquilla.routerVisualEffects', '1'))
  }
  const startedAt = Date.now() - 10_000
  const sockets: WebSocketRoute[] = []
  const held: HeldRequest[] = []
  const requests: WireRequest[] = []
  const failures: string[] = []
  let holdHistory = false
  let holdLive = false
  let holdOwnership = false
  let holdSnapshots = false
  let rejectLive = false
  let rejectOwnership = false
  let rejectedLiveReads = 0
  let nextDelivery = 1
  const deliveryEpoch = 'warm-race-delivery'
  const readMethod = 'sessions.messages.snapshot.read'
  const resumeMethod = 'sessions.messages.resume'
  const releaseMethod = 'sessions.messages.snapshot.release'
  const modernSnapshots = new Map<string, { bytes: Buffer; value: ReturnType<typeof sessionMessagesSnapshotPayload> }>()
  let revision = 1
  let epoch = 0
  let terminal = false
  let advancedHistoryReads = 0
  let degradedHistory = initialHistory === 'fallback'
  const routerModels = ['synthetic/fast', 'synthetic/selected', 'synthetic/reasoning', 'synthetic/quality']
  const liveEvents: Array<{ event: string; payload: Record<string, unknown> }> = []
  let replayOnResume = false
  let handoffAfterAck: number | null = null
  let handoffCount = 0
  const emitTail = (socket: WebSocketRoute, text: string, replayed = false) => {
    const seq = currentSequence() + 1
    const event = { event: 'session.event.text_delta', payload: {
      key: SESSION_A, session_key: SESSION_A, task_id: taskId(), turn_id: taskId(),
      epoch, stream_generation: 'workspace-switch-generation', stream_seq: seq,
      emitted_at: startedAt + seq * 100, text, presentation: 'answer',
    } }
    liveEvents.push(event)
    const deliveryId = nextDelivery++
    socket.send(JSON.stringify({ type: 'event', ...event, meta: {
      flow: { delivery_epoch: deliveryEpoch, delivery_id: deliveryId },
      ...(replayed ? { replayed: true } : {}),
    } }))
    return deliveryId
  }
  const currentSequence = () => 7 + liveEvents.length
  const taskId = (key = SESSION_A) => `warm-live-task-${key === SESSION_B ? 'b-' : ''}${epoch}`
  page.on('pageerror', error => failures.push(error.message))
  const history = (key: string) => {
    if (key !== SESSION_A) return chatHistoryPayload([{
      role: 'assistant', text: `Warm live B history${activeB ? '\n\n' + Array.from({ length: 6 }, (_, index) =>
        `Short session paragraph ${index + 1}. A second task is running, with a shorter transcript than the reading session.`).join('\n\n') : ''}`,
      message_id: 'warm-b', timestamp: startedAt,
    }])
    return chatHistoryPayload([{
      role: 'assistant', text: `Warm A history revision ${revision}${longHistoryText ? `\n\n${longHistoryText}` : ''}`,
      message_id: `warm-a-${epoch}`, timestamp: startedAt - 1000,
    }, {
      role: 'user', text: `Prepare Python source ${epoch}`, message_id: `warm-user-${epoch}`, timestamp: startedAt,
      turn_context: { turn_id: taskId() },
    }], { ...(degradedHistory || (historyAfterAdvance === 'fallback' && revision > 1)
      ? { canonical_available: false, canonical_complete: false } : {}), turn_outcomes: terminal ? [{
      turn_id: taskId(), task_id: taskId(), status: 'failed', started_at: startedAt, finished_at: startedAt + 8000,
      outcome: { kind: 'failed', reason: '429', error_class: '429', failure_kind: 'rate_limited', retryable: true },
    }] : [] })
  }
  const metadata = (key: string) => {
    const active = (key === SESSION_A || activeB) && !terminal
    return {
      ...subscriptionPayload(key), epoch: key === SESSION_A ? epoch : 0,
      current_stream_seq: active ? currentSequence() : 0,
      run_status: active ? 'running' : 'idle',
      active_task: active ? { task_id: taskId(key), turn_id: taskId(key), status: 'running', started_at: startedAt } : null,
      tasks: active ? [{ task_id: taskId(key), status: 'running', started_at: startedAt }] : [],
    }
  }
  const snapshot = (key: string) => {
    const active = (key === SESSION_A || activeB) && !terminal
    const ev = (name: string, seq: number, payload: Record<string, unknown>) => ({
      event: `session.event.${name}`, payload: {
        key, session_key: key, task_id: taskId(key), turn_id: taskId(key), epoch, generation_epoch: 0,
        stream_generation: 'workspace-switch-generation', stream_seq: seq, emitted_at: startedAt + seq * 100,
        ...payload,
      },
    })
    return sessionMessagesSnapshotPayload(key, {
      epoch: key === SESSION_A ? epoch : 0, task_id: active ? taskId(key) : null,
      current_stream_seq: active ? currentSequence() : 0, stream_generation: 'workspace-switch-generation',
      events: active ? [
        ev('provider_activity', 1, { activity_id: `provider-${epoch}`, phase: 'reasoning', reason: 'initial', started_at: startedAt }),
        ev('thinking_start', 2, { block_id: 'warm-thinking', block_index: 0, started_at: startedAt }),
        ev('thinking', 3, { block_id: 'warm-thinking', block_index: 0, text: `Warm A reasoning revision ${revision}` }),
        ev('thinking_end', 4, { block_id: 'warm-thinking', block_index: 0, status: 'completed', ended_at: startedAt + 400 }),
        ev('text_delta', 5, { text: `Narration before draft revision ${revision}`, presentation: 'answer' }),
        ev('provider_activity', 6, { activity_id: `provider-${epoch}`, phase: 'tool_calling', reason: 'initial', started_at: startedAt + 600 }),
        ev('tool_input_preview', 7, { preview_id: `warm-draft-${epoch}`, attempt_id: `warm-attempt-${epoch}`,
          tool_use_id: 'warm-code', revision, state: 'generating', tool_name: 'execute_code', path: '',
          content: `print("draft revision ${revision}")`, truncated: false }),
        ...(key === SESSION_A ? liveEvents : []),
      ] : [],
    })
  }
  const reply = ({ frame, socket }: HeldRequest) => {
    const key = String(frame.params?.key || frame.params?.sessionKey || '')
    const method = String(frame.method || '')
    if (method === 'chat.history' && key === SESSION_A && revision > 1) {
      advancedHistoryReads++
      if (historyAfterAdvance === 'read-error' && advancedHistoryReads > 1) {
        socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: {
          code: 'INTERNAL_ERROR', message: 'Synthetic history refresh remains unavailable.', retryable: true,
        } }))
        return
      }
    }
    if (key === SESSION_A && ((rejectLive && method !== 'chat.history')
      || (rejectOwnership && method === 'sessions.messages.hydrate'))) {
      rejectedLiveReads++
      socket.send(JSON.stringify({ type: 'res', id: frame.id, ok: false, error: {
        code: 'SNAPSHOT_BUSY', message: 'Synthetic live read remains unavailable.', retryable: true, accepted: false,
      } }))
      return
    }
    if (method === readMethod) {
      const syncRevision = String(frame.params?.sync_revision)
      let staged = modernSnapshots.get(syncRevision)
      if (!staged) {
        const value = snapshot(key)
        staged = { value, bytes: Buffer.from(JSON.stringify(value)) }
        modernSnapshots.set(syncRevision, staged)
      }
      socket.send(response(frame.id, {
        key, sync_revision: syncRevision, snapshot_id: `warm-${syncRevision}`,
        segment_index: 0, segment_count: 1, byte_length: staged.bytes.length,
        encoding: 'base64-json-utf8', data: staged.bytes.toString('base64'),
        stream_generation: 'workspace-switch-generation', current_stream_seq: staged.value.current_stream_seq,
        task_id: staged.value.task_id, session_id: null, session_epoch: staged.value.epoch,
        delivery: { delivery_epoch: deliveryEpoch, delivery_id: nextDelivery++ },
      }))
      return
    }
    const payload = method === 'chat.history' ? history(key)
      : method === 'sessions.messages.snapshot' ? snapshot(key)
        : method === 'sessions.messages.hydrate' ? sessionMessagesHydratePayload(key, metadata(key))
          : key === SESSION_A && holdOwnership ? { ...metadata(key), hydration_complete: false,
            active_task: null, tasks: [], run_status: 'idle', deferred_fields: ['active_task', 'tasks', 'run_status'] } : metadata(key)
    socket.send(response(frame.id, payload))
  }
  await page.routeWebSocket(/\/ws$/, socket => {
    const socketIndex = sockets.push(socket) - 1
    socket.send(eventFrame('connect.challenge', {}))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as RpcRequest
      if (frame.type === 'ping') { socket.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      const key = String(frame.params?.key || frame.params?.sessionKey || '')
      requests.push({ method, key, socketIndex })
      if (method === 'connect') {
        socket.send(helloOkResponse({
          server: { version: 'e2e', conn_id: 'warm-live-race' }, policy: { concurrent_history_reads: true,
            ...(modern ? { transport_flow: { delivery_epoch: deliveryEpoch, window_frames: 128, window_bytes: 4 * 1024 * 1024 } } : {}) },
          features: { methods: ['sessions.messages.subscribe', 'sessions.messages.unsubscribe',
            'sessions.messages.snapshot', 'sessions.messages.hydrate', 'workspaces.list', 'artifacts.list',
            ...(modern ? [readMethod, resumeMethod, releaseMethod, 'transport.flow.update'] : [])], events: [] },
          auth: { principal: { isOwner: true, authState: 'authenticated' },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } },
        }))
        return
      }
      if (method === 'sessions.list') {
        socket.send(response(frame.id, { sessions: [
          session(SESSION_A, 'Warm race A', WORKSPACE_A, '/fixtures/workspace-a', 100),
          session(SESSION_B, 'Warm race B', WORKSPACE_B, '/fixtures/workspace-b', 200),
        ], count: 2, ts: 1_800_000_000, has_more: false }))
        return
      }
      if (method === 'transport.flow.update') {
        if (handoffAfterAck !== null && Number(frame.params?.ack_delivery_id || 0) >= handoffAfterAck) {
          handoffAfterAck = null
          // These arrived while the frozen replay was being consumed. The
          // Gateway hands them to the ordinary FIFO/ACK ledger before lifting
          // its barrier, without claiming they belong to the consumed proof.
          for (let i = 1; i <= 4; i++) emitTail(socket, ` [handoff-${i}] `, true)
          handoffCount++
        }
        socket.send(response(frame.id, { delivery_epoch: deliveryEpoch,
          ack_delivery_id: Number(frame.params?.ack_delivery_id || 0), dirty_keys: [], global_dirty: false }))
        return
      }
      if (method === resumeMethod || method === releaseMethod) {
        let replayToSeq = frame.params?.stream_seq
        if (method === resumeMethod && replayOnResume) {
          replayOnResume = false
          for (let i = 1; i <= 3; i++) handoffAfterAck = emitTail(socket, ` [replay-${i}] `, true)
          replayToSeq = currentSequence()
        }
        socket.send(response(frame.id, { ...frame.params, ...(method === resumeMethod
          ? { session_id: null, session_epoch: modernSnapshots.get(String(frame.params?.sync_revision))?.value.epoch,
              replay_to_seq: replayToSeq }
          : { retired: true }) }))
        return
      }
      if (['chat.history', 'sessions.messages.subscribe', 'sessions.messages.snapshot', 'sessions.messages.hydrate', readMethod].includes(method)) {
        if (key === SESSION_A && (method === 'chat.history' ? holdHistory
          : holdLive || (holdSnapshots && method === readMethod)
            || (method === 'sessions.messages.hydrate' && holdOwnership))) held.push({ frame, socket })
        else reply({ frame, socket })
        return
      }
      if (method === 'workspaces.list') {
        socket.send(response(frame.id, { workspaces: [
          workspace(WORKSPACE_A, 'Workspace A', '/fixtures/workspace-a'),
          workspace(WORKSPACE_B, 'Workspace B', '/fixtures/workspace-b'),
        ] })); return
      }
      if (method === 'artifacts.list') { socket.send(response(frame.id, { artifacts: [], has_more: false })); return }
      if (router && method === 'config.get') {
        socket.send(response(frame.id, {
          squilla_router: {
            enabled: true, rollout_phase: 'full', visual_mode: 'real_candidates',
            tiers: Object.fromEntries(routerModels.map((model, index) => [`c${index}`, { model }])),
          },
          llm_ensemble: { enabled: false }, permissions: {}, skills: {},
        }))
        return
      }
      if (router && ['models.routing.get', 'sessions.routing.get'].includes(method)) {
        socket.send(response(frame.id, { key, mode: 'router', revision: 0 }))
        return
      }
      socket.send(response(frame.id, basePayload(method)))
    })
  })
  return {
    sockets, requests, failures,
    replayDuringNextRecovery: () => { replayOnResume = true },
    handoffCount: () => handoffCount,
    emitLiveText: (text: string) => emitTail(sockets[0]!, text),
    emitRouterDecision(winner = 1) {
      const seq = currentSequence() + 1
      const decision = {
        event: 'session.event.router_decision',
        payload: {
          key: SESSION_A, session_key: SESSION_A, task_id: taskId(), turn_id: taskId(),
          epoch, stream_generation: 'workspace-switch-generation', stream_seq: seq,
          emitted_at: startedAt + seq * 100, tier: `c${winner}`, model: routerModels[winner],
          source: 'classifier', routing_applied: true,
          router_tier_snapshot: {
            version: 1, request_kind: 'text',
            tiers: routerModels.map((model, index) => ({ tier: `c${index}`, model, execution_kind: 'single_model' })),
          },
        },
      }
      liveEvents.push(decision)
      sockets[0]!.send(eventFrame(decision.event, decision.payload))
    },
    pending: (side: 'history' | 'live') => held.filter(item => side === 'history'
      ? item.frame.method === 'chat.history' : item.frame.method !== 'chat.history').length,
    hold: () => { holdHistory = true; holdLive = true },
    failLive: () => { rejectLive = true },
    failOwnership: () => { rejectOwnership = true },
    recoverOwnership: () => { rejectOwnership = false },
    canonicalHistory: () => { degradedHistory = false },
    dirtyCurrent: () => sockets[0]!.send(eventFrame('transport.flow.dirty', {
      delivery_epoch: deliveryEpoch, dirty_keys: [SESSION_A], global_dirty: false,
    })),
    rejectedLiveReads: () => rejectedLiveReads,
    deferSnapshots: () => { holdSnapshots = true },
    snapshotPending: () => held.filter(item => item.frame.method === readMethod).length,
    invalidateHeldSnapshot() {
      const request = held.find(item => item.frame.method === readMethod)
      if (!request) throw new Error('Expected a held modern snapshot read')
      request.socket.send(eventFrame('transport.flow.dirty', {
        delivery_epoch: deliveryEpoch, dirty_keys: [SESSION_A], global_dirty: false,
      }))
      held.splice(held.indexOf(request), 1)
      reply(request)
    },
    releaseSnapshots() {
      holdSnapshots = false
      for (const request of held.filter(item => item.frame.method === readMethod)) {
        held.splice(held.indexOf(request), 1)
        reply(request)
      }
    },
    deferOwnership: () => { holdOwnership = true },
    confirmRunningOwner: () => sockets[0]!.send(eventFrame('task.running', {
      key: SESSION_A, session_key: SESSION_A, task_id: taskId(), turn_id: taskId(),
      epoch, status: 'running', started_at: startedAt,
    })),
    ownershipPending: () => held.some(item => item.frame.method === 'sessions.messages.hydrate'),
    releaseOwnership() {
      holdOwnership = false
      for (const request of held.filter(item => item.frame.method === 'sessions.messages.hydrate')) {
        held.splice(held.indexOf(request), 1)
        reply(request)
      }
    },
    advance: (changedEpoch = false, failed = false) => { revision = 2; epoch = changedEpoch ? 1 : 0; terminal = failed },
    release(side: 'history' | 'live') {
      if (side === 'history') holdHistory = false
      else holdLive = false
      const matching = held.filter(item => side === 'history'
        ? item.frame.method === 'chat.history' : item.frame.method !== 'chat.history'
          && !(holdSnapshots && item.frame.method === readMethod)
          && !(holdOwnership && item.frame.method === 'sessions.messages.hydrate'))
      for (const request of matching) { held.splice(held.indexOf(request), 1); reply(request) }
    },
  }
}

async function expectWarmLiveFrames(page: Page, expandedCode = false, stopVisible = true) {
  // Sample actual rendered frames while the other RPC side remains blocked.
  // An immediate assertion alone could pass before the released reply renders.
  const frames = await page.evaluate(async expanded => {
    const samples = []
    for (let frame = 0; frame < 8; frame++) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      const surface = document.querySelector<HTMLElement>('.chat-message-surface')
      const preview = surface?.querySelector('[data-testid="tool-input-preview"]')
      const narration = surface?.querySelector('.activity-narration')
      const stop = document.querySelector<HTMLElement>('.chat-stop-btn')
      samples.push({
        inert: surface?.inert === true,
        history: surface?.textContent?.includes('Warm A history revision 1') === true,
        reasoning: surface?.querySelectorAll('[data-testid="reasoning-timeline"]').length === 1,
        draft: Boolean(preview),
        code: !expanded || preview?.querySelector('code')?.textContent === 'print("draft revision 1")',
        narrationBeforeDraft: Boolean(narration && preview
          && narration.textContent?.includes('Narration before draft revision 1')
          && (narration.compareDocumentPosition(preview) & Node.DOCUMENT_POSITION_FOLLOWING)),
        stopVisible: Boolean(stop?.getClientRects().length),
      })
    }
    return samples
  }, expandedCode)
  expect(frames).toHaveLength(8)
  for (const frame of frames) expect(frame).toEqual({
    inert: true, history: true, reasoning: true, draft: true, code: true, narrationBeforeDraft: true, stopVisible,
  })
}

for (const first of ['history', 'live'] as const) {
  test(`a running router winner stays visible without rescanning when ${first} restores first`, async ({ page }, testInfo) => {
    const gateway = await prepareWarmLiveRace(page, false, '', false, 'canonical', true)
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    await expect(page.getByText('Warm A history revision 1', { exact: true })).toBeVisible()
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    gateway.emitRouterDecision()
    const strip = page.locator('.router-fx')
    await expect(strip).toHaveCount(1)
    await expect(strip).toHaveAttribute('data-phase', 'scanning')
    await expect(strip).toHaveAttribute('data-phase', 'locked')
    await expect(strip.locator('.router-fx-cell.win .nm')).toHaveAttribute('aria-label', 'selected')
    await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
    await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
    gateway.hold()

    await page.evaluate(() => {
      type Sample = { stage: string; source: string; at: number; count: number; phase: string | null; winner: string | null }
      const state = window as Window & {
        routerSwitchSamples?: Sample[]
        routerSwitchStage?: string
        stopRouterSwitchRecording?: () => void
      }
      state.routerSwitchSamples = []
      state.routerSwitchStage = 'held'
      let active = true
      let frame = 0
      const sample = (source: string) => {
        if (!active) return
        const surface = document.querySelector('.chat-message-surface')
        if (!surface?.textContent?.includes('Warm A history revision 1')) return
        const strips = surface.querySelectorAll('.router-fx')
        state.routerSwitchSamples!.push({
          stage: state.routerSwitchStage!,
          source,
          at: performance.now(),
          count: strips.length,
          phase: strips[0]?.getAttribute('data-phase') ?? null,
          winner: strips[0]?.querySelector('.router-fx-cell.win .nm')?.getAttribute('aria-label') ?? null,
        })
      }
      const tick = () => { sample('frame'); if (active) frame = requestAnimationFrame(tick) }
      const observer = new MutationObserver(() => sample('mutation'))
      observer.observe(document.body, { subtree: true, childList: true, attributes: true })
      frame = requestAnimationFrame(tick)
      state.stopRouterSwitchRecording = () => {
        sample('stop')
        active = false
        cancelAnimationFrame(frame)
        observer.disconnect()
      }
    })
    const sampleFrames = async (stage: string) => page.evaluate(async stageName => {
      const state = window as Window & { routerSwitchStage?: string }
      state.routerSwitchStage = stageName
      for (let index = 0; index < 8; index++) {
        await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      }
    }, stage)
    try {
      await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
      await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
      await expect.poll(() => gateway.pending('live')).toBeGreaterThan(0)
      await expect(page.getByText('Warm A history revision 1', { exact: true })).toBeVisible()
      await sampleFrames('held')
      gateway.release(first)
      await sampleFrames(`${first}-released`)
      await page.evaluate(() => {
        (window as Window & { routerSwitchStage?: string }).routerSwitchStage = 'both-released'
      })
      gateway.release(first === 'history' ? 'live' : 'history')
      await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
      await sampleFrames('installed')
      await page.evaluate(() => {
        const state = window as Window & { stopRouterSwitchRecording?: () => void }
        state.stopRouterSwitchRecording?.()
      })

      const samples = await page.evaluate(() => (
        (window as Window & { routerSwitchSamples?: Array<{
          stage: string; count: number; phase: string | null; winner: string | null
        }> }).routerSwitchSamples ?? []
      ))
      expect(samples.length).toBeGreaterThan(16)
      expect(samples.filter(sample => sample.count !== 1
        || sample.phase === 'scanning' || sample.winner !== 'selected')).toEqual([])

      // A genuinely new decision must retain the normal selection animation.
      gateway.emitRouterDecision(2)
      await expect(strip.last()).toHaveAttribute('data-phase', 'scanning')
      await expect(strip.last()).toHaveAttribute('data-phase', 'locked')
      await expect(strip.last().locator('.router-fx-cell.win .nm')).toHaveAttribute('aria-label', 'reasoning')
      expect(gateway.sockets).toHaveLength(1)
      expect(gateway.failures).toEqual([])
    } finally {
      const samples = await page.evaluate(() => {
        const state = window as Window & {
          stopRouterSwitchRecording?: () => void
          routerSwitchSamples?: unknown[]
        }
        state.stopRouterSwitchRecording?.()
        return state.routerSwitchSamples
      })
      const evidencePath = testInfo.outputPath('router-session-switch-frames.json')
      await writeFile(evidencePath, JSON.stringify(samples, null, 2))
      await testInfo.attach('router-session-switch-frames', {
        path: evidencePath, contentType: 'application/json',
      })
    }
  })
}

test('a newer delivery gap keeps the complete warm view and Stop until automatic snapshot recovery', async ({ page }, testInfo) => {
  const gateway = await prepareWarmLiveRace(page, true)
  const warnings: string[] = []
  page.on('console', message => {
    if (message.type() === 'warning') warnings.push(message.text())
  })
  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  const draft = page.getByTestId('tool-input-preview')
  await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
  await draft.locator('button').click()
  await expect(draft.locator('code')).toHaveText('print("draft revision 1")')
  await expect(page.locator('.chat-stop-btn')).toBeEnabled()
  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
  gateway.hold()
  gateway.deferSnapshots()
  gateway.advance()
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect.poll(gateway.snapshotPending).toBe(1)
  gateway.release('history')
  gateway.release('live')
  await expectWarmLiveFrames(page, true)
  await page.evaluate(() => {
    const state = window as unknown as { warmRecoveryFrames: Record<string, boolean>[]; warmRecoveryRecording: boolean }
    state.warmRecoveryFrames = []
    state.warmRecoveryRecording = true
    const sample = () => {
      const surface = document.querySelector('.chat-message-surface')
      const stop = document.querySelector<HTMLElement>('.chat-stop-btn')
      state.warmRecoveryFrames.push({
        stop: Boolean(stop?.getClientRects().length),
        history: surface?.textContent?.includes('Warm A history revision') === true,
        reasoning: Boolean(surface?.querySelector('[data-testid="reasoning-timeline"]')),
        preview: Boolean(surface?.querySelector('[data-testid="tool-input-preview"]')),
      })
      if (state.warmRecoveryRecording) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })
  gateway.invalidateHeldSnapshot()
  // Exercise the real flow invalidation and recovery scheduler: no click,
  // reconnect, explicit retry, or mocked frontend lifecycle method.
  await expect.poll(() => warnings.some(text => text.includes('Snapshot invalidated by a newer delivery gap.'))).toBe(true)
  await expect.poll(gateway.snapshotPending, { timeout: 15_000 }).toBeGreaterThan(0)
  await expectWarmLiveFrames(page, true)
  gateway.releaseSnapshots()
  await expect(page.getByText('Warm A history revision 2', { exact: true })).toBeVisible()
  await expect(draft.locator('code')).toHaveText('print("draft revision 2")')
  await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
  const frames = await page.evaluate(() => {
    const state = window as unknown as { warmRecoveryFrames: Record<string, boolean>[]; warmRecoveryRecording: boolean }
    state.warmRecoveryRecording = false
    return state.warmRecoveryFrames
  })
  await testInfo.attach('warm-recovery-frames', { body: JSON.stringify(frames, null, 2), contentType: 'application/json' })
  expect(frames.length).toBeGreaterThan(8)
  for (const frame of frames) expect(frame).toEqual({ stop: true, history: true, reasoning: true, preview: true })
  expect(gateway.requests.filter(item => item.key === SESSION_A && item.method === 'sessions.messages.snapshot.read').length).toBeGreaterThanOrEqual(3)
  expect(gateway.requests.filter(item => item.method === 'chat.send' || item.method === 'chat.abort')).toEqual([])
  expect(gateway.sockets).toHaveLength(1)
  expect(gateway.failures).toEqual([])
})

test('continuous output crosses recovery handoff once without losing text or the readable view', async ({ page }, testInfo) => {
  const gateway = await prepareWarmLiveRace(page, true)
  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
  await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
  const count = (method: string) => gateway.requests.filter(item => item.key === SESSION_A && item.method === method).length
  const snapshotsBefore = count('sessions.messages.snapshot.read')
  const resumesBefore = count('sessions.messages.resume')
  await page.evaluate(() => {
    const state = window as unknown as {
      handoffFrames: { live: boolean; prefix: boolean; reasoning: boolean; draft: boolean }[]
      stopHandoffFrames: () => void
    }
    state.handoffFrames = []
    let active = true
    state.stopHandoffFrames = () => { active = false }
    const sample = () => {
      const live = document.querySelector('.conversation-live-edge')
      state.handoffFrames.push({
        live: Boolean(live?.getClientRects().length),
        prefix: live?.textContent?.includes('Narration before draft revision 1') === true,
        reasoning: Boolean(live?.querySelector('[data-testid="reasoning-timeline"]')),
        draft: Boolean(live?.querySelector('[data-testid="tool-input-preview"]')),
      })
      if (active) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })
  gateway.hold()
  gateway.replayDuringNextRecovery()
  gateway.dirtyCurrent()
  await expect.poll(gateway.snapshotPending).toBeGreaterThan(0)
  await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
  gateway.release('history')
  gateway.release('live')
  await expect.poll(gateway.handoffCount).toBe(1)
  await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
  const live = page.locator('.conversation-live-edge')
  const tokens = [
    ...Array.from({ length: 3 }, (_, i) => `[replay-${i + 1}]`),
    ...Array.from({ length: 4 }, (_, i) => `[handoff-${i + 1}]`),
  ]
  for (let i = 1; i <= 5; i++) {
    const token = `[live-${i}]`
    gateway.emitLiveText(` ${token} `)
    tokens.push(token)
    await expect(live).toContainText(token)
  }
  // Keep the task active through more than one automatic retry interval.
  // A moving recovery target must not cause another snapshot or blank frame.
  await page.evaluate(async () => {
    for (let i = 0; i < 80; i++) await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
  })
  const text = await live.innerText()
  let previous = -1
  for (const token of tokens) {
    expect(text.split(token)).toHaveLength(2)
    expect(text.indexOf(token)).toBeGreaterThan(previous)
    previous = text.indexOf(token)
  }
  await expect(page.locator('.chat-stop-btn')).toBeVisible()
  expect(count('sessions.messages.snapshot.read')).toBe(snapshotsBefore + 1)
  expect(count('sessions.messages.resume')).toBe(resumesBefore + 1)
  const frames = await page.evaluate(() => {
    const state = window as unknown as { handoffFrames: Record<string, boolean>[]; stopHandoffFrames: () => void }
    state.stopHandoffFrames()
    return state.handoffFrames
  })
  await testInfo.attach('continuous-recovery-frames', { body: JSON.stringify(frames), contentType: 'application/json' })
  expect(frames.length).toBeGreaterThan(80)
  expect(frames.filter(frame => Object.values(frame).some(value => !value))).toEqual([])
  expect(gateway.sockets).toHaveLength(1)
  expect(gateway.failures).toEqual([])
})

test('readable live output stays mounted while an unconfirmed snapshot retries', async ({ page }, testInfo) => {
  test.setTimeout(60_000)
  const gateway = await prepareWarmLiveRace(page, true)
  gateway.deferOwnership()
  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect.poll(gateway.ownershipPending).toBe(true)
  gateway.failOwnership()
  gateway.releaseOwnership()
  const narration = page.getByText('Narration before draft revision 1', { exact: true })
  await expect(narration).toBeVisible()
  gateway.hold()
  await page.evaluate(() => {
    const state = window as unknown as {
      liveRetryFrames: { live: boolean; narration: boolean; reasoning: boolean; draft: boolean }[]
      stopLiveRetryFrames: () => void
    }
    state.liveRetryFrames = []
    let recording = true
    state.stopLiveRetryFrames = () => { recording = false }
    const sample = () => {
      const live = document.querySelector('.conversation-live-edge')
      state.liveRetryFrames.push({
        live: Boolean(live?.getClientRects().length),
        narration: live?.textContent?.includes('Narration before draft revision 1') === true,
        reasoning: Boolean(live?.querySelector('[data-testid="reasoning-timeline"]')),
        draft: Boolean(live?.querySelector('[data-testid="tool-input-preview"]')),
      })
      if (recording) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })
  try {
    // Metadata failed after content installation, so the displayed live tail
    // is readable but unconfirmed. A delivery gap starts automatic recovery.
    gateway.dirtyCurrent()
    await expect.poll(() => gateway.pending('live') + gateway.pending('history'), { timeout: 15_000 }).toBeGreaterThan(0)
    await expect(narration).toBeVisible()
    await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
    gateway.recoverOwnership()
    gateway.release('history')
    gateway.release('live')
    await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
    await expect(narration).toBeVisible()
  } finally {
    const frames = await page.evaluate(() => {
      const state = window as unknown as {
        liveRetryFrames: { live: boolean; narration: boolean; reasoning: boolean; draft: boolean }[]
        stopLiveRetryFrames: () => void
      }
      state.stopLiveRetryFrames()
      return state.liveRetryFrames
    })
    const evidencePath = testInfo.outputPath('unconfirmed-live-retry-frames.json')
    await writeFile(evidencePath, JSON.stringify(frames, null, 2))
    await testInfo.attach('unconfirmed-live-retry-frames', { path: evidencePath, contentType: 'application/json' })
    expect(frames.length).toBeGreaterThan(2)
    expect(frames.filter(frame => !frame.live || !frame.narration || !frame.reasoning || !frame.draft)).toEqual([])
  }
  expect(gateway.failures).toEqual([])
  expect(gateway.requests.filter(item => item.method === 'chat.send' || item.method === 'chat.abort')).toEqual([])
})

for (const initialHistory of ['canonical', 'fallback'] as const) {
  test(`same-session recovery preserves ${initialHistory} live content, router and resized workbench`, async ({ page }, testInfo) => {
    test.setTimeout(60_000)
    const gateway = await prepareWarmLiveRace(page, true,
      '| Item | Count |\n|---|---|\n| Alpha | 12 |\n| Beta | 24 |',
      false, 'canonical', true, initialHistory)
    const warnings: string[] = []
    page.on('console', message => {
      if (message.type() === 'warning') warnings.push(message.text())
    })
    await page.setViewportSize({ width: 1724, height: 916 })
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    const narration = page.getByText('Narration before draft revision 1', { exact: true })
    await expect(narration).toBeVisible()
    gateway.emitRouterDecision()
    await expect(page.locator('.router-fx')).toHaveAttribute('data-phase', 'locked')
    await page.getByTestId('topbar-workbench-toggle').click()
    await expect(page.getByTestId('workbench-host')).toBeVisible()
    const separator = await page.getByRole('separator', { name: 'Resize workbench', exact: true }).boundingBox()
    if (!separator) throw new Error('Expected the workbench resize handle')
    await page.mouse.move(separator.x + separator.width / 2, separator.y + 100)
    await page.mouse.down()
    await page.mouse.move(1000, separator.y + 100, { steps: 15 })
    await page.mouse.up()
    await expect(narration).toBeVisible()

    type RecoveryFrame = {
      live: boolean; reasoning: number; draft: boolean; narration: boolean
      routerCount: number; phase: string | null; winner: string | null
      workbenchSame: boolean; workbenchVisible: boolean; width: number
      routerDocumentTop: number; routerOffset: number; liveOffset: number; liveHeight: number
    }
    type RecoveryRecorder = {
      recoveryFrames: RecoveryFrame[]
      stopRecoveryFrames: () => void
    }
    await page.evaluate(() => {
      const state = window as unknown as RecoveryRecorder
      const workbench = document.querySelector('[data-testid="workbench-host"]')
      state.recoveryFrames = []
      let recording = true
      state.stopRecoveryFrames = () => { recording = false }
      const sample = () => {
        const surface = document.querySelector('.chat-message-surface')
        const live = surface?.querySelector<HTMLElement>('.conversation-live-edge')
        const currentWorkbench = document.querySelector<HTMLElement>('[data-testid="workbench-host"]')
        const router = surface?.querySelector('.router-fx')
        const thread = document.querySelector('.app-chat-pane .chat-thread')
        // The surface itself uses display: contents and has no layout box.
        const listTop = surface?.querySelector('.chat-message-list')?.getBoundingClientRect().top ?? 0
        const routerTop = router?.getBoundingClientRect().top ?? 0
        const liveBox = live?.getBoundingClientRect()
        state.recoveryFrames.push({
          live: Boolean(live?.getClientRects().length),
          reasoning: surface?.querySelectorAll('[data-testid="reasoning-timeline"]').length ?? 0,
          draft: Boolean(surface?.querySelector('[data-testid="tool-input-preview"]')),
          narration: surface?.textContent?.includes('Narration before draft revision 1') === true,
          routerCount: surface?.querySelectorAll('.router-fx').length ?? 0,
          phase: router?.getAttribute('data-phase') ?? null,
          winner: router?.querySelector('.router-fx-cell.win .nm')?.getAttribute('aria-label') ?? null,
          workbenchSame: currentWorkbench === workbench,
          workbenchVisible: Boolean(currentWorkbench?.getClientRects().length),
          width: thread?.getBoundingClientRect().width ?? 0,
          routerDocumentTop: routerTop - (thread?.getBoundingClientRect().top ?? 0) + (thread?.scrollTop ?? 0),
          routerOffset: routerTop - listTop,
          liveOffset: (liveBox?.top ?? 0) - listTop,
          liveHeight: liveBox?.height ?? 0,
        })
        if (recording) requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    })
    try {
      gateway.deferSnapshots()
      gateway.dirtyCurrent()
      await expect.poll(gateway.snapshotPending).toBeGreaterThan(0)
      gateway.invalidateHeldSnapshot()
      await expect.poll(() => warnings.some(text => text.includes('Snapshot invalidated by a newer delivery gap.'))).toBe(true)
      await expect.poll(gateway.snapshotPending, { timeout: 15_000 }).toBeGreaterThan(0)
      gateway.releaseSnapshots()
      if (initialHistory === 'fallback') {
        // Failed reconciliation must preserve the readable cold view even when
        // no authoritative navigation snapshot has ever existed for it.
        await expect.poll(() => warnings.some(text => text.includes('History reconciliation is incomplete.'))).toBe(true)
        const reads = gateway.requests.filter(item => item.method === 'chat.history').length
        await expect.poll(() => gateway.requests.filter(item => item.method === 'chat.history').length,
          { timeout: 15_000 }).toBeGreaterThan(reads)
        gateway.canonicalHistory()
        gateway.dirtyCurrent()
      }
      await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
      await expect(narration).toBeVisible()
      await page.evaluate(async () => {
        for (let frame = 0; frame < 8; frame++) {
          await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
        }
      })
    } finally {
      const frames = await page.evaluate(() => {
        const state = window as unknown as RecoveryRecorder
        state.stopRecoveryFrames()
        return state.recoveryFrames
      })
      const evidencePath = testInfo.outputPath('same-session-recovery-frames.json')
      await writeFile(evidencePath, JSON.stringify(frames, null, 2))
      await testInfo.attach('same-session-recovery-frames', {
        path: evidencePath, contentType: 'application/json',
      })
      expect(frames.length).toBeGreaterThan(8)
      expect(frames.filter(frame => !frame.live || frame.reasoning !== 1 || !frame.draft || !frame.narration
        || frame.routerCount !== 1 || frame.phase === 'scanning' || frame.winner !== 'selected'
        || !frame.workbenchSame || !frame.workbenchVisible)).toEqual([])
      const widths = frames.map(frame => frame.width)
      expect(Math.max(...widths) - Math.min(...widths)).toBeLessThan(1)
      // A recovery notice may change the header above the message list.
      // Unchanged content inside that list must keep its layout throughout
      // recovery, including the live tail whose collapse caused the jitter.
      for (const field of ['routerOffset', 'liveOffset', 'liveHeight'] as const) {
        const values = frames.map(frame => frame[field])
        expect(Math.max(...values) - Math.min(...values), field).toBeLessThan(1)
      }
    }
    expect(gateway.requests.filter(item => item.method === 'chat.send' || item.method === 'chat.abort')).toEqual([])
    expect(gateway.failures).toEqual([])
  })
}

for (const first of ['history', 'live'] as const) {
  test(`warm history and prepared code hand off together when ${first} resolves first`, async ({ page }) => {
    const gateway = await prepareWarmLiveRace(page)
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    await expect(page.getByText('Warm A history revision 1', { exact: true })).toBeVisible()
    const draft = page.getByTestId('tool-input-preview')
    await expect(draft).toBeVisible()
    await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
    await draft.locator('button').click()
    await expect(draft.locator('code')).toHaveText('print("draft revision 1")')
    const reasoning = page.getByTestId('reasoning-timeline')
    await expect(reasoning).toHaveCount(1)
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
    await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
    gateway.hold()
    gateway.advance()
    await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
    await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
    await expect.poll(() => gateway.pending('live')).toBeGreaterThan(0)
    await expect(page.getByText('Warm A history revision 1', { exact: true })).toBeVisible()
    await expect(draft.locator('code')).toHaveText('print("draft revision 1")')
    await expect(reasoning).toHaveCount(1)
    await expect(draft.locator('.activity-motion--active')).toHaveCount(0)
    await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
    gateway.release(first)
    // One side resolving is insufficient to replace the complete, read-only view.
    await expectWarmLiveFrames(page, true)
    await expect(page.getByText('Warm A history revision 1', { exact: true })).toBeVisible()
    await expect(draft.locator('code')).toHaveText('print("draft revision 1")')
    await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
    gateway.release(first === 'history' ? 'live' : 'history')
    await expect(page.getByText('Warm A history revision 2', { exact: true })).toBeVisible()
    await expect(draft.locator('code')).toHaveText('print("draft revision 2")')
    await expect(reasoning).toHaveCount(1)
    await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
    expect(gateway.sockets).toHaveLength(1)
    expect(gateway.requests.filter(item => item.method === 'chat.send' || item.method === 'chat.abort')).toEqual([])
    expect(gateway.failures).toEqual([])
  })
}

for (const ownerKnown of [false, true]) {
  test(`warm presentation waits for deferred metadata with running ownership ${ownerKnown ? 'confirmed' : 'pending'}`, async ({ page }) => {
    const gateway = await prepareWarmLiveRace(page)
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    await expect(page.getByTestId('tool-input-preview')).toBeVisible()
    await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
    await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
    gateway.hold()
    gateway.advance()
    gateway.deferOwnership()
    await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
    await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
    await expect.poll(() => gateway.pending('live')).toBeGreaterThan(0)
    gateway.release('history')
    gateway.release('live')
    await expect.poll(gateway.ownershipPending).toBe(true)
    await expectWarmLiveFrames(page)
    // Cache may keep the Stop shape, but only current server ownership enables it.
    const stop = page.locator('.chat-stop-btn')
    await expect(stop).toBeVisible()
    await expect(stop).toBeDisabled()
    if (ownerKnown) {
      // A real lifecycle event can authorize Stop before the complete metadata
      // arrives. It must not be mistaken for completion of the hydration read.
      gateway.confirmRunningOwner()
      await expect(stop).toBeEnabled()
      await expectWarmLiveFrames(page)
      await expect(page.getByText('Warm A history revision 2', { exact: true })).toHaveCount(0)
    }
    gateway.releaseOwnership()
    await expect(page.getByText('Warm A history revision 2', { exact: true })).toBeVisible()
    await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
    await expect(stop).toBeEnabled()
    expect(gateway.requests.filter(item => item.method === 'chat.abort')).toEqual([])
    expect(gateway.failures).toEqual([])
  })
}

for (const historyAfterAdvance of ['canonical', 'fallback', 'read-error'] as const) {
  test(`changed epoch after history-first restoration handles ${historyAfterAdvance} history and releases the live view`, async ({ page }) => {
    const gateway = await prepareWarmLiveRace(page, false, '', false, historyAfterAdvance)
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    await expect(page.getByTestId('tool-input-preview')).toBeVisible()
    await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
    await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
    gateway.hold()
    gateway.advance(true)
    await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
    await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
    await expect.poll(() => gateway.pending('live')).toBeGreaterThan(0)
    const before = gateway.requests.filter(item => item.key === SESSION_A && item.method === 'chat.history').length
    gateway.release('history')
    await expectWarmLiveFrames(page)
    await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
    gateway.release('live')
    await expect.poll(() => gateway.requests.filter(item => item.key === SESSION_A && item.method === 'chat.history').length).toBeGreaterThan(before)
    await expect(page.getByText('Warm A history revision 2', { exact: true })).toBeVisible()
    await expect(page.getByTestId('tool-input-preview')).toBeVisible()
    await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
    await expect(page.getByText('Warm A history revision 1', { exact: true })).toHaveCount(0)
    expect(gateway.failures).toEqual([])
  })
}

for (const first of ['history', 'live'] as const) {
  test(`terminal failure replaces cached activity when ${first} resolves first`, async ({ page }) => {
    const gateway = await prepareWarmLiveRace(page)
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    await expect(page.getByTestId('tool-input-preview')).toBeVisible()
    await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
    await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
    gateway.hold()
    gateway.advance(false, true)
    await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
    await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
    await expect.poll(() => gateway.pending('live')).toBeGreaterThan(0)
    gateway.release(first)
    if (first === 'history') {
      // A durable terminal result supersedes this task's old live presentation
      // without waiting for the live read to recover.
      await expect(page.locator('.msg-error')).toHaveCount(1)
      await expect(page.getByTestId('tool-input-preview')).toHaveCount(0)
      await expect(page.locator('.chat-stop-btn')).toHaveCount(0)
    } else {
      // Let the authoritative idle metadata render before sampling frames.
      // It retires Stop while history still owns the final outcome content.
      await expect(page.locator('.chat-stop-btn')).toHaveCount(0)
      await expectWarmLiveFrames(page, false, false)
      await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
    }
    gateway.release(first === 'history' ? 'live' : 'history')
    await expect(page.locator('.msg-error')).toHaveCount(1)
    await expect(page.locator('.msg-error')).toContainText('The model is temporarily rate limited; this request did not finish.')
    await expect(page.getByTestId('tool-input-preview')).toHaveCount(0)
    await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
    await expect(page.locator('.chat-stop-btn')).toHaveCount(0)
    expect(gateway.sockets).toHaveLength(1)
    expect(gateway.failures).toEqual([])
  })
}

test('confirmed terminal history retires cached work while live reads keep failing', async ({ page }) => {
  const gateway = await prepareWarmLiveRace(page)
  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect(page.getByText('Narration before draft revision 1', { exact: true })).toBeVisible()
  await expect(page.getByTestId('tool-input-preview')).toBeVisible()
  await expect(page.locator('.chat-stop-btn')).toBeEnabled()
  await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
  await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
  gateway.hold()
  gateway.advance(false, true)
  gateway.failLive()
  await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
  await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
  await expect.poll(() => gateway.pending('live')).toBeGreaterThan(0)
  gateway.release('live')
  await expect.poll(gateway.rejectedLiveReads).toBeGreaterThan(0)
  await expectWarmLiveFrames(page)
  gateway.release('history')
  await expect(page.locator('.msg-error')).toHaveCount(1)
  await expect(page.locator('.msg-error')).toContainText('The model is temporarily rate limited; this request did not finish.')
  await expect(page.getByTestId('reasoning-timeline')).toHaveCount(0)
  await expect(page.getByTestId('tool-input-preview')).toHaveCount(0)
  await expect(page.locator('.chat-stop-btn')).toHaveCount(0)
  const rejectedBefore = gateway.rejectedLiveReads()
  await expect.poll(gateway.rejectedLiveReads, { timeout: 15_000 }).toBeGreaterThan(rejectedBefore)
  await expect(page.locator('.msg-error')).toHaveCount(1)
  await expect(page.getByTestId('reasoning-timeline')).toHaveCount(0)
  await expect(page.getByTestId('tool-input-preview')).toHaveCount(0)
  await expect(page.locator('.chat-stop-btn')).toHaveCount(0)
  expect(gateway.requests.filter(item => item.method === 'chat.send' || item.method === 'chat.abort')).toEqual([])
  expect(gateway.failures).toEqual([])
})

test('returning from a short active session restores the same paragraph with modern snapshots', async ({ page }, info) => {
  const longText = Array.from({ length: 18 }, (_, index) => `### Reader section ${String(index + 1).padStart(2, '0')}

This is a stable paragraph in the same assistant message. The reader is inspecting earlier work while a task continues.

Its place in the viewport must remain tied to this session, even when another session has a much shorter transcript.`).join('\n\n')
  const gateway = await prepareWarmLiveRace(page, true, longText, true)
  const headingText = 'Reader section 09'
  const measure = () => page.locator('.chat-thread').evaluate((thread, text) => {
    const heading = [...thread.querySelectorAll<HTMLElement>('h3')].find(element => element.textContent === text)
    const row = heading?.closest<HTMLElement>('[data-chat-message-key]')
    return {
      key: row?.dataset.chatMessageKey,
      index: row?.dataset.chatMessageIndex,
      heading: heading?.textContent,
      y: heading ? heading.getBoundingClientRect().top - thread.getBoundingClientRect().top : null,
      scrollTop: thread.scrollTop, scrollHeight: thread.scrollHeight, viewportHeight: thread.clientHeight,
      reading: thread.classList.contains('chat-thread--reading-history'),
    }
  }, headingText)
  const evidence: Record<string, unknown> = {}
  try {
    await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
    await expect(page.getByRole('heading', { name: headingText, exact: true })).toHaveCount(1)
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    await expect.poll(async () => (await measure()).scrollHeight).toBeGreaterThan(2000)
    await expect.poll(async () => {
      const position = await measure()
      return position.scrollHeight - position.scrollTop - position.viewportHeight
    }).toBeLessThan(4)
    await page.locator('.chat-thread').hover({ position: { x: 120, y: 160 } })
    const initial = await measure()
    await page.mouse.wheel(0, Number(initial.y) - 120)
    await expect(page.locator('.chat-thread')).toHaveClass(/chat-thread--reading-history/)
    await expect.poll(async () => Math.abs(Number((await measure()).y) - 120)).toBeLessThan(4)
    const saved = await measure()
    evidence.savedA = saved
    expect(saved.scrollTop).toBeGreaterThan(500)
    await page.screenshot({ path: info.outputPath('reader-a-before.png') })
    await page.locator(`[data-session-key="${SESSION_B}"] .sidebar-history-item`).click()
    await expect(page.getByText('Warm live B history', { exact: true })).toBeVisible()
    await expect(page.locator('.chat-stop-btn')).toBeEnabled()
    await expect.poll(async () => (await measure()).scrollTop).toBeGreaterThan(50)
    await expect.poll(async () => {
      const position = await measure()
      return position.scrollHeight - position.scrollTop - position.viewportHeight
    }).toBeLessThan(4)
    evidence.shortB = await measure()
    gateway.hold()
    await page.locator(`[data-session-key="${SESSION_A}"] .sidebar-history-item`).click()
    await expect.poll(() => gateway.pending('history')).toBeGreaterThan(0)
    gateway.release('live')
    await expect(page.getByRole('heading', { name: headingText, exact: true })).toHaveCount(1)
    await expect(page.locator('.chat-message-surface')).toHaveAttribute('inert', '')
    evidence.pendingA = await measure()
    await page.screenshot({ path: info.outputPath('reader-a-pending.png') })
    // Assert the same interior content coordinate, not just message identity,
    // reader mode, or the presence of cached DOM rows.
    await expect.poll(async () => Math.abs(Number((await measure()).y) - Number(saved.y))).toBeLessThan(4)
    const pending = await measure()
    expect(pending).toMatchObject({ key: saved.key, index: saved.index, heading: saved.heading, reading: true })
    gateway.release('history')
    await expect(page.locator('.chat-message-surface')).not.toHaveAttribute('inert', '')
    await expect.poll(async () => Math.abs(Number((await measure()).y) - Number(saved.y))).toBeLessThan(4)
    evidence.installedA = await measure()
    await page.screenshot({ path: info.outputPath('reader-a-installed.png') })
    expect(gateway.sockets).toHaveLength(1)
    expect(gateway.requests.filter(item => item.method === 'chat.send' || item.method === 'chat.abort')).toEqual([])
    expect(gateway.failures).toEqual([])
  } finally {
    evidence.final = await measure().catch(error => ({ error: String(error) }))
    await writeFile(info.outputPath('reader-position-evidence.json'), JSON.stringify(evidence, null, 2))
    await info.attach('reader-position-evidence', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' })
  }
})

test('returning to a visited session keeps history visible while its real bootstrap is pending', async ({ page }) => {
  await preparePage(page)
  const sockets: WebSocketRoute[] = []
  let holdA = false
  let aText = 'Known A history'
  const heldHistory: HeldRequest[] = []
  await page.routeWebSocket(/\/ws$/, socket => {
    sockets.push(socket)
    socket.send(eventFrame('connect.challenge', {}))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw)) as RpcRequest
      if (frame.type === 'ping') { socket.send(JSON.stringify({ type: 'pong' })); return }
      if (frame.type !== 'req') return
      const method = String(frame.method || '')
      const key = String(frame.params?.key || frame.params?.sessionKey || '')
      if (method === 'connect') {
        socket.send(helloOkResponse({
          server: { version: 'e2e', conn_id: 'warm-switch-conn' },
          features: { methods: ['sessions.messages.subscribe', 'sessions.messages.unsubscribe',
            'sessions.messages.snapshot', 'sessions.messages.hydrate', 'workspaces.list', 'artifacts.list'], events: [] },
          auth: { principal: { isOwner: true, authState: 'authenticated' },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' } },
        }))
        return
      }
      if (method === 'sessions.list') {
        socket.send(response(frame.id, { sessions: [
          session(SESSION_A, 'Warm A', WORKSPACE_A, '/fixtures/workspace-a', 100),
          session(SESSION_B, 'Warm B', WORKSPACE_B, '/fixtures/workspace-b', 200),
        ], count: 2, ts: 1_800_000_000, has_more: false }))
        return
      }
      if (method === 'chat.history') {
        if (holdA && key === SESSION_A) { heldHistory.push({ frame, socket }); return }
        socket.send(response(frame.id, chatHistoryPayload([{ role: 'assistant',
          text: key === SESSION_A ? aText : 'Known B history',
          message_id: key === SESSION_A ? 'known-a' : 'known-b', timestamp: '2026-10-13T00:00:00Z',
        }])))
        return
      }
      if (method === 'sessions.messages.subscribe') { socket.send(response(frame.id, subscriptionPayload(key))); return }
      if (method === 'sessions.messages.snapshot') { socket.send(response(frame.id, snapshotPayload(key))); return }
      if (method === 'sessions.messages.hydrate') {
        socket.send(response(frame.id, sessionMessagesHydratePayload(key, subscriptionPayload(key)))); return
      }
      if (method === 'workspaces.list') {
        socket.send(response(frame.id, { workspaces: [
          workspace(WORKSPACE_A, 'Workspace A', '/fixtures/workspace-a'),
          workspace(WORKSPACE_B, 'Workspace B', '/fixtures/workspace-b'),
        ] })); return
      }
      if (method === 'artifacts.list') { socket.send(response(frame.id, { artifacts: [], has_more: false })); return }
      socket.send(response(frame.id, basePayload(method)))
    })
  })
  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect(page.getByText('Known A history', { exact: true })).toBeVisible()
  const navigate = async (key: string) => {
    await page.locator(`[data-session-key="${key}"] .sidebar-history-item`).click()
    await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(key)
  }
  await navigate(SESSION_B)
  await expect(page.getByText('Known B history', { exact: true })).toBeVisible()
  holdA = true
  await navigate(SESSION_A)
  await expect.poll(() => heldHistory.length).toBeGreaterThan(0)
  // Authoritative history has not replied. The known target, never B's rows,
  // stays visible while the one ordinary subscription is re-established.
  await expect(page.getByText('Known A history', { exact: true })).toBeVisible()
  await expect(page.getByText('Known B history', { exact: true })).toHaveCount(0)
  await expect(page.locator('[data-testid="chat-message-row"]').first()).toHaveAttribute('inert', '')
  expect(sockets).toHaveLength(1)
  aText = 'A completed while away'
  holdA = false
  // Commit the server's new history and release all pending reads. Bootstrap
  // may retire an earlier read while its snapshot is being reconciled.
  for (const request of heldHistory.splice(0)) {
    request.socket.send(response(request.frame.id, chatHistoryPayload([
      { role: 'assistant', text: aText, message_id: 'known-a', timestamp: '2026-10-13T00:00:00Z' },
    ])))
  }
  await expect(page.getByText('A completed while away', { exact: true })).toBeVisible()
  await expect(page.getByText('Known A history', { exact: true })).toHaveCount(0)
  await expect(page.locator('[data-testid="chat-message-row"]').first()).not.toHaveAttribute('inert', '')
  expect(sockets).toHaveLength(1)
})

test('workspace navigation keeps one transport while the target subscription recovers', async ({
  page,
}) => {
  test.setTimeout(30_000)
  await preparePage(page)

  const sockets: WebSocketRoute[] = []
  const held = new Map<string, HeldRequest>()
  const wire: WireRequest[] = []
  const sends: Array<Record<string, unknown>> = []
  let bArrivedBeforeUnsubscribeAck = false

  await page.routeWebSocket(/\/ws$/, socket => {
    const socketIndex = sockets.push(socket) - 1
    socket.send(eventFrame('connect.challenge', {}))
    socket.onMessage(raw => {
      let frame: RpcRequest
      try {
        frame = JSON.parse(String(raw)) as RpcRequest
      } catch {
        return
      }
      if (frame.type === 'ping') {
        socket.send(JSON.stringify({ type: 'pong' }))
        return
      }
      if (frame.type !== 'req') return

      const method = String(frame.method || '')
      const key = String(frame.params?.key || frame.params?.sessionKey || '')
      wire.push({ key, method, socketIndex })

      if (method === 'connect') {
        socket.send(helloOkResponse({
          server: { version: 'e2e', conn_id: 'workspace-switch-conn' },
          features: {
            methods: [
              'sessions.messages.subscribe',
              'sessions.messages.unsubscribe',
              'sessions.messages.snapshot',
              'sessions.messages.hydrate',
              'sessions.routing.get',
              'sessions.routing.set',
              'workspaces.list',
              'artifacts.list',
              'config.patch.safe',
            ],
            events: ['session.event.text_delta'],
          },
          auth: {
            principal: { isOwner: true, authState: 'authenticated' },
            runModePolicy: { allowedRunModes: ['safe', 'full'], defaultRunMode: 'full' },
          },
        }))
        return
      }
      if (method === 'sessions.list') {
        socket.send(response(frame.id, {
          sessions: [
            session(SESSION_B, 'Workspace B task', WORKSPACE_B, '/fixtures/workspace-b', 200),
            session(SESSION_A, 'Workspace A task', WORKSPACE_A, '/fixtures/workspace-a', 100),
          ],
          count: 2,
          ts: 1_800_000_000,
          has_more: false,
        }))
        return
      }
      if (method === 'workspaces.list') {
        socket.send(response(frame.id, {
          workspaces: [
            workspace(WORKSPACE_A, 'Workspace A', '/fixtures/workspace-a'),
            workspace(WORKSPACE_B, 'Workspace B', '/fixtures/workspace-b'),
          ],
        }))
        return
      }
      if (method === 'artifacts.list') {
        socket.send(response(frame.id, { artifacts: [], has_more: false }))
        return
      }
      if (method === 'chat.history') {
        socket.send(response(frame.id, chatHistoryPayload([{
            role: 'user',
            text: key === SESSION_A ? 'Workspace A history' : 'Workspace B history',
            message_id: key === SESSION_A ? 'history-a' : 'history-b',
            timestamp: '2026-08-26T00:00:00.000Z',
          }])))
        return
      }
      if (method === 'sessions.messages.subscribe' && key === SESSION_A) {
        socket.send(response(frame.id, subscriptionPayload(key)))
        return
      }
      if (
        method === 'sessions.messages.subscribe'
        || method === 'sessions.messages.unsubscribe'
      ) {
        held.set(`${method}:${key}`, { frame, socket })
        if (method === 'sessions.messages.subscribe' && key === SESSION_B) {
          const pendingRelease = held.get(
            `sessions.messages.unsubscribe:${SESSION_A}`,
          )
          if (pendingRelease) {
            bArrivedBeforeUnsubscribeAck = true
            pendingRelease.socket.send(response(
              pendingRelease.frame.id,
              null,
            ))
            held.delete(`sessions.messages.unsubscribe:${SESSION_A}`)
          }
        }
        return
      }
      if (method === 'sessions.messages.snapshot') {
        socket.send(response(frame.id, snapshotPayload(key)))
        return
      }
      if (method === 'sessions.messages.hydrate') {
        socket.send(response(frame.id, sessionMessagesHydratePayload(
          key,
          subscriptionPayload(key),
        )))
        return
      }
      if (method === 'sessions.routing.get') {
        socket.send(response(frame.id, {
          key,
          mode: 'direct',
          revision: 0,
        }))
        return
      }
      if (method === 'chat.send') {
        sends.push({ ...(frame.params || {}) })
        socket.send(response(frame.id, {
          ok: true,
          sessionKey: key,
          status: 'accepted',
          task_id: 'workspace-b-send',
          message_id: 'workspace-b-message',
        }))
        return
      }
      socket.send(response(frame.id, basePayload(method)))
    })
  })

  const release = (method: string, key: string, payload: unknown) => {
    const request = held.get(`${method}:${key}`)
    if (!request) throw new Error(`Missing held request ${method}:${key}`)
    request.socket.send(response(request.frame.id, payload))
    held.delete(`${method}:${key}`)
  }

  await page.goto(`${CONTROL_URL}chat?session=${encodeURIComponent(SESSION_A)}`)
  await expect.poll(() => sockets.length).toBe(1)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.messages.subscribe' && entry.key === SESSION_A
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.messages.snapshot' && entry.key === SESSION_A
  ))).toBe(true)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()

  // The live-recovery notice intentionally stays quiet for five seconds.
  // Control that interval instead of racing the default five-second expect.
  await page.clock.install()
  const workspaceBRow = page.locator(`[data-session-key="${SESSION_B}"]`)
  await expect(workspaceBRow).toBeVisible()
  await workspaceBRow.locator('.sidebar-history-item').click()
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).toBe(SESSION_B)

  await expect.poll(() => held.has(`sessions.messages.subscribe:${SESSION_B}`)).toBe(true)
  expect(bArrivedBeforeUnsubscribeAck).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.messages.snapshot' && entry.key === SESSION_B
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'artifacts.list' && entry.key === SESSION_B
  ))).toBe(true)
  await expect.poll(() => wire.some(entry => (
    entry.method === 'sessions.routing.get' && entry.key === SESSION_B
  ))).toBe(true)
  expect(sockets).toHaveLength(1)

  const bSubscribeIndex = wire.findIndex(entry => (
    entry.method === 'sessions.messages.subscribe' && entry.key === SESSION_B
  ))
  const bSnapshotIndex = wire.findIndex(entry => (
    entry.method === 'sessions.messages.snapshot' && entry.key === SESSION_B
  ))
  for (const optionalMethod of [
    'sessions.routing.get',
    'artifacts.list',
  ]) {
    const optionalIndex = wire.findIndex(entry => (
      entry.method === optionalMethod && entry.key === SESSION_B
    ))
    expect(optionalIndex).toBeGreaterThan(bSubscribeIndex)
    expect(optionalIndex).toBeGreaterThan(bSnapshotIndex)
  }

  const criticalWire = wire
    .filter(entry => [
      'sessions.messages.subscribe',
      'sessions.messages.snapshot',
      'sessions.messages.unsubscribe',
    ].includes(entry.method))
    .map(entry => `${entry.method}:${entry.key}`)
  expect(criticalWire.slice(0, 5)).toEqual([
    `sessions.messages.subscribe:${SESSION_A}`,
    `sessions.messages.snapshot:${SESSION_A}`,
    `sessions.messages.unsubscribe:${SESSION_A}`,
    `sessions.messages.subscribe:${SESSION_B}`,
    `sessions.messages.snapshot:${SESSION_B}`,
  ])
  expect(wire.every(entry => entry.socketIndex === 0)).toBe(true)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()

  const liveRecovery = page.locator(
    '[data-testid="chat-session-recovery-status"][data-recovery-state="live-connecting"]',
  )
  await expect(liveRecovery).toHaveCount(0)
  await page.clock.fastForward(5_001)
  await expect(liveRecovery).toContainText(
    'Gateway connected. Restoring live updates for this session…',
  )
  await expect(liveRecovery).toHaveAttribute('role', 'status')
  await expect(liveRecovery.getByRole('button')).toHaveCount(0)

  const composer = page.locator('.chat-textarea')
  const sendButton = page.locator('.chat-send-btn.btn--primary')
  await composer.fill(PRESERVED_DRAFT)
  await expectComposerValue(composer, PRESERVED_DRAFT)
  await expect(sendButton).toBeDisabled()

  sockets[0]!.send(eventFrame('session.event.text_delta', {
    key: SESSION_A,
    task_id: 'late-workspace-a-task',
    stream_generation: 'workspace-switch-generation',
    stream_seq: 11,
    text: STALE_A_TEXT,
  }))
  await expect(page.getByText(STALE_A_TEXT, { exact: true })).toHaveCount(0)

  await expect(liveRecovery).toBeVisible()
  await expectComposerValue(composer, PRESERVED_DRAFT)

  release('sessions.messages.subscribe', SESSION_B, subscriptionPayload(SESSION_B))

  await expect(liveRecovery).toHaveCount(0)
  await expect(page.locator('.topbar[data-connection-state="connected"]')).toBeVisible()
  await expectComposerValue(composer, PRESERVED_DRAFT)
  await expect(sendButton).toBeEnabled()
  expect(sockets).toHaveLength(1)

  await sendButton.click()
  await expect.poll(() => sends.length).toBe(1)
  expect(sends[0]).toMatchObject({
    message: PRESERVED_DRAFT,
    sessionKey: SESSION_B,
  })
  sockets[0]!.send(eventFrame('session.event.text_delta', {
    key: SESSION_B,
    task_id: 'workspace-b-send',
    stream_generation: 'workspace-switch-generation',
    stream_seq: 21,
    text: FRESH_B_TEXT,
  }))
  await expect(page.getByText(FRESH_B_TEXT, { exact: true })).toBeVisible()
  await expect(page.getByText(STALE_A_TEXT, { exact: true })).toHaveCount(0)
})
