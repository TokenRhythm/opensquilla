// SquillaWatch — observe the OpenSquilla gateway and drive the pet core.
//
// Connects to the gateway WebSocket, backfills existing sessions via
// `sessions.list`, subscribes to `sessions.changed` (coarse status) plus
// per-session message streams (`session.event.*` — tool calls, text deltas,
// done/error), and forwards approval lifecycle events. It translates every
// frame into normalized `core.updateSession` / `seedSession` calls, the same
// way OpenSquilla pet's Codex watcher fed the shared state machine.

import { GatewayRpc } from './rpc.js'
import { PetCore } from './core.js'
import { detectEmotion } from './emotion.js'

const MAX_MESSAGE_SUBS = 8
const LIVE_WINDOW_MS = 15 * 60 * 1000

export interface SquillaWatchDeps {
  core: PetCore
  /** approval lifecycle: kind ∈ requested|updated|resolved, payload is the WS payload */
  onApproval: (kind: string, payload: Record<string, unknown>) => void
  /** token/cost deltas from a `done` event, for metering */
  onDoneUsage: (info: { inputTokens: number; outputTokens: number; reasoningTokens: number; cachedTokens: number; costUsd: number; model: string }) => void
  /** an assistant turn just finished — used for the "come nudge the user" behavior */
  onSessionDone?: (info: { sessionKey: string; text: string; model?: string }) => void
  gatewayUrl: string
  token?: string
  version: string
}

interface SubState { key: string; updatedAt: number }

