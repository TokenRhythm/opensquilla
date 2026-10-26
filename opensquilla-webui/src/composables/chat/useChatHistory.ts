import { projectConversationHistoryMessage, compactionSummaryMessages, summaryCompactionIds, isHistoryMaintenance, mergeHistoryMaintenance, turnOutcomeRecord, attachHistoryTurnOutcomes, preserveAcceptedEnsembleRouterRows } from '@/utils/chat/conversationHistory'
import { nextTick, ref, watch, type Ref } from 'vue'
import type {
  ChatMaintenanceEvent,
  ChatMessage,
  ChatTurnOutcome,
} from '@/types/chat'
import {
  historyWindowsOverlap,
  reconcileClientTerminalNotices,
  reconcileHistoryWindow,
  reconcileRunningHistoryMessages,
  rehomePromotedSteerRows,
} from '@/utils/chat/historyMerge'
import { createScrollHandoffGuard } from '@/utils/chat/scrollAnchor'
import type { InitialHistoryLoadStatus } from '@/utils/chat/sessionLoadState'
import {
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  isRpcAbort,
  type SessionBootstrapPhaseContext,
  type SessionPhaseResult,
} from '@/composables/chat/sessionBootstrapContract'
import {
  SessionReadHistoryCursorError,
  SessionReadSessionMissingError,
  type SessionReadHistoryPage,
  type SessionReadLease,
  type SessionReadLeaseReader,
  type SessionReadMessage,
} from '@/modules/sessionReadLifecycle'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { dedupeTerminalErrorNotices } from '@/utils/chat/terminalErrorNotices'
import { interleaveHistoryModelCallSegments, interleaveHydratedHistoryModelCallSegments } from '@/utils/chat/historyModelCallSegments'

export interface UseChatHistoryOptions {
  /** The bootstrap owner supplies the one lease shared with live subscription. */
  sessionReadLeaseReader: SessionReadLeaseReader
  sessionKey: Ref<string>
  /** Delivery authority, not the transient socket connection state. */
  deliveryIdentity?: Readonly<Ref<string | null>>
  messages: Ref<ChatMessage[]>
  threadRef?: Ref<HTMLElement | null>
  lastHeaderRole: Ref<string>
  lastHeaderDay: Ref<string>
  preserveLiveTail?: Ref<boolean>
  autoScroll?: Ref<boolean>
  /** Invalidates deferred anchor work when the reused chat viewport changes session. */
  scrollEpoch?: Ref<number>
  /** Lets application-owned navigation take precedence over deferred viewport corrections. */
  canApplyViewportCorrection?: () => boolean
  stripTimePrefix: (text: string) => string
  scrollToBottom: () => void
  onTerminalTask?: (outcome: ChatTurnOutcome) => void
  /** A current canonical page has been installed; display-only previews may retire. */
  onHistoryInstalled?: (key: string, compactions: readonly ChatMaintenanceEvent[]) => void
  /** Display-only window size; does not restore pagination or replay cursors. */
  initialWindowSize?: () => number
}

export interface ChatHistoryState {
  hasMore: boolean
  oldestCursor: string | number | null
  newestCursor: string | number | null
  historyScope: string
  canonicalAvailable: boolean | null
  canonicalComplete: boolean | null
  loading: boolean
  loadingEarlier: boolean
  retrying: boolean
  initialLoadStatus: InitialHistoryLoadStatus
  loadEarlierError: boolean
  recoveryError: boolean
  sessionMissing: boolean
}

interface HistoryLoadParams {
  before?: string | number | null
  prepend?: boolean
  bridgeRetry?: boolean
  retry?: boolean
  nonReconnecting?: boolean
  replaceCanonicalWindow?: boolean
}

type FailedHistoryRequest =
  | {
      kind: 'page'
      key: string
      before: string | number | null
      prepend: boolean
    }
  | {
      kind: 'bridge'
      key: string
    }
  | {
      kind: 'latest'
      key: string
      error: unknown
    }

const MAX_FORWARD_BRIDGE_PAGES = 2
const BACKGROUND_HISTORY_RETRY_DELAYS_MS = [250, 1_000, 3_000] as const

type HistoryLoadResult = SessionPhaseResult & { historyContinuation?: boolean }

interface BackgroundHistoryRecovery {
  key: string
  lease: SessionReadLease | null
  expectedUserMessageIds: Set<string>
  retryCount: number
  exhausted: boolean
}

