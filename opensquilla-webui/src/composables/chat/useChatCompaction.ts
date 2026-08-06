import { ref, type Ref } from 'vue'
import i18n from '@/i18n'

export type ChatCompactStatusTone = 'info' | 'ok' | 'warn' | 'err' | string
export type ChatCompactionPlacement = 'activity' | 'standalone'

export interface ChatCompactStatus {
  visible: boolean
  message: string
  detail: string
  tone: ChatCompactStatusTone
  isBusy: boolean
  status: string
  source: string
  compactionId: string
  durability: string
}

export interface ShowCompactStatusOptions {
  tone?: ChatCompactStatusTone
  detail?: string
  dismissMs?: number
  source?: string
  compactionId?: string
  durability?: string
}

export interface UseChatCompactionOptions {
  sessionKey: Ref<string>
  schedulePendingDrainAfterTerminal: () => void
  popAllPendingIntoComposer: () => boolean
}

interface ChatCompactPayload extends Record<string, unknown> {
  key?: string
  status?: string
  compacted?: boolean
  source?: string
  refused?: boolean
  safe_to_send?: boolean
  safeToSend?: boolean
  reason?: string
  error_reason?: string
  errorClass?: string
  error_class?: string
  error?: { reason?: string; code?: string }
  compaction_id?: unknown
  compactionId?: unknown
  sequence?: unknown
  heartbeat?: unknown
  stage?: unknown
  phase?: unknown
  durability?: unknown
  user_visible?: unknown
  userVisible?: unknown
}

interface SettleCompactOptions {
  preservePending?: boolean
  recoverPending?: boolean
}

interface ChatCompactMeta {
  replayed?: unknown
  placement?: unknown
  authoritativeLive?: unknown
}

interface TrackedCompactionPlacement {
  placement: ChatCompactionPlacement
  provisional: boolean
}

const COMPACTION_TERMINAL_STATUSES = new Set([
  'completed',
  'skipped',
  'stale',
  'failed',
  'error',
  'cancelled',
  'timed_out',
  'emergency_ephemeral',
])

function isCompactionTerminalStatus(status: string): boolean {
  return COMPACTION_TERMINAL_STATUSES.has(status)
}

const EMPTY_COMPACT_STATUS: ChatCompactStatus = {
  visible: false,
  message: '',
  detail: '',
  tone: 'info',
  isBusy: false,
  status: '',
  source: '',
  compactionId: '',
  durability: '',
}

function createEmptyCompactStatus(): ChatCompactStatus {
  return { ...EMPTY_COMPACT_STATUS }
}

function toFiniteNumber(value: unknown): number | null {
  const num = typeof value === 'number'
    ? value
    : typeof value === 'string' && value.trim() !== '' ? Number(value) : NaN
  return Number.isFinite(num) ? num : null
}

