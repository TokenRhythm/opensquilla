import type { ProviderRecovery } from '@/types/parts'
import type { ChatMaintenanceEvent } from '@/types/chat'
import { ref, type Ref } from 'vue'
import i18n from '@/i18n'
import {
  compactionCompletedLabelCode,
  compactionSkippedProviderFailure,
  compactionFailurePresentation,
  compactionSkippedLabelCode,
  compactionSkipIsInformational,
} from '@/utils/chat/compactionStatus'

export type ChatCompactStatusTone = 'info' | 'ok' | 'warn' | 'err' | string
export type ChatCompactionPlacement = 'activity' | 'standalone'
export type ChatCompactionRequestNotice = 'unconfirmed' | 'rejected'

/** A command receipt belongs to this request, not to a backend terminal event. */
export interface ChatManualCompactionRequest {
  resolve: (payload: Record<string, unknown>) => void
  reject: (error: unknown) => ChatCompactionRequestNotice | null
  retire: () => ChatCompactionRequestNotice | null
}

export interface ChatCompactStatus {
  providerRecovery?: ProviderRecovery
  httpStatus?: number
  failureKind?: string
  visible: boolean
  message: string
  detail: string
  tone: ChatCompactStatusTone
  isBusy: boolean
  status: string
  source: string
  compactionId: string
  durability: string
  reason: string
}

export interface ShowCompactStatusOptions {
  providerRecovery?: ProviderRecovery
  httpStatus?: number
  failureKind?: string
  tone?: ChatCompactStatusTone
  detail?: string
  dismissMs?: number
  source?: string
  compactionId?: string
  durability?: string
  reason?: string
}

export interface UseChatCompactionOptions {
  sessionKey: Ref<string>
  schedulePendingDrainAfterTerminal: () => void
  popAllPendingIntoComposer: () => boolean
}

