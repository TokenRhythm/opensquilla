export const SESSION_NAVIGATION_DIAG_STORAGE_KEY = 'opensquilla.chat.sessionNavigationDiag'
export const SESSION_NAVIGATION_DIAG_LIMIT = 200

export interface SessionNavigationDiagStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
  removeItem: (key: string) => void
}

export interface SessionNavigationDiagEntry {
  t: number
  iso: string
  source: string
  from?: string
  to?: string
  current?: string
  routeSession?: string
  requestSession?: string
  responseSession?: string
  reason?: string
  rendererInstance?: string
  generation?: number
  connId?: string
  handoffEpoch?: number
  targetKeyHash?: string
  phase?: string
  transportPhase?: 'healthy' | 'checking' | 'suspect' | 'reconnecting'
  closeCode?: number
  wasClean?: boolean
  reconnectAttempt?: number
  delayMs?: number
  recoveryMs?: number
  loopLagMs?: number
  maxLoopLagMs?: number
  at?: number
  topology?: 'loopback' | 'remote' | 'proxy/vpn' | 'unknown'
  visibility?: 'visible' | 'hidden' | 'unknown'
  health?: 'healthy' | 'suspect'
  suspectAt?: number
  lastRxAt?: number
  wakeIncidentId?: number
  wakeIncidentStartedAt?: number
  wakeIncidentDeadlineAt?: number
  wakeIncidentStatus?: 'probing' | 'suspect' | 'reconnecting' | 'recovered'
  wakeIncidentSource?: 'desktop-resume' | 'pageshow' | 'online' | 'manual'
  wakeIncidentProbeTimeoutMs?: number
  wakeSignalCount?: number
  roundTripMs?: number
  resumeSource?: 'desktop-resume'
}

export type SessionNavigationDiagData = Omit<SessionNavigationDiagEntry, 't' | 'iso' | 'source'>

let storageOverride: SessionNavigationDiagStorage | null = null
let rendererInstance = ''
let activeHandoff: { epoch: number; targetKeyHash: string } | null = null
const OPAQUE_TARGET_PATTERN = /^target-[0-9a-f]{8}$/
const SESSION_CORRELATION_FIELDS = [
  'from',
  'to',
  'current',
  'routeSession',
  'requestSession',
  'responseSession',
  'targetKeyHash',
] as const
const SAFE_DIAGNOSTIC_REASONS = new Set([
  'committed',
  'unchanged',
  'failed',
  'superseded',
  'newer_handoff',
  'current_session_changed',
  'missing_response_session',
  'same_session',
  'state_only',
  'route_replace',
  'connect_requested',
  'server_challenge',
  'authenticated',
  'connect_challenge_timeout',
  'connect_hello_timeout',
  'immediate_recovery',
  'transport_backoff',
  'socket_closed',
  'connection_replaced',
  'client_disconnect',
  'internal_retire',
  'request_timeout',
  'request_abort',
  'request_send_failure',
  'generation_consistency_recovery',
  'connection_wait_timeout',
  'connection_wait_abort',
  'connect_request_failure',
  'connect_send_failure',
  'wake_probe_send_failure',
  'wake_socket_stale',
  'wake_probe_timeout',
  'wake_incident_timeout',
  'socket_not_open',
  'probe_socket_unavailable',
  'probe_send_failure',
  'probe_failed',
  'control_unconfirmed',
  'wake_probe_unconfirmed',
  'native_resume_probe_timeout',
  'native_resume_socket_unavailable',
  'desktop_resume',
  'scheduler_lag',
  'wake_grace',
  'hello',
  'round_trip',
  'direct_send_timeout',
  'recovery_credit_timeout',
  'writer_send_failed',
  'writer_serialize_failed',
  'writer_capacity',
  'transport_resource_limit',
  'transport_recovery',
  'peer_close_reason_redacted',
  'reason_redacted',
])
const SAFE_TRANSPORT_PHASES = new Set([
  'connect_start', 'hello', 'challenge', 'first_successful_rpc', 'close',
  'watchdog_timeout', 'handshake_invalid', 'wake_incident_timeout',
  'wake_incident_start', 'wake_incident_recovered', 'retire',
  'probe_socket_unavailable', 'probe_deferred', 'probe_timeout',
  'scheduler_lag', 'reconnect_scheduled',
  'desktop_resume',
  'soft_suspect', 'soft_suspect_deferred',
])
const TRANSPORT_TIMING_FIELDS = [
  'at', 'suspectAt', 'lastRxAt', 'wakeIncidentStartedAt',
  'wakeIncidentDeadlineAt', 'roundTripMs',
] as const

