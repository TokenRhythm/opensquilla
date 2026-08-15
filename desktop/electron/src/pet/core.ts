// PetCore — session store + state machine, ported from OpenSquilla pet backend/core.js.
//
// The pet's session vocabulary comes from shared/states.js. OpenSquilla drives
// sessions through seedSession (backfill) + updateSession (live events) the same
// way OpenSquilla pet's Codex watcher did; there is no transcript polling here — the
// gateway is the authority on session state.

import { PetSession, PetSnapshot, CoreState } from './types.js'

const STATE_PRIORITY: Record<string, number> = {
  error: 8,
  notification: 7,
  sweeping: 6,
  attention: 5,
  carrying: 4,
  juggling: 4,
  working: 3,
  thinking: 2,
  idle: 1,
  roam: 1,
  sleeping: 0,
}

const ONESHOT_TTL_MS: Record<string, number> = {
  attention: 15000,
  carrying: 15000,
  sweeping: 20000,
  error: 45000,
}

const BUSY_STATES = new Set<string>(['working', 'thinking', 'juggling', 'carrying', 'sweeping'])
const VALID_STATES = new Set<string>(Object.keys(STATE_PRIORITY))

const DONE_EVENTS = new Set<string>(['Stop'])
const WORK_START_EVENTS = new Set<string>(['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'SubagentStart', 'TaskStarted'])

const RECENT_EVENT_LIMIT = 8
const WORKING_STALE_MS = 5 * 60 * 1000
const DETACHED_REMOVE_MS = 30 * 1000
const SESSION_STALE_MS = 30 * 60 * 1000

interface RecentEvent { at: number; event: string | null; state: string }

function pushRecentEvent(session: InternalSession, state: string, event: string | null, now: number): RecentEvent[] {
  const prev = (session.recentEvents || []).slice(-(RECENT_EVENT_LIMIT - 1))
  prev.push({ at: now, event: event || null, state: state || 'idle' })
  return prev
}

interface InternalSession {
  id: string
  createdAt: number
  updatedAt: number
  state: string
  recentEvents: RecentEvent[]
  ended?: boolean
  headless?: boolean
  sessionRole?: string
  travelAgent?: string
  requiresCompletionAck?: boolean
  agentId?: string
  cwd?: string
  model?: string
  sessionTitle?: string
  sourcePid?: number
  contextUsage?: { used: number; limit?: number; percent?: number } | null
  assistantLastOutput?: string | null
  assistantLastOutputTruncated?: boolean
  lastEvent?: { rawEvent: string | null; at: number } | null
  lastEventTool?: string | null
  [key: string]: unknown
}

export function deriveBadge(s: InternalSession | null | undefined): string {
  if (!s) return 'idle'
  const events = Array.isArray(s.recentEvents) ? s.recentEvents : []
  const latest = events.length ? events[events.length - 1] : null
  const ev = latest && latest.event
  if (ev === 'StopFailure' || ev === 'PostToolUseFailure' || ev === 'ApiError' || ev === 'TurnAborted') return 'interrupted'
  if (s.state !== 'idle' && s.state !== 'sleeping') return 'running'
  if (s.state === 'sleeping') return 'idle'
  if (s.requiresCompletionAck === true) return 'done'
  return 'idle'
}

export interface PetCoreOptions {
  onActivity?: (act: {
    session: InternalSession
    event: string | null
    prevState: string
    newState: string
    isNew: boolean
    realCompletion: boolean
    assistantChanged: boolean
  }) => void
  onDirty?: () => void
}

export function createPetCore(options: PetCoreOptions = {}) {
  const onActivity = typeof options.onActivity === 'function' ? options.onActivity : () => {}
  const onDirty = typeof options.onDirty === 'function' ? options.onDirty : () => {}

  const sessions = new Map<string, InternalSession>()
  let cleanupTimer: ReturnType<typeof setInterval> | null = null

  function setField(s: InternalSession, key: string, value: unknown): void {
    if (value === undefined || value === null) return
    s[key] = value
  }

  function updateSession(sid: string, incomingState: string, event: string | null, f: Record<string, unknown> = {}): InternalSession {
    const id = sid || 'default'
    const now = Date.now()
    const prev = sessions.get(id)
    const isNew = !prev
    const s: InternalSession = prev || { id, createdAt: now, updatedAt: now, state: 'idle', recentEvents: [] }
    const prevState = s.state

    setField(s, 'agentId', f.agentId)
    setField(s, 'cwd', f.cwd)
    setField(s, 'sourcePid', f.sourcePid)
    setField(s, 'model', f.model)
    setField(s, 'sessionRole', f.sessionRole)
    setField(s, 'travelAgent', f.travelAgent)
    if (typeof f.headless === 'boolean') s.headless = f.headless
    if (f.sessionTitle != null) s.sessionTitle = f.sessionTitle as string
    if (f.contextUsage) s.contextUsage = f.contextUsage as { used: number; limit?: number; percent?: number }
    if (f.errorType) s.errorType = f.errorType
    if (f.preserveState === true && prev) s.state = prevState

    let assistantChanged = false
    if (typeof f.assistantLastOutput === 'string' && f.assistantLastOutput) {
      if (s.assistantLastOutput !== f.assistantLastOutput) assistantChanged = true
      s.assistantLastOutput = f.assistantLastOutput
      s.assistantLastOutputTruncated = f.assistantLastOutputTruncated === true
    }

    let resolvedState = VALID_STATES.has(incomingState) ? incomingState : 'idle'
    let realCompletion = false

    if (event === 'Stop') {
      resolvedState = 'idle'
      realCompletion = true
      s.requiresCompletionAck = true
    }

    if (event === 'SessionEnd' && f.externalResume !== true) s.ended = true
    else if (WORK_START_EVENTS.has(event || '') || event === 'SessionStart') s.ended = false

    s.state = resolvedState
    s.lastEvent = { rawEvent: event || null, at: now }
    if (f.toolName) s.lastEventTool = f.toolName as string
    s.recentEvents = pushRecentEvent(s, resolvedState, event, now)
    s.updatedAt = now

    if (event && WORK_START_EVENTS.has(event)) s.requiresCompletionAck = false

    sessions.set(id, s)

    try {
      onActivity({ session: s, event: event || null, prevState, newState: resolvedState, isNew, realCompletion, assistantChanged })
    } catch {}
    onDirty()
    return s
  }

  function seedSession(fields: Record<string, unknown> & { id: string }): InternalSession | null {
    if (!fields || !fields.id || sessions.has(fields.id)) return null
    const now = Date.now()
    const s: InternalSession = { state: 'idle', recentEvents: [], createdAt: now, updatedAt: now, ...fields }
    sessions.set(s.id, s)
    onDirty()
    return s
  }

  function setContextUsage(sid: string, cu: { used: number; limit?: number; percent?: number } | null): void {
    const s = sessions.get(sid)
    if (!s || !cu) return
    s.contextUsage = cu
    onDirty()
  }

  function ackCompletion(sid: string): boolean {
    const s = sessions.get(sid)
    if (!s || !s.requiresCompletionAck) return false
    s.requiresCompletionAck = false
    onDirty()
    return true
  }

  function getSession(sid: string): InternalSession | null {
    return sessions.get(sid) || null
  }

  function toEntry(s: InternalSession): PetSession {
    const now = Date.now()
    return {
      id: s.id,
      agentId: (s.agentId as string) || 'squilla',
      state: (s.state as CoreState) || 'idle',
      badge: deriveBadge(s),
      cwd: (s.cwd as string) || '',
      headless: !!s.headless,
      sessionTitle: (s.sessionTitle as string) || null,
      model: (s.model as string) || null,
      sessionRole: (s.sessionRole as string) || null,
      travelAgent: (s.travelAgent as string) || null,
      contextUsage: s.contextUsage || null,
      assistantLastOutput: typeof s.assistantLastOutput === 'string' ? s.assistantLastOutput : null,
      assistantLastOutputTruncated: !!s.assistantLastOutputTruncated,
      requiresCompletionAck: !!s.requiresCompletionAck,
      lastEvent: s.lastEvent || null,
      lastEventTool: s.lastEventTool || null,
      updatedAt: s.updatedAt || 0,
      idleMs: Math.max(0, now - (s.updatedAt || now)),
      sourcePid: (s.sourcePid as number) || null,
    }
  }

  function buildSnapshot(): PetSnapshot {
    const list = [...sessions.values()]
    const entries = list.map(toEntry)
    let active: PetSnapshot['active'] = null
    for (const e of entries) {
      if (e.headless) continue
      if (!active || e.updatedAt > active.lastActivity) active = { sessionId: e.id, project: e.cwd, model: e.model, lastActivity: e.updatedAt }
    }
    return {
      sessions: entries,
      active,
      idleMs: active ? (list.find((e) => e.id === active.sessionId)?.updatedAt != null ? Math.max(0, Date.now() - (list.find((e) => e.id === active.sessionId)?.updatedAt || Date.now())) : null) : null,
      lastActivityTs: active ? active.lastActivity : 0,
      ts: Date.now(),
    }
  }

  function cleanStaleSessions(): void {
    let changed = false
    const now = Date.now()
    for (const [id, s] of sessions) {
      const idle = now - (s.updatedAt || now)
      const durableTravel = s.sessionRole === 'travel'

      const ttl = ONESHOT_TTL_MS[s.state]
      if (ttl && idle > ttl) { s.state = 'idle'; changed = true }

      if (s.state === 'sleeping' || s.ended) {
        if (!durableTravel && idle > SESSION_STALE_MS) { sessions.delete(id); changed = true; continue }
        if (s.state === 'sleeping') continue
      }
      if (!durableTravel && s.sourcePid && idle > DETACHED_REMOVE_MS) {
        sessions.delete(id); changed = true; continue
      }
      const busyIdle = now - (s.updatedAt || now)
      if (BUSY_STATES.has(s.state) && busyIdle > WORKING_STALE_MS) {
        s.state = 'idle'; changed = true
      }
    }
    if (changed) onDirty()
  }

  function startStaleCleanup(): void {
    if (cleanupTimer) return
    cleanupTimer = setInterval(cleanStaleSessions, 10000)
    if (cleanupTimer.unref) cleanupTimer.unref()
  }

  function stopStaleCleanup(): void {
    if (cleanupTimer) { clearInterval(cleanupTimer); cleanupTimer = null }
  }

  function removeSession(id: string): boolean {
    if (!sessions.has(id)) return false
    sessions.delete(id)
    if (options.onDirty) try { options.onDirty() } catch {}
    return true
  }

  // Drop sessions not in `keepIds` — but protect sessions the pet itself
  // created (travel, loot, meme) or any session still actively running.
  // These are invisible to the gateway's sessions.list and would otherwise
  // be silently culled every 15 seconds.
  function retainSessions(keepIds: Set<string>): boolean {
    const protectedRoles = new Set(['travel', 'loot', 'meme'])
    let changed = false
    for (const [id, s] of sessions) {
      if (keepIds.has(id)) continue
      // Never prune an active/busy session or a session the pet owns.
      if (protectedRoles.has(s.sessionRole || '')) continue
      if (BUSY_STATES.has(s.state)) continue
      if (s.state === 'sleeping') continue
      sessions.delete(id)
      changed = true
    }
    if (changed && options.onDirty) try { options.onDirty() } catch {}
    return changed
  }

  return {
    sessions,
    VALID_STATES,
    updateSession,
    seedSession,
    setContextUsage,
    ackCompletion,
    getSession,
    buildSnapshot,
    cleanStaleSessions,
    startStaleCleanup,
    stopStaleCleanup,
    removeSession,
    retainSessions,
  }
}

export type PetCore = ReturnType<typeof createPetCore>
export { STATE_PRIORITY, BUSY_STATES, DONE_EVENTS }