export function useChatHistory(options: UseChatHistoryOptions) {
  let historySyncTimer: ReturnType<typeof setTimeout> | null = null
  let historyRequestSeq = 0
  let preserveLocalTailGeneration = 0
  let acknowledgedPreserveLocalTailGeneration = 0
  let historySyncPending = false
  let historySyncTimerNonReconnecting = false
  let historySyncPendingNonReconnecting = false
  // Exposed read-only by convention so session hand-offs can distinguish the
  // prior session's terminal `ready` state from the new session's first load.
  const historySessionKey = ref('')
  // Capture the departed rows at the actual navigation boundary. A live
  // snapshot can populate this session before its first history read finishes
  // (or even starts); those new rows must not be discarded as cross-session.
  const departedSessionRows = new WeakSet<ChatMessage>()
  const stopObservingSessionRows = watch(options.sessionKey, () => {
    for (const message of options.messages.value) departedSessionRows.add(message)
  }, { flush: 'sync' })
  let hasLoadedEarlier = false
  let loadEarlierPending = false
  let failedHistoryRequest: FailedHistoryRequest | null = null
  let backgroundRecovery: BackgroundHistoryRecovery | null = null
  let activeHistory: {
    key: string
    lease: SessionReadLease | null
    bootstrapGeneration: number
    controller: AbortController
    promise: Promise<SessionPhaseResult | void>
  } | null = null
  const loadedEarlierCursors = new Set<string>()
  const historyState = ref<ChatHistoryState>({
    hasMore: false,
    oldestCursor: null,
    newestCursor: null,
    historyScope: '',
    canonicalAvailable: null,
    canonicalComplete: null,
    loading: false,
    loadingEarlier: false,
    retrying: false,
    initialLoadStatus: 'pending',
    loadEarlierError: false,
    recoveryError: false,
    sessionMissing: false,
  })

  function notifyTerminalTasks(data: SessionReadHistoryPage) {
    if (!options.onTerminalTask) return
    for (const raw of data.turnOutcomes) {
      const outcome = normalizeTurnOutcome(turnOutcomeRecord(raw))
      if (
        outcome?.taskId
        && ['succeeded', 'failed', 'cancelled', 'timeout', 'abandoned', 'interrupted']
          .includes(outcome.status.toLowerCase())
      ) {
        options.onTerminalTask(outcome)
      }
    }
  }

  function isCurrentRecovery(recovery: BackgroundHistoryRecovery): boolean {
    return backgroundRecovery === recovery
      && recovery.key === options.sessionKey.value
      && recovery.lease === options.sessionReadLeaseReader.current()
  }

  function armHistorySync(nonReconnecting: boolean, advanceGeneration: boolean, delayMs = 50) {
    const key = options.sessionKey.value
    const lease = options.sessionReadLeaseReader.current()
    if (nonReconnecting && advanceGeneration) preserveLocalTailGeneration += 1
    historySyncTimerNonReconnecting ||= nonReconnecting
    if (historySyncTimer) clearTimeout(historySyncTimer)
    historySyncTimer = setTimeout(() => {
      historySyncTimer = null
      const timerNonReconnecting = historySyncTimerNonReconnecting
      historySyncTimerNonReconnecting = false
      if (key !== options.sessionKey.value || lease !== options.sessionReadLeaseReader.current()) return
      const activeReadIsStale = activeHistory && (
        activeHistory.key !== key || activeHistory.lease !== lease
      )
      if ((historyState.value.loading && !activeReadIsStale) || failedHistoryRequest?.kind === 'latest') {
        historySyncPending = true
        historySyncPendingNonReconnecting ||= timerNonReconnecting
        return
      }
      void loadHistory({ nonReconnecting: timerNonReconnecting })
    }, delayMs)
  }

  function scheduleHistorySync(preserveLocalTail = false, expectedUserMessageId?: string) {
    if (preserveLocalTail) {
      const expectedId = expectedUserMessageId?.trim()
      if (!backgroundRecovery || !isCurrentRecovery(backgroundRecovery)) {
        backgroundRecovery = {
          key: options.sessionKey.value,
          lease: options.sessionReadLeaseReader.current(),
          expectedUserMessageIds: new Set(),
          retryCount: 0,
          exhausted: false,
        }
      } else if (expectedId && backgroundRecovery.expectedUserMessageIds.has(expectedId)) {
        // queued/running/input notifications for one input share one budget.
        return
      } else if (backgroundRecovery.exhausted) {
        // An explicit terminal/commit invalidation can arrive after an outage
        // exhausted input hydration. Give that new durable evidence its own
        // bounded budget; identified duplicate input events return above.
        backgroundRecovery.retryCount = 0
        backgroundRecovery.exhausted = false
      }
      if (expectedId) backgroundRecovery.expectedUserMessageIds.add(expectedId)
    }
    armHistorySync(preserveLocalTail, true)
  }

  function finishBackgroundRecovery(
    recovery: BackgroundHistoryRecovery,
    result: HistoryLoadResult | void,
    requestPreserveLocalTailGeneration: number,
  ) {
    if (!isCurrentRecovery(recovery)) return
    if (result?.cancelled) {
      backgroundRecovery = null
      if (historySyncTimer) clearTimeout(historySyncTimer)
      historySyncTimer = null
      historySyncTimerNonReconnecting = false
      historySyncPending = false
      historySyncPendingNonReconnecting = false
      return
    }
    // Forward bridging already schedules its next bounded page. Progress is
    // not a failed attempt merely because the latest input is further ahead.
    if (result?.ok && result.historyContinuation) return
    // A successful read admitted before a later terminal/input invalidation
    // cannot settle it. The pending post-invalidation read owns that proof.
    if (result?.ok && requestPreserveLocalTailGeneration < preserveLocalTailGeneration) return
    const missingInput = [...recovery.expectedUserMessageIds].some(id => (
      !options.messages.value.some(message => message.role === 'user' && message.messageId === id)
    ))
    if (result?.ok && !missingInput) {
      backgroundRecovery = null
      return
    }
    // Cursor invalidation has its own explicit canonical-window replacement.
    if (failedHistoryRequest?.kind === 'latest') return
    historySyncPending = false
    historySyncPendingNonReconnecting = false
    const delay = BACKGROUND_HISTORY_RETRY_DELAYS_MS[recovery.retryCount]
    if (delay !== undefined) {
      recovery.retryCount += 1
      armHistorySync(true, true, delay)
      return
    }
    recovery.exhausted = true
    failedHistoryRequest = { kind: 'page', key: recovery.key, before: null, prepend: false }
    historyState.value = { ...historyState.value, recoveryError: true }
  }

  function flushPendingHistorySync() {
    if (historyState.value.loading || failedHistoryRequest) return
    if (loadEarlierPending) {
      loadEarlierPending = false
      void loadEarlierHistory()
      return
    }
    if (!historySyncPending) return
    const pendingNonReconnecting = historySyncPendingNonReconnecting
    historySyncPending = false
    historySyncPendingNonReconnecting = false
    armHistorySync(pendingNonReconnecting, false)
  }

  const mapHistoryMessage = (message: SessionReadMessage, suppressed: ReadonlySet<string> = new Set()) =>
    projectConversationHistoryMessage(message, suppressed, options.stripTimePrefix)

  function messageKey(msg: ChatMessage): string {
    return msg.messageId || msg.clientId || `${msg.role}:${msg.ts || ''}:${msg.text || ''}`
  }

  function hasLocalOptimisticRows(messages: ChatMessage[]): boolean {
    return messages.some(msg => msg.restoredFromHistory !== true)
  }

  function responseCanonicalComplete(data: SessionReadHistoryPage): boolean | null {
    return data.canonicalComplete
  }

  function responseCanonicalAvailable(data: SessionReadHistoryPage): boolean | null {
    return data.canonicalAvailable
  }

  function updateHistoryState(
    data: SessionReadHistoryPage,
    prepend: boolean,
    initialLoadError = false,
  ) {
    const nextOldestCursor = data.oldestCursor
    const requestedCursor = prepend ? historyState.value.oldestCursor : null
    const cursorAdvanced = !prepend || nextOldestCursor !== requestedCursor
    const preserveLoadedBoundary = !prepend && hasLoadedEarlier
    historyState.value = {
      hasMore: preserveLoadedBoundary
        ? historyState.value.hasMore
        : data.hasMore && cursorAdvanced,
      oldestCursor: preserveLoadedBoundary ? historyState.value.oldestCursor : nextOldestCursor,
      newestCursor: prepend
        ? historyState.value.newestCursor
        : data.newestCursor,
      historyScope: data.scope,
      canonicalAvailable: responseCanonicalAvailable(data),
      canonicalComplete: responseCanonicalComplete(data),
      loading: false,
      loadingEarlier: false,
      retrying: false,
      initialLoadStatus: prepend
        ? historyState.value.initialLoadStatus
        : initialLoadError ? 'error' : 'ready',
      loadEarlierError: false,
      recoveryError: prepend ? historyState.value.recoveryError : initialLoadError,
      sessionMissing: false,
    }
  }

  function resetForSession(key: string): boolean {
    if (historySessionKey.value === key) return false
    if (backgroundRecovery?.key !== key) backgroundRecovery = null
    const crossedSession = Boolean(historySessionKey.value)
    if (crossedSession) {
      acknowledgedPreserveLocalTailGeneration = preserveLocalTailGeneration
    }
    historySessionKey.value = key
    hasLoadedEarlier = false
    loadEarlierPending = false
    failedHistoryRequest = null
    loadedEarlierCursors.clear()
    historyState.value = {
      hasMore: false,
      oldestCursor: null,
      newestCursor: null,
      historyScope: '',
      canonicalAvailable: null,
      canonicalComplete: null,
      loading: false,
      loadingEarlier: false,
      retrying: false,
      initialLoadStatus: 'pending',
      loadEarlierError: false,
      recoveryError: false,
      sessionMissing: false,
    }
    return crossedSession
  }

  function markSessionMissing(key: string): boolean {
    if (!key || key !== options.sessionKey.value) return false
    resetForSession(key)
    // The live registration can prove absence before the eager history frame
    // is admitted. Retire that read so its later rejection cannot overwrite
    // this terminal state or a successor session.
    cancelActiveHistory()
    failedHistoryRequest = null
    historyState.value = {
      ...historyState.value,
      loading: false,
      loadingEarlier: false,
      retrying: false,
      initialLoadStatus: 'ready',
      loadEarlierError: false,
      recoveryError: false,
      sessionMissing: true,
    }
    return true
  }

  function callHistory(
    lease: SessionReadLease,
    request: {
      direction: 'latest' | 'before' | 'after'
      cursor?: string
      limit: number
    },
    bootstrap: SessionBootstrapPhaseContext,
  ): Promise<SessionReadHistoryPage> {
    const deadlineAt = Math.min(bootstrap.deadlineAt, bootstrap.attemptDeadlineAt)
    const readOptions = {
      signal: bootstrap.signal,
      deadlineAt,
      budgetMs: Math.max(1, deadlineAt - Date.now()),
      limit: request.limit,
    }
    if (request.direction === 'before') {
      return lease.history.before(request.cursor || '', readOptions)
    }
    if (request.direction === 'after') {
      return lease.history.after(request.cursor || '', readOptions)
    }
    return lease.history.latest(readOptions)
  }

  async function runHistoryLoad(
    params: HistoryLoadParams = {},
    bootstrap: SessionBootstrapPhaseContext,
  ): Promise<HistoryLoadResult | void> {
    if (!options.sessionKey.value) return
    const key = options.sessionKey.value
    const lease = options.sessionReadLeaseReader.current()
    const requestIdentity = options.deliveryIdentity?.value
    const requestScrollEpoch = options.scrollEpoch?.value ?? 0
    resetForSession(key)
    const historyStateBeforeLoad = historyState.value
    const failedHistoryRequestBeforeLoad = failedHistoryRequest
    const requestPreserveLocalTailGeneration = preserveLocalTailGeneration
    const nonReconnecting = Boolean(params.nonReconnecting)
    const requestSeq = ++historyRequestSeq
    let bridgeAttempted = Boolean(params.bridgeRetry)
    const isInitialLoad = !params.prepend
      && (
        historyState.value.initialLoadStatus === 'pending'
        || historyState.value.initialLoadStatus === 'error'
      )
    if (!nonReconnecting) {
      historyState.value = {
        ...historyState.value,
        loading: true,
        // Only explicit backward pagination owns the sentinel. Forward
        // catch-up/bridge recovery is a session-recovery concern and must not
        // impersonate "load earlier" progress or failure.
        loadingEarlier: Boolean(params.prepend),
        retrying: Boolean(params.retry && !params.prepend),
        initialLoadStatus: isInitialLoad ? 'loading' : historyState.value.initialLoadStatus,
        loadEarlierError: false,
        recoveryError: params.prepend ? historyState.value.recoveryError : false,
      }
    }
    const isCurrentRequest = () => (
      key === options.sessionKey.value
      // Initial durable identity hashing runs alongside the authorized
      // transport. Its null -> ready completion does not retire this lease.
      // Once a proven identity was captured, require that exact authority.
      && (requestIdentity == null || options.deliveryIdentity?.value === requestIdentity)
      && options.sessionReadLeaseReader.current() === lease
      && requestSeq === historyRequestSeq
      && requestScrollEpoch === (options.scrollEpoch?.value ?? 0)
    )
    const restoreSilentBackgroundState = () => {
      failedHistoryRequest = failedHistoryRequestBeforeLoad
      historyState.value = historyStateBeforeLoad
    }
    try {
      if (!lease) throw new Error('No active session read lease.')
      if (!isCurrentRequest()) {
        if (requestSeq === historyRequestSeq) {
          historyState.value = {
            ...historyState.value,
            loading: false,
            loadingEarlier: false,
            retrying: false,
          }
          flushPendingHistorySync()
        }
        return { ok: false, cancelled: true }
      }
      const windowSize = Math.max(options.messages.value.length, options.initialWindowSize?.() ?? 0)
      const limit = !params.prepend && Number.isFinite(windowSize) && windowSize > 50
        ? Math.min(200, Math.floor(windowSize)) : 50
      const data = await callHistory(
        lease,
        params.before != null
          ? { direction: 'before', cursor: String(params.before), limit }
          : { direction: 'latest', limit },
        bootstrap,
      )
      if (!isCurrentRequest()) return { ok: false, cancelled: true }
      const msgs = data.messages
      const canonicalAvailable = data.canonicalAvailable
      // A draft WebChat key has no canonical store yet, but the server can
      // positively confirm its empty transcript. Reconnection must accept
      // that state without treating unavailable or already-loaded history
      // as empty, or the first message remains blocked by the live fence.
      const confirmedEmptyDraft = data.canonicalComplete === true
        && msgs.length === 0
        && !data.hasMore
        && !params.prepend
        && !hasLoadedEarlier
        && options.messages.value.length === 0
      if (canonicalAvailable === false && !confirmedEmptyDraft) {
        if (nonReconnecting) {
          restoreSilentBackgroundState()
          return { ok: false }
        }
        failedHistoryRequest = hasLoadedEarlier && !params.prepend
          ? { kind: 'bridge', key }
          : {
              kind: 'page',
              key,
              before: params.before ?? null,
              prepend: Boolean(params.prepend),
            }
        if (params.prepend || hasLoadedEarlier) {
          historyState.value = {
            ...historyState.value,
            canonicalAvailable: false,
            canonicalComplete: responseCanonicalComplete(data),
            loading: false,
            loadingEarlier: false,
            retrying: false,
            loadEarlierError: false,
            recoveryError: !params.prepend,
          }
          flushPendingHistorySync()
          return { ok: !historyState.value.recoveryError }
        }
      }

      if (canonicalAvailable !== false) notifyTerminalTasks(data)

      const summaryIds = summaryCompactionIds(data)
      let mapped = attachHistoryTurnOutcomes(
        msgs.map(message => mapHistoryMessage(message, summaryIds)),
        data,
      )
      const previousMessages = options.messages.value.filter(message => !departedSessionRows.has(message))
      const previousMaintenance = previousMessages.filter(message => (
        isHistoryMaintenance(message)
        && (!params.replaceCanonicalWindow || message.restoredFromHistory !== true)
      ))
      const previousTranscript = previousMessages.filter(message => (
        !isHistoryMaintenance(message)
        && (
          !params.replaceCanonicalWindow
          || (message.restoredFromHistory !== true && !message.terminalNotice)
        )
      ))
      const maintenanceMessages = compactionSummaryMessages(data)
      const installedCompactions = () => maintenanceMessages.flatMap(message => message.maintenance ? [message.maintenance] : [])
      let historyData = data
      let bridgeContinuationNeeded = false
      const needsForwardBridge = canonicalAvailable !== false
        && !params.prepend
        && hasLoadedEarlier
        && mapped.length > 0
        && !historyWindowsOverlap(previousTranscript, mapped)
      if (needsForwardBridge) {
        bridgeAttempted = true
        failedHistoryRequest = { kind: 'bridge', key }
        historyState.value = {
          ...historyState.value,
          loadEarlierError: false,
        }

        const anchor = [...previousTranscript]
          .reverse()
          .find(message => message.restoredFromHistory === true && Boolean(message.messageId))
        const bridgeStart = historyState.value.newestCursor
        if (!anchor || bridgeStart == null) {
          throw new Error('Cannot bridge a history window without a canonical anchor')
        }

        const bridged: ChatMessage[] = []
        const bridgedKeys = new Set<string>()
        const visitedCursors = new Set<string>()
        let after: string | number = bridgeStart
        let finalBridgeData: SessionReadHistoryPage | null = null
        let bridgeComplete = responseCanonicalComplete(data)
        let bridgePageCount = 0
        let bridgeTruncated = false

        while (true) {
          const afterKey = String(after)
          if (visitedCursors.has(afterKey)) {
            throw new Error('History forward pagination stalled')
          }
          visitedCursors.add(afterKey)

          const bridgeData = await callHistory(
            lease,
            { direction: 'after', cursor: String(after), limit: 200 },
            bootstrap,
          )
          if (!isCurrentRequest()) return { ok: false, cancelled: true }
          const bridgeAvailable = bridgeData.canonicalAvailable
          if (bridgeAvailable === false) {
            if (nonReconnecting) {
              restoreSilentBackgroundState()
              return { ok: false }
            }
            historyState.value = {
              ...historyState.value,
              canonicalAvailable: false,
              canonicalComplete: responseCanonicalComplete(bridgeData),
              loading: false,
              loadingEarlier: false,
              retrying: false,
              loadEarlierError: false,
              recoveryError: true,
            }
            flushPendingHistorySync()
            return { ok: false }
          }

          notifyTerminalTasks(bridgeData)

          const page = attachHistoryTurnOutcomes(
            bridgeData.messages.map(message =>
              mapHistoryMessage(message, summaryCompactionIds(bridgeData)),
            ),
            bridgeData,
          )
          maintenanceMessages.push(...compactionSummaryMessages(bridgeData))
          for (const message of page) {
            const keyValue = messageKey(message)
            if (bridgedKeys.has(keyValue)) continue
            bridgedKeys.add(keyValue)
            bridged.push(message)
          }
          finalBridgeData = bridgeData
          bridgePageCount += 1
          const pageComplete = bridgeData.canonicalComplete
          if (pageComplete === false) bridgeComplete = false

          const hasMore = bridgeData.hasMore
          const nextCursor = bridgeData.newestCursor
          if (page.length === 0 || nextCursor == null || String(nextCursor) === afterKey) {
            throw new Error('History forward pagination did not advance')
          }
          if (!hasMore) break
          if (bridgePageCount >= MAX_FORWARD_BRIDGE_PAGES) {
            bridgeTruncated = true
            break
          }
          after = nextCursor
        }

        if (bridged.length === 0 || finalBridgeData == null) {
          throw new Error('History forward pagination returned no bridge')
        }
        // A disconnected latest window is only safe to merge after the bounded
        // bridge reaches it. Otherwise keep this refresh contiguous and advance
        // newestCursor so a later sync can resume from the new boundary.
        if (!bridgeTruncated) {
          for (const message of mapped) {
            const keyValue = messageKey(message)
            if (bridgedKeys.has(keyValue)) continue
            bridgedKeys.add(keyValue)
            bridged.push(message)
          }
        }

        mapped = [anchor, ...bridged.filter(message => messageKey(message) !== messageKey(anchor))]
        bridgeContinuationNeeded = bridgeTruncated
        historyData = {
          ...data,
          newestCursor: finalBridgeData.newestCursor,
          canonicalAvailable: true,
          canonicalComplete: bridgeComplete,
        }
      }

      if (canonicalAvailable !== false || confirmedEmptyDraft) failedHistoryRequest = null
      // Gate the full-session error on explicit coverage metadata. Older
      // Gateways used canonical_available=false for a legitimate empty WebChat
      // session but did not yet publish canonical_complete.
      const initialCanonicalLoadFailed = isInitialLoad
        && !params.prepend
        && !hasLoadedEarlier
        && mapped.length === 0
        && canonicalAvailable === false
        && data.canonicalComplete === false
      updateHistoryState(
        historyData,
        Boolean(params.prepend),
        initialCanonicalLoadFailed,
      )
      if (params.prepend && params.before != null) {
        hasLoadedEarlier = true
        loadedEarlierCursors.add(String(params.before))
      }
      const preserveLiveTail = (
        Boolean(options.preserveLiveTail?.value)
        || preserveLocalTailGeneration > acknowledgedPreserveLocalTailGeneration
      )

      const acknowledgePreservedLocalTail = () => {
        if (
          !params.prepend
          && canonicalAvailable !== false
          && requestPreserveLocalTailGeneration > acknowledgedPreserveLocalTailGeneration
          && requestPreserveLocalTailGeneration === preserveLocalTailGeneration
        ) {
          acknowledgedPreserveLocalTailGeneration = requestPreserveLocalTailGeneration
        }
      }

      if (msgs.length === 0 && !params.prepend) {
        const transcript = preserveLiveTail
          ? reconcileRunningHistoryMessages(previousTranscript, [])
          : hasLocalOptimisticRows(previousTranscript)
            ? previousTranscript
            : []
        options.messages.value = mergeHistoryMaintenance(
          transcript,
          [...previousMaintenance, ...maintenanceMessages],
        )
        if (options.messages.value.length === 0) {
          options.lastHeaderRole.value = ''
          options.lastHeaderDay.value = ''
        }
        if (canonicalAvailable !== false || confirmedEmptyDraft) options.onHistoryInstalled?.(key, installedCompactions())
        acknowledgePreservedLocalTail()
        flushPendingHistorySync()
        return { ok: true }
      }

      const followingLiveEdge = !params.prepend && (options.autoScroll?.value ?? true)
      const historyContainer = options.threadRef?.value ?? null
      if (params.prepend) {
        const existing = new Set(previousTranscript.map(messageKey))
        const transcript = interleaveHistoryModelCallSegments(
          rehomePromotedSteerRows(
            dedupeTerminalErrorNotices([
              ...mapped.filter(msg => !existing.has(messageKey(msg))
                || (msg.role === 'error' && msg.terminalNotice && msg.turnId)),
              ...previousTranscript,
            ]),
          ),
        )
        options.messages.value = mergeHistoryMaintenance(
          transcript,
          [...previousMaintenance, ...maintenanceMessages],
        )
      } else {
        const refreshedWindow = reconcileHistoryWindow(previousTranscript, mapped)
        let nextMessages: ChatMessage[]
        if (params.replaceCanonicalWindow || preserveLiveTail) {
          nextMessages = reconcileRunningHistoryMessages(previousTranscript, refreshedWindow)
        } else {
          nextMessages = refreshedWindow
        }
        nextMessages = preserveAcceptedEnsembleRouterRows(
          previousTranscript,
          nextMessages,
          mapped,
        )
        const transcript = interleaveHistoryModelCallSegments(
          rehomePromotedSteerRows(
            reconcileClientTerminalNotices(previousTranscript, nextMessages),
          ),
        )
        options.messages.value = mergeHistoryMaintenance(
          transcript,
          [...previousMaintenance, ...maintenanceMessages],
        )
      }

      options.lastHeaderRole.value = ''
      options.lastHeaderDay.value = ''
      if (!params.prepend && canonicalAvailable !== false) options.onHistoryInstalled?.(key, installedCompactions())

      // The list virtualizer owns prepend/reflow geometry. History only
      // preserves the user's live-edge intent across its async data commit.
      if (followingLiveEdge) {
        // Message assignment is one synchronous commit. Install the input
        // guard immediately afterwards and before yielding to layout.
        const liveEdgeGuard = historyContainer
          ? createScrollHandoffGuard(historyContainer)
          : null
        try {
          await nextTick()
          if (!isCurrentRequest()) return { ok: false, cancelled: true }
          if (
            !liveEdgeGuard?.isCancelled()
            && (options.canApplyViewportCorrection?.() ?? true)
            && (
              !historyContainer
              || (
                historyContainer.isConnected
                && options.threadRef?.value === historyContainer
              )
            )
          ) {
            // The long-history virtualizer may clamp scrollTop while replacing
            // its rows. That layout event is not reader intent, so restore the
            // live-edge ownership captured before the commit and mark the
            // correction as application-owned.
            if (options.autoScroll) options.autoScroll.value = true
            options.scrollToBottom()
          }
        } finally {
          liveEdgeGuard?.dispose()
        }
      }
      // Keep reconnect catch-up moving even when no later live event arrives.
      // Each scheduled request is still bounded to MAX_FORWARD_BRIDGE_PAGES,
      // and the existing timer/session cleanup makes the continuation yielding
      // and cancellable rather than one unbounded request or DOM update.
      if (bridgeContinuationNeeded) historySyncPending = true
      if (bridgeContinuationNeeded) {
        historySyncPendingNonReconnecting ||= nonReconnecting
      }
      if (!bridgeContinuationNeeded) acknowledgePreservedLocalTail()
      flushPendingHistorySync()
      return bridgeContinuationNeeded ? { ok: true, historyContinuation: true } : { ok: true }
    } catch (error: unknown) {
      // History endpoint may not exist yet.
      if (isCurrentRequest()) {
        const cursorRequiresLatestReload = error instanceof SessionReadHistoryCursorError
        if (nonReconnecting && !cursorRequiresLatestReload) {
          restoreSilentBackgroundState()
          return {
            ok: false,
            error,
            cancelled: bootstrap.signal.aborted || isRpcAbort(error),
          }
        }
        const initialLoadFailed = isInitialLoad && !bridgeAttempted
        failedHistoryRequest = cursorRequiresLatestReload || params.replaceCanonicalWindow
          ? { kind: 'latest', key, error }
          : bridgeAttempted
            ? { kind: 'bridge', key }
            : {
                kind: 'page',
                key,
                before: params.before ?? null,
                prepend: Boolean(params.prepend),
              }
        historyState.value = {
          ...historyState.value,
          loading: false,
          loadingEarlier: false,
          retrying: false,
          initialLoadStatus: initialLoadFailed
            ? 'error'
            : historyState.value.initialLoadStatus,
          loadEarlierError: Boolean(params.prepend),
          recoveryError: !params.prepend,
          sessionMissing: !params.prepend
            && error instanceof SessionReadSessionMissingError,
        }
        flushPendingHistorySync()
      }
      return {
        ok: false,
        error,
        cancelled: !isCurrentRequest() || bootstrap.signal.aborted || isRpcAbort(error),
      }
    }
  }

  function loadHistory(
    params: HistoryLoadParams = {},
    bootstrap?: SessionBootstrapPhaseContext,
  ): Promise<SessionPhaseResult | void> | undefined {
    const key = options.sessionKey.value
    if (!key) return
    if (
      failedHistoryRequest?.key === key
      && failedHistoryRequest.kind === 'latest'
      && !params.replaceCanonicalWindow
    ) {
      historySyncPending = true
      historySyncPendingNonReconnecting ||= Boolean(params.nonReconnecting)
      // Bootstrap and live reconciliation treat an absent result as success.
      // Keep their recovery fence closed until the replacement page succeeds.
      return Promise.resolve({ ok: false, error: failedHistoryRequest.error })
    }
    if (activeHistory) {
      if (
        activeHistory.key === key
        && activeHistory.lease === options.sessionReadLeaseReader.current()
        && (
          !bootstrap
          || activeHistory.bootstrapGeneration === bootstrap.generation
        )
      ) {
        if (params.prepend) {
          if (!historyState.value.loadingEarlier) loadEarlierPending = true
        } else {
          historySyncPending = true
          historySyncPendingNonReconnecting ||= Boolean(params.nonReconnecting)
        }
        // Never report a deduplicated request as a successful bootstrap.
        // The caller observes the real terminal result of the in-flight read.
        return activeHistory.promise
      }
      const currentRecovery = backgroundRecovery && isCurrentRecovery(backgroundRecovery)
        ? backgroundRecovery
        : null
      cancelActiveHistory()
      backgroundRecovery = currentRecovery
    }

    const controller = new AbortController()
    const parentSignal = bootstrap?.signal
    const relayAbort = () => controller.abort()
    if (parentSignal?.aborted) controller.abort()
    else parentSignal?.addEventListener('abort', relayAbort, { once: true })
    const now = Date.now()
    const boundedContext: SessionBootstrapPhaseContext = bootstrap
      ? { ...bootstrap, signal: controller.signal }
      : {
          generation: -1,
          key,
          attempt: 0,
          deadlineAt: now + SESSION_PHASE_ATTEMPT_BUDGET_MS,
          attemptDeadlineAt: now + SESSION_PHASE_ATTEMPT_BUDGET_MS,
          signal: controller.signal,
          skipSnapshot: false,
        }

    const lease = options.sessionReadLeaseReader.current()
    const requestPreserveLocalTailGeneration = preserveLocalTailGeneration
    const request = runHistoryLoad(params, boundedContext)
    const finishRequest = () => {
      parentSignal?.removeEventListener('abort', relayAbort)
      if (activeHistory?.promise !== tracked) return false
      activeHistory = null
      return true
    }
    const tracked = request.then(result => {
      if (finishRequest()) {
        // An input notification can create recovery after this read started.
        // Join it at completion, but never retire a successor lease's recovery
        // because the previous request returned a cancelled result.
        if (
          (!params.prepend || result?.ok === false)
          && backgroundRecovery?.key === key
          && backgroundRecovery.lease === lease
        ) {
          finishBackgroundRecovery(backgroundRecovery, result, requestPreserveLocalTailGeneration)
        }
        flushPendingHistorySync()
      }
      return result
    }, error => {
      if (finishRequest()) flushPendingHistorySync()
      throw error
    })
    activeHistory = {
      key,
      lease,
      bootstrapGeneration: bootstrap?.generation ?? -1,
      controller,
      promise: tracked,
    }
    return tracked
  }

  function loadEarlierHistory(bootstrap?: SessionBootstrapPhaseContext) {
    if (!historyState.value.hasMore) return
    if (historyState.value.loading) {
      if (!historyState.value.loadingEarlier) loadEarlierPending = true
      return
    }
    const cursor = historyState.value.oldestCursor
    if (cursor == null || loadedEarlierCursors.has(String(cursor))) return
    return loadHistory({ before: cursor, prepend: true }, bootstrap)
  }

  function retryHistory(bootstrap?: SessionBootstrapPhaseContext) {
    if (
      backgroundRecovery?.exhausted
      && isCurrentRecovery(backgroundRecovery)
      && failedHistoryRequest?.kind !== 'latest'
    ) {
      backgroundRecovery.retryCount = 0
      backgroundRecovery.exhausted = false
      failedHistoryRequest = null
      preserveLocalTailGeneration += 1
      return loadHistory({ nonReconnecting: true, retry: true }, bootstrap)
    }
    const failed = failedHistoryRequest
    if (failed?.key === options.sessionKey.value) {
      if (failed.kind === 'latest') {
        hasLoadedEarlier = false
        loadEarlierPending = false
        loadedEarlierCursors.clear()
        failedHistoryRequest = null
        historyState.value = {
          ...historyState.value,
          hasMore: false,
          oldestCursor: null,
          newestCursor: null,
        }
        return loadHistory({ replaceCanonicalWindow: true, retry: true }, bootstrap)
      }
      if (failed.kind === 'bridge') {
        return loadHistory({ bridgeRetry: true, retry: true }, bootstrap)
      }
      return loadHistory({
        before: failed.before,
        prepend: failed.prepend,
        retry: true,
      }, bootstrap)
    }
    if (historyState.value.canonicalAvailable === false) {
      return loadHistory({ retry: true }, bootstrap)
    }
    if (historyState.value.recoveryError || historyState.value.initialLoadStatus === 'error') {
      return loadHistory({ retry: true }, bootstrap)
    }
    return loadEarlierHistory(bootstrap)
  }

  function cancelActiveHistory() {
    backgroundRecovery = null
    activeHistory?.controller.abort()
    activeHistory = null
    ++historyRequestSeq
    if (historySyncTimer) {
      clearTimeout(historySyncTimer)
      historySyncTimer = null
    }
    historySyncTimerNonReconnecting = false
    historySyncPending = false
    historySyncPendingNonReconnecting = false
    loadEarlierPending = false
    historyState.value = {
      ...historyState.value,
      loading: false,
      loadingEarlier: false,
      retrying: false,
    }
  }

  async function reconcileHistory(): Promise<SessionPhaseResult | void> {
    const key = options.sessionKey.value
    // A read admitted before the snapshot cannot prove a terminal transition
    // after its watermark. Join it, then admit one fresh current-window read.
    await activeHistory?.promise.catch(() => {})
    if (options.sessionKey.value !== key) return { ok: false, cancelled: true }
    return loadHistory({ nonReconnecting: true })
  }

  function cleanup() {
    stopObservingSessionRows()
    cancelActiveHistory()
    historySyncPending = false
    loadEarlierPending = false
  }

  // Find uses a bounded, independent history window. Reuse exactly the normal
  // display projection without installing it into the live session or retiring
  // any task owner from this older snapshot.
  function projectHistoryPage(data: SessionReadHistoryPage): ChatMessage[] {
    const summaryIds = summaryCompactionIds(data)
    return attachHistoryTurnOutcomes(
      (data.messages ?? []).map(message => mapHistoryMessage(message, summaryIds)), data,
    )
  }

  function reconcileHydratedContent(
    message: Pick<ChatMessage, 'messageId' | 'contentRef' | 'contentRevision'>,
    text: string,
  ) {
    if (message.contentRef?.sessionKey !== options.sessionKey.value) return
    options.messages.value = interleaveHydratedHistoryModelCallSegments(options.messages.value, message, text)
  }

  return {
    historySessionKey,
    historyState,
    loadHistory,
    reconcileHistory,
    reconcileHydratedContent,
    projectHistoryPage,
    loadEarlierHistory,
    retryHistory,
    markSessionMissing,
    scheduleHistorySync,
    cancelActiveHistory,
    cleanup,
  }
}