function safeTransportMetrics(value: Record<string, unknown>): SessionNavigationDiagData {
  const metrics: SessionNavigationDiagData = {}
  for (const field of TRANSPORT_TIMING_FIELDS) {
    const candidate = value[field]
    if (typeof candidate === 'number' && Number.isFinite(candidate) && candidate >= 0) {
      metrics[field] = candidate
    }
  }
  for (const field of ['wakeIncidentId', 'wakeSignalCount'] as const) {
    const candidate = value[field]
    if (typeof candidate === 'number' && Number.isSafeInteger(candidate) && candidate >= 0) {
      metrics[field] = candidate
    }
  }
  if (value.topology === 'loopback' || value.topology === 'remote'
    || value.topology === 'proxy/vpn' || value.topology === 'unknown') {
    metrics.topology = value.topology
  }
  if (value.visibility === 'visible' || value.visibility === 'hidden' || value.visibility === 'unknown') {
    metrics.visibility = value.visibility
  }
  if (value.health === 'healthy' || value.health === 'suspect') metrics.health = value.health
  if (value.transportPhase === 'healthy' || value.transportPhase === 'checking'
    || value.transportPhase === 'suspect' || value.transportPhase === 'reconnecting') {
    metrics.transportPhase = value.transportPhase
  }
  if (value.wakeIncidentStatus === 'probing' || value.wakeIncidentStatus === 'suspect'
    || value.wakeIncidentStatus === 'reconnecting' || value.wakeIncidentStatus === 'recovered') {
    metrics.wakeIncidentStatus = value.wakeIncidentStatus
  }
  if (value.resumeSource === 'desktop-resume') metrics.resumeSource = value.resumeSource
  if (value.wakeIncidentSource === 'desktop-resume' || value.wakeIncidentSource === 'pageshow'
    || value.wakeIncidentSource === 'online' || value.wakeIncidentSource === 'manual') {
    metrics.wakeIncidentSource = value.wakeIncidentSource
  }
  if (typeof value.wakeIncidentProbeTimeoutMs === 'number'
    && Number.isFinite(value.wakeIncidentProbeTimeoutMs)
    && value.wakeIncidentProbeTimeoutMs >= 0) {
    metrics.wakeIncidentProbeTimeoutMs = value.wakeIncidentProbeTimeoutMs
  }
  return metrics
}