export function useChatCompaction(options: UseChatCompactionOptions) {
  const compactInFlight = ref(false)
  const compactInFlightKey = ref('')
  const activeCompactionId = ref('')
  const compactStatus = ref<ChatCompactStatus>(createEmptyCompactStatus())
  const lastSequenceById = new Map<string, number>()
  const terminalCompactionIds = new Set<string>()
  const placementByCompactionId = new Map<string, TrackedCompactionPlacement>()
  let dismissTimer: ReturnType<typeof setTimeout> | null = null

  function clearDismissTimer() {
    if (!dismissTimer) return
    clearTimeout(dismissTimer)
    dismissTimer = null
  }

  function isCompactInFlightForCurrentSession(): boolean {
    if (!compactInFlight.value) return false
    return !compactInFlightKey.value || compactInFlightKey.value === options.sessionKey.value
  }

  function setCompactInFlight(active: boolean, key = options.sessionKey.value, compactionId = '') {
    compactInFlight.value = active
    compactInFlightKey.value = active ? String(key || options.sessionKey.value || '') : ''
    if (active && compactionId) activeCompactionId.value = compactionId
    if (!active) activeCompactionId.value = ''
  }

  function hideCompactStatus() {
    clearDismissTimer()
    compactStatus.value = createEmptyCompactStatus()
  }

  function showCompactStatus(status: string, message: string, statusOptions: ShowCompactStatusOptions = {}) {
    clearDismissTimer()
    const previous = compactStatus.value
    const isBusy = status === 'started'
    // Lifecycle refinements may omit identity metadata. Keep it stable while
    // the same standalone row is being updated.
    const carryMetadata = previous.visible
    compactStatus.value = {
      visible: true,
      message,
      detail: statusOptions.detail || '',
      tone: statusOptions.tone || 'info',
      isBusy,
      status,
      source: statusOptions.source ?? (carryMetadata ? previous.source : ''),
      compactionId: statusOptions.compactionId ?? (carryMetadata ? previous.compactionId : ''),
      durability: statusOptions.durability ?? (carryMetadata ? previous.durability : ''),
    }
    if (statusOptions.dismissMs && statusOptions.dismissMs > 0) {
      dismissTimer = setTimeout(() => {
        dismissTimer = null
        hideCompactStatus()
      }, statusOptions.dismissMs)
    }
  }

  function compactFailureBlocksPending(payload: ChatCompactPayload): boolean {
    if (!payload) return false
    if (payload.refused === true || payload.safe_to_send === false || payload.safeToSend === false) return true
    const reason = String(payload.reason || payload.error_reason || payload.errorClass || payload.error_class || payload.error?.reason || payload.error?.code || '').toLowerCase()
    return ['compaction_insufficient', 'compaction_flush_failed', 'context_overflow', 'unsafe_flush_receipt'].includes(reason)
  }

  function settleCompactInFlight(payload: ChatCompactPayload = {}, settleOptions: SettleCompactOptions = {}) {
    const key = String(payload.key || compactInFlightKey.value || options.sessionKey.value || '')
    if (!compactInFlight.value || (compactInFlightKey.value && key && key !== compactInFlightKey.value)) return false
    setCompactInFlight(false)
    const status = String(payload.status || '').toLowerCase()
    const compactedFlag = Object.prototype.hasOwnProperty.call(payload, 'compacted') ? !!payload.compacted : null
    if (
      status === 'completed'
      || status === 'skipped'
      || status === 'stale'
      || status === 'emergency_ephemeral'
      || (status === '' && compactedFlag !== null)
    ) {
      options.schedulePendingDrainAfterTerminal()
    } else if (settleOptions.preservePending) {
      // Pending queue remains blocked until the user acts.
    } else if (settleOptions.recoverPending) {
      options.popAllPendingIntoComposer()
    }
    return true
  }

  function payloadCompactionId(payload: ChatCompactPayload): string {
    return String(payload.compaction_id ?? payload.compactionId ?? '').trim()
  }

  function rememberTerminalCompactionId(compactionId: string) {
    terminalCompactionIds.add(compactionId)
    if (terminalCompactionIds.size <= 256) return
    const oldest = terminalCompactionIds.values().next().value
    if (typeof oldest === 'string') {
      terminalCompactionIds.delete(oldest)
      lastSequenceById.delete(oldest)
      placementByCompactionId.delete(oldest)
    }
  }

  function resolveCompactionPlacement(
    key: string,
    requested: ChatCompactionPlacement,
    options: { provisional?: boolean } = {},
  ): TrackedCompactionPlacement {
    const existing = placementByCompactionId.get(key)
    if (existing) return existing
    const tracked = {
      placement: requested,
      provisional: options.provisional === true,
    }
    placementByCompactionId.set(key, tracked)
    return tracked
  }

  function getCompactionPlacement(compactionId: string): ChatCompactionPlacement | null {
    const id = String(compactionId || '').trim()
    return id ? placementByCompactionId.get(id)?.placement ?? null : null
  }

  function acceptCompactionEvent(
    payload: ChatCompactPayload,
    status: string,
  ): boolean {
    const payloadKey = String(payload.key || '')
    if (payloadKey && payloadKey !== options.sessionKey.value) return false
    const incomingId = payloadCompactionId(payload)
    const terminal = isCompactionTerminalStatus(status)
    // The wait:false RPC acknowledgement can race a very fast terminal event.
    // Once an operation is terminal, its delayed "started" acknowledgement
    // must not resurrect the busy indicator.
    if (!terminal && incomingId && terminalCompactionIds.has(incomingId)) return false
    if (terminal && incomingId && activeCompactionId.value && incomingId !== activeCompactionId.value) {
      return false
    }
    // A reconnect terminal is authoritative even while an optimistic
    // wait:false /compact is still waiting for its acknowledgement and has no
    // id yet. Session stream cursors already scope replay to this session; the
    // terminal-id cache prevents the delayed started acknowledgement from
    // resurrecting the operation afterwards.
    const sequence = toFiniteNumber(payload.sequence)
    if (incomingId && sequence !== null) {
      const previous = lastSequenceById.get(incomingId) ?? 0
      if (sequence <= previous) return false
      lastSequenceById.set(incomingId, sequence)
    }
    if (status === 'started' && incomingId) activeCompactionId.value = incomingId
    if (terminal) {
      if (incomingId) rememberTerminalCompactionId(incomingId)
      activeCompactionId.value = ''
    }
    return true
  }

  function showCompactionToast(
    payload: ChatCompactPayload,
    meta: ChatCompactMeta = {},
  ): false | ChatCompactionPlacement {
    let status = String(payload.status || '').toLowerCase()
    if (!status && Object.prototype.hasOwnProperty.call(payload, 'compacted')) {
      status = payload.compacted ? 'completed' : 'skipped'
    }
    const source = String(payload.source || '').toLowerCase()
    const compactionId = payloadCompactionId(payload)
    // Capture the active id before a terminal event settles it. Legacy id-less
    // lifecycles still get one stable slot per source.
    const placementKey = compactionId
      || activeCompactionId.value
      || `legacy:${source || 'automatic'}`
    const terminal = isCompactionTerminalStatus(status)
    const requestedPlacement: ChatCompactionPlacement = meta.placement === 'activity'
      ? 'activity'
      : 'standalone'
    const authoritativeLive = meta.authoritativeLive === true
    const visible = payload.user_visible ?? payload.userVisible ?? true
    const trackedBefore = placementByCompactionId.get(placementKey)
    let correctedPlacement: ChatCompactionPlacement | null = null
    if (
      trackedBefore?.provisional
      && authoritativeLive
      && (visible !== false || source === 'manual')
    ) {
      correctedPlacement = requestedPlacement === 'activity'
        ? 'activity'
        : trackedBefore.placement
      placementByCompactionId.set(placementKey, {
        placement: correctedPlacement,
        provisional: false,
      })
    }
    // Reconnect replay is authoritative for terminal state, but a replayed
    // progress event must never resurrect an already-finished busy indicator.
    // Legacy payloads without `status` remain supported via `compacted` above.
    if (meta.replayed === true && !terminal) return false
    if (!acceptCompactionEvent(payload, status)) {
      // Snapshot ownership can arrive after the replayed terminal with the
      // same lifecycle sequence. Placement reconciliation is still new work
      // even though applying the terminal twice is not.
      return terminal ? correctedPlacement ?? false : false
    }
    const trackedAfterAcceptance = placementByCompactionId.get(placementKey)
    const canCloseTrackedHiddenFeedback = terminal
      && trackedAfterAcceptance !== undefined
      && !trackedAfterAcceptance.provisional
    if (
      visible === false
      && source !== 'manual'
      && !canCloseTrackedHiddenFeedback
    ) return false

    const optimisticStandaloneOwner = source === 'manual'
      && (compactInFlight.value || compactStatus.value.visible)
    if (
      meta.replayed === true
      && terminal
      && !authoritativeLive
      && !trackedAfterAcceptance
      && !optimisticStandaloneOwner
    ) {
      // A replayed terminal proves the lifecycle ended but cannot identify a
      // UI owner. Remember a non-rendering provisional placement; a subsequent
      // authoritative live snapshot may promote it into the active Activity.
      resolveCompactionPlacement(placementKey, requestedPlacement, { provisional: true })
      return false
    }

    const tracked = trackedAfterAcceptance
      ?? resolveCompactionPlacement(placementKey, requestedPlacement)
    if (tracked.provisional) return false
    const placement = tracked.placement
    const inActivity = placement === 'activity'

    if (status === 'started') {
      if (source === 'manual') {
        setCompactInFlight(
          true,
          String(payload.key || options.sessionKey.value),
          payloadCompactionId(payload),
        )
      }
      if (inActivity) return placement
      showCompactStatus('started', i18n.global.t('chat.compact.compacting'), {
        tone: 'info',
        source,
        compactionId,
        durability: String(payload.durability || ''),
      })
      return placement
    }
    if (status === 'observed' || payload.heartbeat === true) {
      if (inActivity) return placement
      const stage = String(payload.stage || payload.phase || '').trim()
      showCompactStatus('started', i18n.global.t('chat.compact.compacting'), {
        tone: 'info',
        detail: stage,
      })
      return placement
    }
    if (status === 'skipped') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('skipped', i18n.global.t('chat.compact.withinBudget'), {
        tone: 'info',
        source,
        compactionId,
      })
      return placement
    }
    if (status === 'stale') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('stale', i18n.global.t('chat.compact.cancelled'), {
        tone: 'warn',
        detail: typeof payload.detail === 'string'
          ? payload.detail
          : typeof payload.reason === 'string' ? payload.reason : '',
        dismissMs: 8000,
      })
      return placement
    }
    if (status === 'failed' || status === 'error') {
      const preservePending = compactFailureBlocksPending(payload || {})
      settleCompactInFlight(payload || {}, { preservePending })
      if (inActivity) return placement
      showCompactStatus('failed', i18n.global.t('chat.compact.failed'), {
        tone: 'err',
        source,
        compactionId,
      })
      return placement
    }
    if (status === 'timed_out') {
      const preservePending = compactFailureBlocksPending(payload || {})
      settleCompactInFlight(payload || {}, {
        preservePending,
        recoverPending: !preservePending,
      })
      if (inActivity) return placement
      showCompactStatus('timed_out', i18n.global.t('chat.compact.failed'), {
        tone: 'warn',
        source,
        compactionId,
      })
      return placement
    }
    if (status === 'cancelled') {
      settleCompactInFlight(payload || {}, { recoverPending: true })
      if (inActivity) return placement
      showCompactStatus('cancelled', i18n.global.t('chat.compact.cancelled'), {
        tone: 'warn',
        source,
        compactionId,
      })
      return placement
    }
    if (status === 'emergency_ephemeral') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('emergency_ephemeral', i18n.global.t('chat.compact.compacted'), {
        tone: 'warn',
        detail: typeof payload.detail === 'string'
          ? payload.detail
          : i18n.global.t('chat.compact.requestScoped'),
        source,
        compactionId,
        durability: 'request_scoped',
      })
      return placement
    }
    if (status === 'completed') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('completed', i18n.global.t('chat.compact.compacted'), {
        tone: 'ok',
        source,
        compactionId,
        durability: String(payload.durability || ''),
      })
      return placement
    }
    return false
  }

  function cleanup() {
    clearDismissTimer()
    activeCompactionId.value = ''
    lastSequenceById.clear()
    terminalCompactionIds.clear()
    placementByCompactionId.clear()
  }

  return {
    compactStatus,
    getCompactionPlacement,
    isCompactInFlightForCurrentSession,
    setCompactInFlight,
    hideCompactStatus,
    showCompactStatus,
    showCompactionToast,
    cleanup,
  }
}