interface ChatCompactPayload extends Record<string, unknown> {
  compactionRecovery?: ProviderRecovery
  compactionFailure?: { httpStatus?: number; failureKind?: string }
  key?: string
  status?: string
  compacted?: boolean
  source?: string
  refused?: boolean
  safe_to_send?: boolean
  safeToSend?: boolean
  reason?: string
  skip_reason?: string
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
  /** Presentation only; accepted or validated idempotent terminal, never a rejected event. */
  onTerminal?: (receipt: ChatMaintenanceEvent) => void
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
  reason: '',
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
  const terminalCompactionIds = new Map<string, string>()
  const placementByCompactionId = new Map<string, TrackedCompactionPlacement>()
  let manualRequest: { key: string; compactionId: string; previousCompactionId: string } | null = null
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
    if (!active) {
      activeCompactionId.value = ''
      // Reset and terminal settlement retire the command's local receipt too.
      // Its delayed callback cannot reopen the same session incarnation.
      manualRequest = null
    }
  }

  function hideCompactStatus() {
    clearDismissTimer()
    compactStatus.value = createEmptyCompactStatus()
  }

  function beginManualCompactionRequest(key = options.sessionKey.value): ChatManualCompactionRequest {
    const previousCompactionId = activeCompactionId.value
      || (compactStatus.value.source === 'manual' ? compactStatus.value.compactionId : '')
    const request = { key, compactionId: '', previousCompactionId }
    manualRequest = request
    const activeId = isCompactInFlightForCurrentSession() ? activeCompactionId.value : ''
    setCompactInFlight(true, key)
    showCompactStatus('started', i18n.global.t('chat.compact.compacting'), {
      source: 'manual', compactionId: activeId,
    })

    function finish(error: unknown, retiring = false): ChatCompactionRequestNotice | null {
      if (manualRequest !== request) return null
      manualRequest = null
      if (request.key !== options.sessionKey.value) return null
      const rejected = !retiring && !!error && typeof error === 'object'
        && (error as { accepted?: unknown }).accepted === false
      // Only a lifecycle observed during this request can confirm its result.
      // An old terminal receipt must not hide a new command's rejection.
      if (request.compactionId && terminalCompactionIds.has(request.compactionId)) {
        return rejected ? 'rejected' : null
      }
      if (request.compactionId || (isCompactInFlightForCurrentSession() && activeCompactionId.value)) {
        // Losing the command ACK cannot end an already accepted operation.
        return retiring ? null : rejected ? 'rejected' : 'unconfirmed'
      }
      if (isCompactInFlightForCurrentSession()) {
        setCompactInFlight(false)
        options.popAllPendingIntoComposer()
      }
      showCompactStatus('failed', i18n.global.t(rejected ? 'chat.compact.requestRejected' : 'chat.compact.requestUnconfirmed'), {
        source: 'manual', compactionId: '', tone: 'warn',
        reason: rejected ? 'request_rejected' : 'request_unconfirmed',
      })
      return retiring ? 'unconfirmed' : null
    }

    return {
      resolve(payload) {
        if (manualRequest !== request || request.key !== options.sessionKey.value) return
        manualRequest = null
        showCompactionToast({ ...payload, key: request.key, source: 'manual' })
      },
      reject: error => finish(error),
      retire: () => finish(undefined, true),
    }
  }

  function showCompactStatus(status: string, message: string, statusOptions: ShowCompactStatusOptions = {}) {
    clearDismissTimer()
    const previous = compactStatus.value
    const isBusy = status === 'started'
    // Lifecycle refinements may omit identity metadata, but a new optimistic
    // start must not revive the previous terminal receipt under its old id.
    const carryMetadata = previous.visible
      && !(isBusy && !previous.isBusy)
      && (statusOptions.compactionId === undefined
        || statusOptions.compactionId === previous.compactionId)
    const recovery = isBusy ? statusOptions.providerRecovery ?? (carryMetadata && previous.isBusy ? previous.providerRecovery : undefined) : undefined
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
      reason: statusOptions.reason ?? (carryMetadata ? previous.reason : ''),
      providerRecovery: recovery,
      httpStatus: status === 'failed' ? statusOptions.httpStatus : isBusy ? recovery?.httpStatus : undefined,
      failureKind: status === 'failed' ? statusOptions.failureKind : undefined,
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
    // Refusing a summary does not prove that execution cannot use a local
    // window. Only an explicit execution-safety verdict retains the queue.
    return payload.safe_to_send === false || payload.safeToSend === false
  }

  function settleCompactInFlight(payload: ChatCompactPayload = {}, settleOptions: SettleCompactOptions = {}) {
    // Automatic compaction belongs to its turn and cannot release a queued or
    // running manual maintenance operation in the same session.
    if (payload.source && String(payload.source).toLowerCase() !== 'manual') return false
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

  function rememberTerminalCompactionId(compactionId: string, status: string) {
    terminalCompactionIds.set(compactionId, status)
    if (terminalCompactionIds.size <= 256) return
    const oldest = terminalCompactionIds.keys().next().value
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
    authoritativeLive: boolean,
  ): 'accepted' | 'duplicate-terminal' | 'rejected' {
    const payloadKey = String(payload.key || '')
    if (payloadKey && payloadKey !== options.sessionKey.value) return 'rejected'
    const incomingId = payloadCompactionId(payload)
    const terminal = isCompactionTerminalStatus(status)
    const source = String(payload.source || '').toLowerCase()
    const manual = source === 'manual' || (!source && compactInFlight.value)
    // The wait:false RPC acknowledgement can race a very fast terminal event.
    // Once an operation is terminal, its delayed "started" acknowledgement
    // must not resurrect the busy indicator.
    if (!terminal && incomingId && terminalCompactionIds.has(incomingId)) return 'rejected'
    // A terminal already known before this request is not its receipt. Legacy
    // terminals can omit sequence, so sequence deduplication alone cannot
    // protect a newer optimistic command from being retired by that replay.
    if (manual && terminal && incomingId && terminalCompactionIds.has(incomingId)
      && manualRequest && manualRequest.compactionId !== incomingId) return 'rejected'
    if (manual && terminal && incomingId && activeCompactionId.value && incomingId !== activeCompactionId.value) {
      return 'rejected'
    }
    // A reconnect terminal is authoritative even while an optimistic
    // wait:false /compact is still waiting for its acknowledgement and has no
    // id yet. Session stream cursors already scope replay to this session; the
    // terminal-id cache prevents the delayed started acknowledgement from
    // resurrecting the operation afterwards.
    const sequence = toFiniteNumber(payload.sequence)
    const previous = incomingId ? lastSequenceById.get(incomingId) ?? 0 : 0
    if (terminal && incomingId && terminalCompactionIds.has(incomingId)) {
      // An exact known outcome can repair a detached presentation without
      // settling controls again. Conflicting outcomes and stale seq cannot.
      if (terminalCompactionIds.get(incomingId) !== status
        || (sequence !== null && sequence < previous)) return 'rejected'
      if (sequence !== null) lastSequenceById.set(incomingId, sequence)
      return 'duplicate-terminal'
    }
    if (incomingId && sequence !== null) {
      const restoreSnapshot = sequence === previous && authoritativeLive && !terminal
        && (!manual || !compactInFlight.value || activeCompactionId.value === incomingId)
      if (sequence < previous || (sequence === previous && !restoreSnapshot)) return 'rejected'
      lastSequenceById.set(incomingId, sequence)
    }
    if (manual && status === 'started' && incomingId) activeCompactionId.value = incomingId
    if (terminal) {
      if (incomingId) rememberTerminalCompactionId(incomingId, status)
      if (manual) activeCompactionId.value = ''
    }
    return 'accepted'
  }

  function showCompactionToast(
    payload: ChatCompactPayload,
    meta: ChatCompactMeta = {},
  ): false | ChatCompactionPlacement {
    let status = String(payload.status || '').toLowerCase()
    if (compactionSkippedProviderFailure(status, payload.reason || payload.skip_reason, payload.compactionFailure?.failureKind)) status = 'failed'
    if (!status && Object.prototype.hasOwnProperty.call(payload, 'compacted')) {
      status = payload.compacted ? 'completed' : 'skipped'
    }
    const compactionId = payloadCompactionId(payload)
    const previouslyTerminal = terminalCompactionIds.has(compactionId)
    // Legacy terminal events may omit their source. Retain the source only
    // for the same operation; never borrow it from an unrelated receipt.
    const source = String(payload.source
      || (compactionId && compactionId === compactStatus.value.compactionId ? compactStatus.value.source : '')
      || (compactInFlight.value && (!compactionId || compactionId === activeCompactionId.value) ? 'manual' : '')
      || '').toLowerCase()
    const manualFailure = source === 'manual' || !source
    // Capture the active id before a terminal event settles it. Legacy id-less
    // lifecycles still get one stable slot per source.
    const placementKey = compactionId
      || (source === 'manual' ? activeCompactionId.value : '')
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
    const acceptance = acceptCompactionEvent(payload, status, authoritativeLive)
    if (terminal && compactionId && acceptance !== 'rejected') {
      meta.onTerminal?.({
        kind: 'context_compaction', compactionId, source: source || 'manual',
        state: status === 'skipped' ? 'skipped' : status === 'stale' ? 'stale'
          : status === 'cancelled' ? 'cancelled'
          : ['failed', 'error', 'timed_out'].includes(status) ? 'failed' : 'completed',
        durability: status === 'emergency_ephemeral' ? 'request_scoped' : String(payload.durability || ''),
        reason: String(payload.reason || payload.skip_reason || payload.error_reason || ''),
        ...(typeof payload.detail === 'string' ? { detail: payload.detail } : {}),
        httpStatus: payload.compactionFailure?.httpStatus,
        failureKind: payload.compactionFailure?.failureKind,
      })
    }
    if (acceptance !== 'accepted') {
      // Snapshot ownership can arrive after the replayed terminal with the
      // same lifecycle sequence. Placement reconciliation is still new work
      // even though applying the terminal twice is not.
      return terminal && acceptance === 'duplicate-terminal' ? correctedPlacement ?? false : false
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
    const identity = {
      ...(source ? { source } : {}),
      ...(compactionId ? { compactionId } : {}),
    }
    if (source === 'manual' && compactionId && !previouslyTerminal
      && manualRequest?.key === options.sessionKey.value
      && compactionId !== manualRequest.previousCompactionId
      && (!manualRequest.compactionId || manualRequest.compactionId === compactionId)) {
      manualRequest.compactionId = compactionId
    }
    if (!terminal && source === 'manual'
      && (status === 'started' || status === 'observed' || payload.heartbeat === true)) {
      setCompactInFlight(true, String(payload.key || options.sessionKey.value), compactionId)
    }

    if (status === 'started') {
      if (inActivity) return placement
      showCompactStatus('started', i18n.global.t('chat.compact.compacting'), {
        tone: 'info',
        ...identity,
        durability: String(payload.durability || ''),
      })
      return placement
    }
    if (!terminal && (status === 'observed' || payload.heartbeat === true)) {
      if (inActivity) return placement
      const stage = String(payload.stage || payload.phase || '').trim()
      showCompactStatus('started', i18n.global.t('chat.compact.compacting'), {
        tone: 'info',
        detail: stage,
        ...identity,
        durability: String(payload.durability || ''),
        providerRecovery: payload.compactionRecovery,
      })
      return placement
    }
    if (status === 'skipped') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      const skipReason = payload.reason || payload.skip_reason || payload.error_reason || ''
      showCompactStatus('skipped', i18n.global.t(compactionSkippedLabelCode(skipReason)), {
        tone: compactionSkipIsInformational(skipReason) ? 'info' : 'warn',
        ...identity,
        reason: skipReason,
      })
      return placement
    }
    if (status === 'stale') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('stale', i18n.global.t('chat.compact.cancelled'), {
        tone: 'warn',
        ...identity,
        detail: typeof payload.detail === 'string'
          ? payload.detail
          : typeof payload.reason === 'string' ? payload.reason : '',
        dismissMs: 8000,
      })
      return placement
    }
    if (status === 'failed' || status === 'error') {
      const preservePending = compactFailureBlocksPending(payload || {})
      settleCompactInFlight(payload || {}, {
        preservePending,
        recoverPending: !preservePending,
      })
      if (inActivity) return placement
      const reason = String(payload.reason || payload.skip_reason || payload.error_reason || '')
      const failure = compactionFailurePresentation(reason)
      showCompactStatus('failed', i18n.global.t(manualFailure ? failure.title : 'chat.compact.incomplete'), {
        tone: !manualFailure ? 'info' : failure.warning ? 'warn' : 'err',
        httpStatus: payload.compactionFailure?.httpStatus,
        failureKind: payload.compactionFailure?.failureKind,
        detail: manualFailure ? i18n.global.t(failure.detail) : '',
        ...identity,
        reason,
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
      const failure = compactionFailurePresentation('timed_out')
      showCompactStatus('timed_out', i18n.global.t(manualFailure ? failure.title : 'chat.compact.incomplete'), {
        tone: manualFailure ? 'warn' : 'info',
        detail: manualFailure ? i18n.global.t(failure.detail) : '',
        ...identity,
        reason: 'compaction_deadline_exceeded',
      })
      return placement
    }
    if (status === 'cancelled') {
      settleCompactInFlight(payload || {}, { recoverPending: true })
      if (inActivity) return placement
      showCompactStatus('cancelled', i18n.global.t('chat.compact.cancelled'), {
        tone: 'warn',
        ...identity,
      })
      return placement
    }
    if (status === 'emergency_ephemeral') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('emergency_ephemeral', i18n.global.t('chat.compact.temporarilyReduced'), {
        tone: 'ok',
        ...identity,
        durability: 'request_scoped',
      })
      return placement
    }
    if (status === 'completed') {
      settleCompactInFlight(payload || {})
      if (inActivity) return placement
      showCompactStatus('completed', i18n.global.t(compactionCompletedLabelCode(payload.durability)), {
        tone: 'ok',
        ...identity,
        durability: String(payload.durability || ''),
      })
      return placement
    }
    return false
  }

  function handleStreamGenerationChange(generation: string | null, previousGeneration: string | null) {
    if (!generation || !previousGeneration || generation === previousGeneration) return
    handleGatewayRestart()
  }

  function handleGatewayRestart() {
    if (!isCompactInFlightForCurrentSession()) return
    // A replacement Gateway cannot own the old process's maintenance task.
    // It has no terminal event to replay, so release its UI owner explicitly.
    showCompactionToast({
      key: options.sessionKey.value,
      source: 'manual',
      status: 'failed',
      compaction_id: activeCompactionId.value || compactStatus.value.compactionId,
      reason: 'gateway_restarted',
    })
  }

  function cleanup() {
    clearDismissTimer()
    manualRequest = null
    activeCompactionId.value = ''
    lastSequenceById.clear()
    terminalCompactionIds.clear()
    placementByCompactionId.clear()
  }

  return {
    compactStatus,
    beginManualCompactionRequest,
    getCompactionPlacement,
    isCompactInFlightForCurrentSession,
    setCompactInFlight,
    hideCompactStatus,
    showCompactStatus,
    showCompactionToast,
    handleStreamGenerationChange,
    handleGatewayRestart,
    cleanup,
  }
}