function createRendererInstance(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.()
  if (randomUuid) return randomUuid
  return `renderer-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function sessionNavigationRendererInstance(): string {
  if (!rendererInstance) rendererInstance = createRendererInstance()
  return rendererInstance
}

/**
 * Produce a renderer-local opaque correlation value without persisting the
 * original workspace path or session key. This is diagnostic redaction, not a
 * security primitive.
 */
export function sessionNavigationTargetHash(value: string): string {
  let hash = 0x811c9dc5
  const salted = `${sessionNavigationRendererInstance()}\0${value}`
  for (let index = 0; index < salted.length; index += 1) {
    hash ^= salted.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return `target-${(hash >>> 0).toString(16).padStart(8, '0')}`
}

export function beginSessionHandoffDiag(epoch: number, targetKey: string) {
  const next = {
    epoch,
    targetKeyHash: sessionNavigationTargetHash(targetKey),
  }
  if (activeHandoff && activeHandoff.epoch !== epoch) {
    recordSessionNavigationDiag('session.handoff.superseded', {
      rendererInstance: sessionNavigationRendererInstance(),
      handoffEpoch: activeHandoff.epoch,
      targetKeyHash: activeHandoff.targetKeyHash,
      reason: 'newer_handoff',
    })
  }
  activeHandoff = next
  recordSessionNavigationDiag('session.handoff.begin', {
    rendererInstance: sessionNavigationRendererInstance(),
    handoffEpoch: epoch,
    targetKeyHash: next.targetKeyHash,
  })
}

export function finishSessionHandoffDiag(
  epoch: number,
  outcome: 'committed' | 'unchanged' | 'failed' | 'superseded',
) {
  if (!activeHandoff || activeHandoff.epoch !== epoch) return
  recordSessionNavigationDiag('session.handoff.finish', {
    rendererInstance: sessionNavigationRendererInstance(),
    handoffEpoch: epoch,
    targetKeyHash: activeHandoff.targetKeyHash,
    reason: outcome,
  })
  activeHandoff = null
}

function safeTransportReason(value: unknown, phase: string): string | undefined {
  if (typeof value !== 'string' || !value) return undefined
  if (SAFE_DIAGNOSTIC_REASONS.has(value)) return value
  // Peer close text is not controlled by the renderer and could contain a
  // workspace path or session content. Preserve the phase without the text.
  return phase === 'close' ? 'peer_close_reason_redacted' : 'reason_redacted'
}

export function recordRpcTransportDiag(detail: unknown): SessionNavigationDiagEntry | null {
  if (!detail || typeof detail !== 'object') return null
  const value = detail as Record<string, unknown>
  const phase = typeof value.phase === 'string' ? value.phase : ''
  const generation = typeof value.generation === 'number' && Number.isFinite(value.generation)
    ? value.generation
    : undefined
  if (!SAFE_TRANSPORT_PHASES.has(phase) || generation === undefined) return null
  const handoff = activeHandoff
  const reason = safeTransportReason(value.reason, phase)
  return recordSessionNavigationDiag('rpc.transport', {
    rendererInstance: sessionNavigationRendererInstance(),
    generation,
    phase,
    ...safeTransportMetrics(value),
    ...(typeof value.connId === 'string' && value.connId
      ? { connId: value.connId.slice(0, 128) }
      : {}),
    ...(typeof value.code === 'number' && Number.isFinite(value.code)
      ? { closeCode: value.code }
      : {}),
    ...(typeof value.wasClean === 'boolean' ? { wasClean: value.wasClean } : {}),
    ...(typeof value.reconnectAttempt === 'number' && Number.isFinite(value.reconnectAttempt)
      ? { reconnectAttempt: value.reconnectAttempt }
      : {}),
    ...(typeof value.delay === 'number' && Number.isFinite(value.delay)
      ? { delayMs: value.delay }
      : {}),
    ...(typeof value.recoveryMs === 'number' && Number.isFinite(value.recoveryMs) && value.recoveryMs >= 0
      ? { recoveryMs: value.recoveryMs }
      : {}),
    ...(typeof value.loopLagMs === 'number' && Number.isFinite(value.loopLagMs) && value.loopLagMs >= 0
      ? { loopLagMs: value.loopLagMs }
      : {}),
    ...(typeof value.maxLoopLagMs === 'number' && Number.isFinite(value.maxLoopLagMs) && value.maxLoopLagMs >= 0
      ? { maxLoopLagMs: value.maxLoopLagMs }
      : {}),
    ...(reason ? { reason } : {}),
    ...(handoff
      ? { handoffEpoch: handoff.epoch, targetKeyHash: handoff.targetKeyHash }
      : {}),
  })
}

/** Record the native wake observation without persisting renderer payloads. */
export function recordRpcResumeDiag(detail: {
  generation: number
  resumeSource: 'desktop-resume'
}): SessionNavigationDiagEntry | null {
  if (!Number.isFinite(detail.generation)) return null
  return recordRpcTransportDiag({
    phase: 'desktop_resume',
    generation: detail.generation,
    reason: 'desktop_resume',
    resumeSource: detail.resumeSource,
  })
}

export function setSessionNavigationDiagStorageForTest(storage: SessionNavigationDiagStorage | null) {
  storageOverride = storage
}

function storage(): SessionNavigationDiagStorage | null {
  if (storageOverride) return storageOverride
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function redactSessionFields<T extends Record<string, unknown>>(data: T): {
  value: T
  changed: boolean
} {
  const value: Record<string, unknown> = { ...data }
  let changed = false
  for (const field of SESSION_CORRELATION_FIELDS) {
    const raw = value[field]
    if (typeof raw !== 'string' || !raw || OPAQUE_TARGET_PATTERN.test(raw)) continue
    value[field] = sessionNavigationTargetHash(raw)
    changed = true
  }
  if (
    typeof value.reason === 'string'
    && value.reason
    && !SAFE_DIAGNOSTIC_REASONS.has(value.reason)
  ) {
    value.reason = 'reason_redacted'
    changed = true
  }
  return { value: value as T, changed }
}

export function readSessionNavigationDiag(): SessionNavigationDiagEntry[] {
  const store = storage()
  if (!store) return []
  try {
    const raw = store.getItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    let changed = false
    const entries = parsed.flatMap(item => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        changed = true
        return []
      }
      const redacted = redactSessionFields(item as Record<string, unknown>)
      changed ||= redacted.changed
      return [redacted.value as unknown as SessionNavigationDiagEntry]
    })
    if (changed) {
      store.setItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY, JSON.stringify(entries))
    }
    return entries
  } catch {
    return []
  }
}

export function clearSessionNavigationDiag() {
  const store = storage()
  if (!store) return
  try {
    store.removeItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY)
  } catch {
    // Ignore diagnostics storage failures.
  }
}

export function recordSessionNavigationDiag(
  source: string,
  data: SessionNavigationDiagData = {},
): SessionNavigationDiagEntry | null {
  const store = storage()
  if (!store) return null
  const now = Date.now()
  const { value: redacted } = redactSessionFields(data)
  const entry: SessionNavigationDiagEntry = {
    t: now,
    iso: new Date(now).toISOString(),
    source,
    ...redacted,
  }
  try {
    const next = [entry, ...readSessionNavigationDiag()].slice(0, SESSION_NAVIGATION_DIAG_LIMIT)
    store.setItem(SESSION_NAVIGATION_DIAG_STORAGE_KEY, JSON.stringify(next))
    return entry
  } catch {
    return null
  }
}

export function installSessionNavigationDiagConsole() {
  if (typeof window === 'undefined') return
  window.OpenSquillaSessionDiag = {
    read: readSessionNavigationDiag,
    clear: clearSessionNavigationDiag,
  }
}

declare global {
  interface Window {
    OpenSquillaSessionDiag?: {
      read: () => SessionNavigationDiagEntry[]
      clear: () => void
    }
  }
}