export function createSquillaWatch(deps: SquillaWatchDeps) {
  const { core, gatewayUrl, token, version } = deps
  const url = gatewayUrl.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws'

  let rpc: GatewayRpc | null = null
  let stopped = false
  let reconnectSubscribed = false
  let reconcileTimer: ReturnType<typeof setTimeout> | null = null
  let refreshTimer: ReturnType<typeof setInterval> | null = null
  let userEmotionTimer: ReturnType<typeof setInterval> | null = null
  const subscribed = new Map<string, SubState>()

  function emitApproval(kind: string, payload: Record<string, unknown>): void {
    try { deps.onApproval(kind, payload) } catch {}
  }
  function emitDoneUsage(info: { inputTokens: number; outputTokens: number; reasoningTokens: number; cachedTokens: number; costUsd: number; model: string }): void {
    try { deps.onDoneUsage(info) } catch {}
  }

  // ── session state translation ─────────────────────────────────────────────
  function stateFromRunStatus(runStatus: string | undefined, reason: string | undefined): { state: string; event: string } | null {
    if (reason === 'created' || reason === 'forked') return { state: 'idle', event: 'SessionStart' }
    switch (runStatus) {
      case 'queued':
        return { state: 'thinking', event: 'TaskStarted' }
      case 'running':
        return { state: 'working', event: 'TaskStarted' }
      case 'interrupted':
      case 'cancelled':
        return { state: 'idle', event: 'TurnAborted' }
      case 'failed':
        return { state: 'error', event: 'StopFailure' }
      case 'timeout':
        return { state: 'error', event: 'ApiError' }
      default:
        // idle — settle without a fake completion (the message-stream `done`
        // event owns the real celebration + 💬 bubble).
        return { state: 'idle', event: 'Idle' }
    }
  }

  function onSessionsChanged(payload: Record<string, unknown>): void {
    const key = typeof payload.key === 'string' ? payload.key : ''
    if (!key) return
    // Session deleted server-side (user removed it in the shell) — drop it
    // from the pet immediately so its card disappears without waiting for a
    // syncFromList tick.
    const reason = typeof payload.reason === 'string' ? payload.reason : ''
    if (reason === 'deleted' || reason === 'removed' || reason === 'purged') {
      core.removeSession(key)
      scheduleReconcile()
      return
    }
    const mapped = stateFromRunStatus(
      typeof payload.run_status === 'string' ? payload.run_status : undefined,
      reason || undefined,
    )
    if (!mapped) return
    const fields: Record<string, unknown> = {}
    const status = payload.status
    if (status === 'done' || status === 'failed' || status === 'killed' || status === 'timeout') {
      fields.ended = true
    }
    core.updateSession(key, mapped.state, mapped.event, fields)
    scheduleReconcile()
  }

  // session.event.<kind> frames carry payload.session_key (enriched by the
  // gateway's SessionStreamRegistry).
  function onSessionEvent(name: string, payload: Record<string, unknown>, meta: Record<string, unknown>): void {
    const key = (typeof payload.session_key === 'string' && payload.session_key)
      || (typeof meta.session_key === 'string' && meta.session_key)
      || ''
    if (!key) return
    const kind = name.replace(/^session\.event\./, '')
    applyStreamEvent(key, kind, payload)
  }

  function applyStreamEvent(key: string, kind: string, payload: Record<string, unknown>): void {
    switch (kind) {
      case 'tool_use_start': {
        const tool = typeof payload.tool_name === 'string' ? payload.tool_name : ''
        core.updateSession(key, 'working', 'PreToolUse', { toolName: tool })
        break
      }
      case 'tool_result': {
        const tool = typeof payload.tool_name === 'string' ? payload.tool_name : ''
        if (payload.is_error === true) {
          core.updateSession(key, 'error', 'PostToolUseFailure', { toolName: tool, errorType: 'tool_error' })
        } else {
          core.updateSession(key, 'working', 'PostToolUse', { toolName: tool })
        }
        break
      }
      case 'thinking': {
        core.updateSession(key, 'thinking', 'Reasoning')
        break
      }
      case 'text_delta': {
        // Keep the session busy while streaming; presentation 'answer' is the
        // final reply, accumulated into the session for the `done` bubble.
        if (typeof payload.text === 'string' && payload.presentation === 'answer') {
          const s = core.getSession(key)
          if (s) {
            const prev = typeof s.textBuffer === 'string' ? s.textBuffer : ''
            s.textBuffer = prev + payload.text
          }
        }
        core.updateSession(key, 'working', 'TextStream')
        break
      }
      case 'done': {
        const text = typeof payload.text === 'string' ? payload.text : ''
        const s = core.getSession(key)
        const buffered = s && typeof s.textBuffer === 'string' ? s.textBuffer : ''
        const finalText = text || buffered
        if (s) delete s.textBuffer
        const emo = detectEmotion(finalText, 'assistant') || undefined
        core.updateSession(key, 'idle', 'Stop', {
          assistantLastOutput: finalText || undefined,
          assistantEmotion: emo,
          model: typeof payload.model === 'string' ? payload.model : undefined,
        })
        emitDoneUsage({
          inputTokens: toNum(payload.input_tokens),
          outputTokens: toNum(payload.output_tokens),
          reasoningTokens: toNum(payload.reasoning_tokens),
          cachedTokens: toNum(payload.cached_tokens),
          costUsd: toNum(payload.cost_usd),
          model: typeof payload.model === 'string' ? payload.model : '',
        })
        try {
          deps.onSessionDone?.({
            sessionKey: key,
            text: finalText || '',
            model: typeof payload.model === 'string' ? payload.model : undefined,
          })
        } catch {}
        break
      }
      case 'error': {
        const code = typeof payload.code === 'string' ? payload.code : 'api_error'
        core.updateSession(key, 'error', 'ApiError', { errorType: code })
        break
      }
      case 'compaction':
        core.updateSession(key, 'sweeping', 'PreCompact')
        break
      case 'run_heartbeat': {
        const s = core.getSession(key)
        core.updateSession(key, s && s.state !== 'idle' ? (s.state as string) : 'working', 'Heartbeat')
        break
      }
      default:
        break
    }
    scheduleReconcile()
  }

  function toNum(v: unknown): number {
    const n = Number(v)
    return Number.isFinite(n) && n > 0 ? n : 0
  }

  // ── backfill ──────────────────────────────────────────────────────────────
  async function backfill(): Promise<void> {
    if (!rpc) return
    try {
      const res = await rpc.call<any>('sessions.list', { limit: 100 })
      const list = Array.isArray(res && res.sessions) ? res.sessions : []
      for (const row of list) {
        const key = typeof row.key === 'string' ? row.key : ''
        if (!key) continue
        const fields: Record<string, unknown> = {
          agentId: 'squilla',
          model: row.model ?? undefined,
          sessionTitle: row.displayName || row.subject || row.display_name || undefined,
          updatedAt: row.updatedAt || row.updated_at || undefined,
          status: row.status || undefined,
        }
        core.seedSession({ id: key, ...fields })
      }
    } catch (err) {
      // backfill failure is non-fatal; sessions.changed will catch up
    }
  }

  // Poll sessions.list to catch sessions that never emit sessions.changed
  // (e.g. `sessions.create` does not broadcast "created"), and mark terminal
  // sessions as ended so stale cleanup can retire them.
  async function syncFromList(): Promise<void> {
    if (!rpc) return
    try {
      // Big enough to fit any reasonable session list — we use the full page
      // to decide which local sessions no longer exist (retainSessions).
      const res = await rpc.call<any>('sessions.list', { limit: 500 })
      const list = Array.isArray(res && res.sessions) ? res.sessions : []
      const seenKeys = new Set<string>()
      for (const row of list) {
        const key = typeof row.key === 'string' ? row.key : ''
        if (!key) continue
        seenKeys.add(key)
        const status = typeof row.status === 'string' ? row.status : ''
        const existing = core.getSession(key)
        if (!existing) {
          core.seedSession({
            id: key,
            agentId: 'squilla',
            model: row.model ?? undefined,
            sessionTitle: row.displayName || row.subject || row.display_name || undefined,
            updatedAt: row.updatedAt || row.updated_at || undefined,
            status: status || undefined,
          })
        } else if (status === 'done' || status === 'failed' || status === 'killed' || status === 'timeout') {
          if (!existing.ended) core.updateSession(key, existing.state, 'SessionEnd', {})
        }
      }
      // Drop sessions that no longer exist server-side (user deleted them from
      // the shell). We only trust this when the RPC actually returned a page:
      // if the network hiccuped and list is empty we don't wipe local state.
      if (list.length > 0) {
        core.retainSessions(seenKeys)
      }
    } catch {
      // poll failure is non-fatal
    }
  }

  // ── per-session message subscriptions ─────────────────────────────────────
  function liveSessions(): Array<{ key: string; updatedAt: number }> {
    const snap = core.buildSnapshot()
    const now = Date.now()
    const out: Array<{ key: string; updatedAt: number }> = []
    for (const s of snap.sessions) {
      if (s.headless) continue
      if (s.state === 'sleeping') continue
      const live = (now - s.updatedAt) < LIVE_WINDOW_MS || s.state === 'working' || s.state === 'thinking'
      if (live) out.push({ key: s.id, updatedAt: s.updatedAt })
    }
    out.sort((a, b) => b.updatedAt - a.updatedAt)
    return out.slice(0, MAX_MESSAGE_SUBS)
  }

  function scheduleReconcile(): void {
    if (reconcileTimer || !rpc || !rpc.isConnected) return
    reconcileTimer = setTimeout(() => {
      reconcileTimer = null
      void reconcile()
    }, 1200)
  }

  async function reconcile(): Promise<void> {
    if (!rpc || !rpc.isConnected) return
    const want = new Map(liveSessions().map((s) => [s.key, s]))
    // subscribe new
    for (const [key, info] of want) {
      if (subscribed.has(key)) {
        subscribed.set(key, { ...subscribed.get(key)!, updatedAt: info.updatedAt })
        continue
      }
      subscribed.set(key, { key, updatedAt: info.updatedAt })
      rpc.call('sessions.messages.subscribe', { key, sinceStreamSeq: 0 }).catch(() => {
        subscribed.delete(key)
      })
    }
    // unsubscribe stale
    for (const [key] of subscribed) {
      if (!want.has(key)) {
        subscribed.delete(key)
        rpc.call('sessions.messages.unsubscribe', { key }).catch(() => {})
      }
    }
  }

  // ── user-message emotion poll ─────────────────────────────────────────────
  // The gateway only streams agent events; the user's own prompt is not one of
  // them. Poll the active session's last user message via chat.history and, when
  // a fresh user message carries an emotion (e.g. "代码有点臭" → chou), fire a
  // user-turn event so the pet reacts immediately.
  const lastUserMsg = new Map<string, string>()
  async function pollUserEmotion(): Promise<void> {
    if (!rpc || !rpc.isConnected) return
    const snap = core.buildSnapshot()
    let activeKey: string | null = null
    let activeUpdated = -1
    for (const s of snap.sessions) {
      if (s.headless || s.state === 'sleeping') continue
      if (s.updatedAt > activeUpdated) { activeKey = s.id; activeUpdated = s.updatedAt }
    }
    if (!activeKey) return
    try {
      const res = await rpc.call<any>('chat.history', { sessionKey: activeKey, limit: 3 })
      const entries = Array.isArray(res && (res.entries || res.messages)) ? (res.entries || res.messages) : []
      for (let i = entries.length - 1; i >= 0; i--) {
        const e = entries[i]
        if (!e || typeof e !== 'object') continue
        const role = String(e.role || e.type || '')
        if (role !== 'user' && role !== 'human') continue
        const content = typeof e.content === 'string' ? e.content : ''
        if (!content.trim()) continue
        const mid = String(e.id || e.message_id || e.messageId || (e.created_at || '') + i)
        if (lastUserMsg.get(activeKey) === mid) return
        lastUserMsg.set(activeKey, mid)
        const emo = detectEmotion(content, 'user')
        if (emo) core.updateSession(activeKey, 'thinking', 'UserPromptSubmit', { userEmotion: emo })
        return
      }
    } catch {
      // poll failure is non-fatal
    }
  }

  // ── lifecycle ─────────────────────────────────────────────────────────────
  async function start(): Promise<void> {
    if (stopped || rpc) return
    rpc = new GatewayRpc({
      url,
      token,
      role: 'operator',
      client: {
        id: 'opensquilla-pet',
        display_name: 'OpenSquilla Pet',
        version,
        platform: process.platform,
        mode: 'desktop',
      },
      onReconnect: () => {
        reconnectSubscribed = true
        void backfill()
        subscribeAll()
        void reconcile()
      },
    })
    rpc.on('sessions.changed', onSessionsChanged)
    rpc.on('exec.approval.requested', (p) => emitApproval('requested', p))
    rpc.on('exec.approval.updated', (p) => emitApproval('updated', p))
    rpc.on('exec.approval.resolved', (p) => emitApproval('resolved', p))

    // any session.event.<kind>
    rpc.on('session.event.tool_use_start', (p, m) => onSessionEvent('session.event.tool_use_start', p, m))
    rpc.on('session.event.tool_result', (p, m) => onSessionEvent('session.event.tool_result', p, m))
    rpc.on('session.event.tool_use_delta', (p, m) => onSessionEvent('session.event.tool_use_delta', p, m))
    rpc.on('session.event.thinking', (p, m) => onSessionEvent('session.event.thinking', p, m))
    rpc.on('session.event.text_delta', (p, m) => onSessionEvent('session.event.text_delta', p, m))
    rpc.on('session.event.done', (p, m) => onSessionEvent('session.event.done', p, m))
    rpc.on('session.event.error', (p, m) => onSessionEvent('session.event.error', p, m))
    rpc.on('session.event.compaction', (p, m) => onSessionEvent('session.event.compaction', p, m))
    rpc.on('session.event.run_heartbeat', (p, m) => onSessionEvent('session.event.run_heartbeat', p, m))
    rpc.on('session.event.state_change', (p, m) => onSessionEvent('session.event.state_change', p, m))

    await rpc.connect()
    subscribeAll()
    await backfill()
    await reconcile()
    void syncFromList()
    refreshTimer = setInterval(() => { void syncFromList(); void reconcile() }, 15000)
    if (refreshTimer.unref) refreshTimer.unref()
    userEmotionTimer = setInterval(() => { void pollUserEmotion() }, 6000)
    if (userEmotionTimer.unref) userEmotionTimer.unref()
  }

  function subscribeAll(): void {
    if (!rpc || !rpc.isConnected) return
    rpc.call('sessions.subscribe', {}).catch(() => {})
  }

  function stop(): void {
    stopped = true
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
    if (userEmotionTimer) { clearInterval(userEmotionTimer); userEmotionTimer = null }
    if (reconcileTimer) { clearTimeout(reconcileTimer); reconcileTimer = null }
    if (rpc) { rpc.close(); rpc = null }
  }

  function refreshNow(): void {
    void backfill()
    void syncFromList()
    void reconcile()
  }

  return { start, stop, refreshNow, get rpc() { return rpc } }
}

export type SquillaWatch = ReturnType<typeof createSquillaWatch>
