import { nextTick, ref, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatTimelineSegment,
  ChatUsagePayload,
  RawToolCallPayload,
} from '@/types/chat'
import type {
  ChatCompactionSummary,
  ChatHistoryMessage,
  ChatHistoryResponse,
} from '@/types/rpc'
import type { StatusPart } from '@/types/parts'
import { normalizeDisplayAttachments } from '@/utils/chat/attachments'
import {
  historyWindowsOverlap,
  reconcileClientTerminalNotices,
  reconcileHistoryWindow,
  reconcileRunningHistoryMessages,
  rehomePromotedSteerRows,
} from '@/utils/chat/historyMerge'
import {
  captureVisibleMessageAnchor,
  restoreMessageAnchor,
  stabilizeMessageAnchor,
} from '@/utils/chat/scrollAnchor'
import { applyProgrammaticScroll } from '@/utils/chat/scrollMutation'
import type { InitialHistoryLoadStatus } from '@/utils/chat/sessionLoadState'
import { planRevisionsFromToolSegments } from '@/utils/chat/plans'
import {
  SESSION_PHASE_ATTEMPT_BUDGET_MS,
  isRpcAbort,
  phaseCallOptions,
  phaseTimeoutMs,
  type SessionBootstrapPhaseContext,
  type SessionPhaseResult,
} from '@/composables/chat/sessionBootstrapContract'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { isImageInputUnsupported, localizedChatErrorMessage } from '@/utils/chat/errors'
import { isUsageAccountingBarrier } from '@/utils/chat/usageAccountingFailure'
import { interleaveHistoryModelCallSegments } from '@/utils/chat/historyModelCallSegments'

type RpcClient = {
  policy?: Record<string, unknown> | null
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<T>
}

function historyTerminationActions(rpc: RpcClient) {
  const action = rpc.policy?.concurrent_history_reads === true
    ? 'reject' as const
    : 'reconnect' as const
  return {
    timeoutAction: action,
    abortAction: action,
  }
}

function nonReconnectingHistoryActions() {
  return {
    timeoutAction: 'reject' as const,
    abortAction: 'reject' as const,
  }
}

function recordArray<T extends Record<string, unknown>>(value: unknown): T[] {
  return Array.isArray(value)
    ? value.filter((item): item is T => !!item && typeof item === 'object' && !Array.isArray(item))
    : []
}

function usagePayload(value: unknown): ChatUsagePayload | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as ChatUsagePayload
}

function historyTurnId(value: unknown): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const context = value as Record<string, unknown>
  const turnId = context.disposition === 'promoted'
    ? context.promoted_turn_id ?? context.turn_id ?? context.target_turn_id
    : context.turn_id
  return typeof turnId === 'string' && turnId ? turnId : undefined
}

function historyContextText(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const raw = (value as Record<string, unknown>)[key]
  return typeof raw === 'string' && raw ? raw : undefined
}

function historyTurnPresentationProvenance(value: unknown): {
  inputMode?: string
  runKind?: string
} {
  const explicitInputMode = historyContextText(value, 'input_mode')
    ?? historyContextText(value, 'inputMode')
  const explicitRunKind = historyContextText(value, 'run_kind')
    ?? historyContextText(value, 'runKind')
  const isLegacyGoalContinuation = historyContextText(value, 'intent') === 'goal_continuation'
  return {
    inputMode: explicitInputMode
      ?? (isLegacyGoalContinuation ? 'system_event' : undefined),
    runKind: explicitRunKind
      ?? (isLegacyGoalContinuation ? 'goal' : undefined),
  }
}

function historyHasSteerEvidence(value: unknown): boolean {
  const disposition = historyContextText(value, 'disposition')
  const intent = historyContextText(value, 'intent')
  // Current gateways persist an intent for both primary sends and Steers.
  // Treat that explicit value as authoritative: primary sends also carry a
  // client_request_id, so transport identity alone cannot prove Steer UX.
  if (intent) return intent === 'steer'
  // Older gateways omitted intent. Preserve their Steer rows using fields
  // that are specific to same-turn admission/application rather than IDs
  // shared by every durable user input.
  return disposition === 'steering'
    || disposition === 'promoted'
    || Boolean(historyContextText(value, 'promoted_turn_id'))
    || Boolean(historyContextText(value, 'promoted_from_turn_id'))
    || Boolean(historyContextText(value, 'model_call_id'))
    || historyContextInteger(value, 'applied_iteration') !== undefined
}

function historyInputDisposition(value: unknown): ChatMessage['inputDisposition'] {
  const disposition = historyContextText(value, 'disposition')
  if (!historyHasSteerEvidence(value)) return undefined
  return ['steering', 'applied', 'promoted', 'cancelled', 'rejected'].includes(
    disposition || '',
  )
    ? disposition as NonNullable<ChatMessage['inputDisposition']>
    : undefined
}

function historyDispositionRevision(value: unknown): number | undefined {
  if (
    !historyHasSteerEvidence(value)
    || !value
    || typeof value !== 'object'
    || Array.isArray(value)
  ) return undefined
  const revision = Number((value as Record<string, unknown>).revision)
  return Number.isInteger(revision) && revision >= 0 ? revision : undefined
}

function historyContextInteger(value: unknown, key: string): number | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const raw = (value as Record<string, unknown>)[key]
  if (typeof raw !== 'number' && typeof raw !== 'string') return undefined
  if (typeof raw === 'string' && !raw.trim()) return undefined
  const number = Number(raw)
  return Number.isInteger(number) && number >= 0 ? number : undefined
}

function historyActivityMarkers(
  value: unknown,
  suppressedCompactionIds: ReadonlySet<string> = new Set(),
): StatusPart[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  const markers = (value as Record<string, unknown>).activity_markers
  if (!Array.isArray(markers)) return []
  return markers.flatMap((marker): StatusPart[] => {
    if (!marker || typeof marker !== 'object' || Array.isArray(marker)) return []
    const data = marker as Record<string, unknown>
    if (data.kind !== 'context_compaction') return []
    const id = String(data.id || '').trim()
    if (!id || suppressedCompactionIds.has(id)) return []
    const rawStatus = String(data.status || 'completed').toLowerCase()
    const state = rawStatus === 'completed'
      ? 'completed'
      : rawStatus === 'failed' ? 'failed' : 'running'
    const at = Number(data.at)
    return [{
      action: 'context_compaction',
      label: '',
      at: Number.isFinite(at) ? at : 0,
      id,
      category: 'maintenance',
      state,
      source: 'automatic',
      durability: 'durable',
    }]
  })
}

function summaryStableValue(value: unknown): string | null {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (typeof value !== 'string') return null
  const normalized = value.trim()
  return normalized || null
}

function summaryCount(value: unknown): number | undefined {
  const count = Number(value)
  return Number.isInteger(count) && count >= 0 ? count : undefined
}

function compactionSummaryMessage(
  summary: ChatCompactionSummary,
  canonicalComplete: boolean | null,
): ChatMessage | null {
  const summaryId = summaryStableValue(summary.id)
  const compactionId = summaryStableValue(summary.compaction_id)
  const compactionIndex = summaryStableValue(summary.compaction_index)
  const identity = summaryId
    ? `summary:${summaryId}`
    : compactionId
      ? `compaction:${compactionId}`
      : compactionIndex
        ? `index:${compactionIndex}`
        : null
  if (!identity) return null

  return {
    role: 'maintenance',
    text: '',
    ts: normalizedEpochMilliseconds(summary.created_at),
    messageId: `maintenance:context-compaction:${identity}`,
    restoredFromHistory: true,
    maintenance: {
      kind: 'context_compaction',
      compactionId: compactionId || identity,
      source: String(summary.trigger_reason || '').trim().toLowerCase() === 'manual'
        ? 'manual'
        : 'automatic',
      state: 'completed',
      durability: 'durable',
      removedCount: summaryCount(summary.removed_count),
      keptCount: summaryCount(summary.kept_count),
      historyArchived: true,
      canonicalComplete,
    },
  }
}

function compactionSummaryMessages(data: ChatHistoryResponse): ChatMessage[] {
  const summaries = data.compaction_summaries ?? data.compactionSummaries ?? []
  const completeness = data.canonical_complete ?? data.canonicalComplete
  const canonicalComplete = typeof completeness === 'boolean' ? completeness : null
  return summaries.flatMap((summary) => {
    const message = compactionSummaryMessage(summary, canonicalComplete)
    return message ? [message] : []
  })
}

function summaryCompactionIds(data: ChatHistoryResponse): Set<string> {
  return new Set(
    compactionSummaryMessages(data).map(message => message.maintenance!.compactionId),
  )
}

function isHistoryMaintenance(message: ChatMessage): boolean {
  return message.role === 'maintenance'
    && message.maintenance?.kind === 'context_compaction'
    && Boolean(message.maintenance.compactionId.trim())
}

function maintenancePriority(message: ChatMessage): number {
  return (message.restoredFromHistory === true ? 2 : 0)
    + (message.maintenance?.durability === 'durable' ? 1 : 0)
}

function normalizedEpochMilliseconds(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !value.trim()) return null
  const numeric = Number(value)
  if (Number.isFinite(numeric)) {
    // Persisted summaries from older gateways may use epoch seconds while
    // transcript rows and relativeTime use epoch milliseconds.
    return Math.abs(numeric) < 100_000_000_000 ? numeric * 1_000 : numeric
  }
  if (typeof value !== 'string') return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function historyTimestamp(value: ChatMessage['ts']): number {
  return normalizedEpochMilliseconds(value) ?? Number.POSITIVE_INFINITY
}

/** Merge durable maintenance without letting it participate in canonical page overlap. */
function mergeHistoryMaintenance(
  messages: ChatMessage[],
  maintenance: ChatMessage[],
): ChatMessage[] {
  const candidates = [
    ...messages.filter(isHistoryMaintenance),
    ...maintenance,
  ]
  const archivedCompactionIds = new Set(
    candidates.flatMap(message =>
      message.maintenance?.historyArchived
        ? [message.maintenance.compactionId.trim()]
        : [],
    ),
  )
  const canonical = messages
    .filter(message => !isHistoryMaintenance(message))
    .map((message) => {
      const statusHistory = message.statusHistory?.filter(entry =>
        !(entry.category === 'maintenance'
          && entry.id
          && archivedCompactionIds.has(entry.id)),
      )
      return statusHistory?.length === message.statusHistory?.length
        ? message
        : { ...message, statusHistory }
    })
  const embeddedCompactionIds = new Set(
    canonical.flatMap(message =>
      (message.statusHistory ?? []).flatMap(entry =>
        entry.category === 'maintenance' && entry.id ? [entry.id] : [],
      ),
    ),
  )
  const maintenanceByCompactionId = new Map<string, ChatMessage>()
  for (const message of candidates) {
    if (!isHistoryMaintenance(message)) continue
    const compactionId = message.maintenance!.compactionId.trim()
    if (embeddedCompactionIds.has(compactionId)) continue
    const existing = maintenanceByCompactionId.get(compactionId)
    if (!existing || maintenancePriority(message) >= maintenancePriority(existing)) {
      maintenanceByCompactionId.set(compactionId, message)
    }
  }
  if (maintenanceByCompactionId.size === 0) return canonical

  const orderedMaintenance = [...maintenanceByCompactionId.values()].sort((left, right) => {
    const leftTime = historyTimestamp(left.ts)
    const rightTime = historyTimestamp(right.ts)
    if (leftTime !== rightTime) return leftTime < rightTime ? -1 : 1
    const leftIdentity = left.messageId || left.clientId || left.maintenance!.compactionId
    const rightIdentity = right.messageId || right.clientId || right.maintenance!.compactionId
    return leftIdentity.localeCompare(rightIdentity)
  })
  const merged = canonical.slice()

  for (const event of orderedMaintenance) {
    const eventTime = historyTimestamp(event.ts)
    // Canonical order can intentionally be non-chronological (for example a
    // promoted steer re-homed behind the completed turn it originally
    // targeted). Never sort those rows. Insert the maintenance event at the
    // first timestamp boundary after it, with equal timestamps remaining
    // canonical-first.
    const nextCanonicalIndex = Number.isFinite(eventTime)
      ? merged.findIndex(candidate =>
          !isHistoryMaintenance(candidate)
          && historyTimestamp(candidate.ts) > eventTime,
        )
      : -1
    merged.splice(nextCanonicalIndex < 0 ? merged.length : nextCanonicalIndex, 0, event)
  }

  return merged
}

function attachHistoryTurnOutcomes(
  messages: ChatMessage[],
  data: ChatHistoryResponse,
): ChatMessage[] {
  const outcomes =
    (data.turn_outcomes || [])
      .map(normalizeTurnOutcome)
      .filter(outcome => outcome !== undefined)
  const byTurnId = new Map(outcomes.map(outcome => [outcome.turnId, outcome] as const))
  if (byTurnId.size === 0) return messages
  const enriched = messages.map(message => {
    const outcome = message.turnId ? byTurnId.get(message.turnId) : undefined
    if (!outcome) return message
    const usageBarrier = isUsageAccountingBarrier(outcome.errorClass)
    const durableLocalizedError = (usageBarrier || isImageInputUnsupported(outcome.errorClass))
      && message.role === 'system'
      && message.text.trimStart().startsWith('Error:')
    return {
      ...message,
      ...(durableLocalizedError
        ? {
            role: 'error',
            text: localizedChatErrorMessage(
              outcome.errorClass,
              outcome.terminalMessage || message.text,
              outcome.replaySafe === true,
            ),
            errorCode: outcome.errorClass,
            terminalNotice: true,
          }
        : {}),
      ...(message.role === 'assistant' && outcome.statusHistory?.length
        ? {
            statusHistory: [
              ...(message.statusHistory || []),
              ...outcome.statusHistory.filter(entry =>
                !(message.statusHistory || []).some(existing =>
                  existing.action === entry.action && existing.at === entry.at,
                ),
              ),
            ].map(entry => ({ ...entry })),
          }
        : {}),
      turnOutcome: outcome,
    }
  })

  // A pre-provider barrier may fail before an assistant transcript row exists.
  // Materialize one status-only assistant row from the terminal task snapshot
  // so refresh and reconnect show the completed route/activity trace.
  for (const outcome of outcomes) {
    if (!isUsageAccountingBarrier(outcome.errorClass) || !outcome.statusHistory?.length) continue
    if (enriched.some(message => message.turnId === outcome.turnId && message.role === 'assistant')) continue
    const turnIndexes = enriched.flatMap((message, index) =>
      message.turnId === outcome.turnId ? [index] : [],
    )
    if (!turnIndexes.length) continue
    const firstTerminalIndex = turnIndexes.find(index => enriched[index]?.role === 'error')
    const insertionIndex = firstTerminalIndex ?? Math.max(...turnIndexes) + 1
    enriched.splice(insertionIndex, 0, {
      role: 'assistant',
      text: '',
      ts: outcome.finishedAt ?? null,
      turnId: outcome.turnId,
      turnOutcome: outcome,
      statusHistory: outcome.statusHistory.map(entry => ({ ...entry })),
      messageId: `terminal-activity:${outcome.taskId || outcome.turnId}`,
      restoredFromHistory: true,
    })
  }

  // The task outcome is the durable authority for a pre-provider usage
  // barrier. Transcript error rows are best-effort and may be absent from a
  // compacted or paginated window, so materialize the retry card whenever the
  // outcome has no matching terminal row in this page.
  for (const outcome of outcomes) {
    if (
      !isUsageAccountingBarrier(outcome.errorClass)
      && !isImageInputUnsupported(outcome.errorClass)
    ) continue
    if (enriched.some(message =>
      message.turnId === outcome.turnId && message.role === 'error',
    )) continue
    const turnIndexes = enriched.flatMap((message, index) =>
      message.turnId === outcome.turnId ? [index] : [],
    )
    if (!turnIndexes.length) continue
    enriched.splice(Math.max(...turnIndexes) + 1, 0, {
      role: 'error',
      text: localizedChatErrorMessage(
        outcome.errorClass,
        outcome.terminalMessage || '',
        outcome.replaySafe === true,
      ),
      ts: outcome.finishedAt ?? null,
      turnId: outcome.turnId,
      turnOutcome: outcome,
      messageId: `terminal-error:${outcome.taskId || outcome.turnId}`,
      restoredFromHistory: true,
      errorCode: outcome.errorClass,
      terminalNotice: true,
    })
  }
  return enriched
}

function dedupeSyntheticUsageBarrierErrors(messages: ChatMessage[]): ChatMessage[] {
  const durableErrorTurns = new Set(
    messages
      .filter(message =>
        message.role === 'error'
        && Boolean(message.turnId)
        && (
          isUsageAccountingBarrier(message.errorCode)
          || isImageInputUnsupported(message.errorCode)
        )
        && !message.messageId?.startsWith('terminal-error:'),
      )
      .map(message => message.turnId!),
  )
  const seenSynthetic = new Set<string>()
  return messages.filter(message => {
    if (
      message.role !== 'error'
      || !message.turnId
      || !(
        isUsageAccountingBarrier(message.errorCode)
        || isImageInputUnsupported(message.errorCode)
      )
      || !message.messageId?.startsWith('terminal-error:')
    ) return true
    if (durableErrorTurns.has(message.turnId)) return false
    if (seenSynthetic.has(message.messageId)) return false
    seenSynthetic.add(message.messageId)
    return true
  })
}

type AcceptedEnsembleMode = 'ensemble' | 'llm_ensemble'

function acceptedEnsembleMode(message: ChatMessage): AcceptedEnsembleMode | null {
  if (message.role !== 'router' || !message.routerDecision) return null
  const raw = String(
    message.routerDecision.accepted_routing_mode
    || message.routerDecision.acceptedRoutingMode
    || '',
  ).trim().toLowerCase()
  return raw === 'ensemble' || raw === 'llm_ensemble' ? raw : null
}

function exactMessageTurnId(message: ChatMessage): string {
  return String(message.turnId || '').trim()
}

/**
 * A live router strip is a presentation row rather than a transcript row, so
 * terminal canonical history can legitimately replace the rest of its turn
 * without returning that strip. Preserve an accepted ensemble strip only when
 * the same refresh independently proves the exact server turn id still exists.
 * Never use transcript position, text, or a neighbouring turn as a fallback.
 */
function preserveAcceptedEnsembleRouterRows(
  previous: ChatMessage[],
  reconciled: ChatMessage[],
  authoritative: ChatMessage[],
): ChatMessage[] {
  const authoritativeTurnIds = new Set(
    authoritative.map(exactMessageTurnId).filter(Boolean),
  )
  if (authoritativeTurnIds.size === 0) return reconciled

  const acceptedByTurn = new Map<
    string,
    { message: ChatMessage; mode: AcceptedEnsembleMode }
  >()
  for (const message of previous) {
    const turnId = exactMessageTurnId(message)
    const mode = acceptedEnsembleMode(message)
    if (!turnId || !mode || !authoritativeTurnIds.has(turnId)) continue
    // Router events update one same-turn strip in place. If a compatibility
    // client left more than one row behind, the newest row is the richest one.
    acceptedByTurn.set(turnId, { message, mode })
  }
  if (acceptedByTurn.size === 0) return reconciled

  const merged = reconciled.slice()
  for (const [turnId, accepted] of acceptedByTurn) {
    let existingRouterIndex = -1
    for (let index = 0; index < merged.length; index++) {
      const candidate = merged[index]
      if (candidate?.role === 'router' && exactMessageTurnId(candidate) === turnId) {
        existingRouterIndex = index
      }
    }

    const acceptedDecision = {
      ...accepted.message.routerDecision,
      accepted_routing_mode: accepted.mode,
    }
    if (existingRouterIndex >= 0) {
      const canonical = merged[existingRouterIndex]!
      merged[existingRouterIndex] = {
        ...accepted.message,
        ...canonical,
        routerDecision: {
          ...acceptedDecision,
          ...canonical.routerDecision,
          accepted_routing_mode: accepted.mode,
        },
        ensemble: canonical.ensemble ?? accepted.message.ensemble,
        routerState: canonical.routerState ?? accepted.message.routerState,
        routerSettled: canonical.routerSettled ?? accepted.message.routerSettled ?? true,
        restoredFromHistory: true,
      }
      continue
    }

    const preserved: ChatMessage = {
      ...accepted.message,
      turnId,
      routerDecision: acceptedDecision,
      routerSettled: accepted.message.routerSettled ?? true,
      restoredFromHistory: true,
    }
    const firstUserIndex = merged.findIndex(message =>
      message.role === 'user' && exactMessageTurnId(message) === turnId,
    )
    const firstAssistantIndex = merged.findIndex(message =>
      message.role === 'assistant' && exactMessageTurnId(message) === turnId,
    )
    const firstTurnIndex = merged.findIndex(message => exactMessageTurnId(message) === turnId)
    const insertAt = firstUserIndex >= 0
      && (firstAssistantIndex < 0 || firstUserIndex < firstAssistantIndex)
      ? firstUserIndex + 1
      : firstAssistantIndex >= 0
        ? firstAssistantIndex
        : firstTurnIndex >= 0 ? firstTurnIndex : merged.length
    merged.splice(insertAt, 0, preserved)
  }
  return merged
}

export interface UseChatHistoryOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  messages: Ref<ChatMessage[]>
  threadRef?: Ref<HTMLElement | null>
  lastHeaderRole: Ref<string>
  lastHeaderDay: Ref<string>
  preserveLiveTail?: Ref<boolean>
  autoScroll?: Ref<boolean>
  stripTimePrefix: (text: string) => string
  scrollToBottom: () => void
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
}

interface HistoryLoadParams {
  before?: string | number | null
  prepend?: boolean
  bridgeRetry?: boolean
  retry?: boolean
  nonReconnecting?: boolean
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

const MAX_FORWARD_BRIDGE_PAGES = 2

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
  let hasLoadedEarlier = false
  let loadEarlierPending = false
  let failedHistoryRequest: FailedHistoryRequest | null = null
  let activeHistory: {
    key: string
    bootstrapGeneration: number
    controller: AbortController
    promise: Promise<SessionPhaseResult | void>
  } | null = null
  let stopAnchorStabilization: () => void = () => {}
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
  })

  function cancelAnchorStabilization() {
    const stop = stopAnchorStabilization
    stopAnchorStabilization = () => {}
    stop()
  }

  function armHistorySync(nonReconnecting: boolean, advanceGeneration: boolean) {
    if (nonReconnecting && advanceGeneration) preserveLocalTailGeneration += 1
    historySyncTimerNonReconnecting ||= nonReconnecting
    if (historySyncTimer) clearTimeout(historySyncTimer)
    historySyncTimer = setTimeout(() => {
      historySyncTimer = null
      const timerNonReconnecting = historySyncTimerNonReconnecting
      historySyncTimerNonReconnecting = false
      if (historyState.value.loading) {
        historySyncPending = true
        historySyncPendingNonReconnecting ||= timerNonReconnecting
        return
      }
      void loadHistory({ nonReconnecting: timerNonReconnecting })
    }, 50)
  }

  function scheduleHistorySync(preserveLocalTail = false) {
    armHistorySync(preserveLocalTail, true)
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

  function mapHistoryMessage(
    msg: ChatHistoryMessage,
    suppressedCompactionIds: ReadonlySet<string> = new Set(),
  ): ChatMessage {
    // History rows carry the turn's reasoning text but not the measured
    // thinking duration; live turn records re-fill seconds after sync.
    const reasoningText = typeof msg.reasoning_content === 'string' ? msg.reasoning_content.trim() : ''
    const messageId = msg.message_id || msg.id || ''
    const steerContext = historyHasSteerEvidence(msg.turn_context)
    const turnProvenance = historyTurnPresentationProvenance(msg.turn_context)
    return {
      role: msg.role || 'assistant',
      text: msg.role === 'user' ? options.stripTimePrefix(msg.text || '') : msg.text || '',
      ts: msg.timestamp || msg.ts || null,
      reasoning: reasoningText ? { text: reasoningText, seconds: 0 } : undefined,
      routerDecision: msg.router_decision || msg.routerDecision || null,
      artifacts: msg.artifacts || [],
      tool_calls: recordArray<RawToolCallPayload>(msg.tool_calls),
      planRevisions: planRevisionsFromToolSegments(msg.tool_calls),
      timeline: recordArray<ChatTimelineSegment>(msg.timeline),
      attachments: normalizeDisplayAttachments(msg.attachments, { messageId }),
      provenanceKind: msg.provenance_kind || '',
      provenanceSourceSessionKey: msg.provenance_source_session_key || '',
      provenanceSourceTool: msg.provenance_source_tool || '',
      turnId: historyTurnId(msg.turn_context),
      turnInputMode: turnProvenance.inputMode,
      turnRunKind: turnProvenance.runKind,
      inputDisposition: historyInputDisposition(msg.turn_context),
      inputDispositionRevision: historyDispositionRevision(msg.turn_context),
      steerClientRequestId: steerContext
        ? historyContextText(msg.turn_context, 'client_request_id')
        : undefined,
      steerClientMessageId: steerContext
        ? historyContextText(msg.turn_context, 'client_message_id')
        : undefined,
      steerModelCallId: steerContext
        ? historyContextText(msg.turn_context, 'model_call_id')
        : undefined,
      steerAppliedIteration: steerContext
        ? historyContextInteger(msg.turn_context, 'applied_iteration')
        : undefined,
      promotedFromTurnId: steerContext
        ? historyContextText(msg.turn_context, 'promoted_from_turn_id')
        : undefined,
      usage: usagePayload(msg.usage) || usagePayload(msg.turn_usage),
      model: msg.model || undefined,
      input: msg.input || msg.input_tokens || undefined,
      output: msg.output || msg.output_tokens || undefined,
      statusHistory: msg.role === 'assistant'
        ? historyActivityMarkers(msg.turn_context, suppressedCompactionIds)
        : undefined,
      messageId,
      restoredFromHistory: true,
    }
  }

  function messageKey(msg: ChatMessage): string {
    return msg.messageId || msg.clientId || `${msg.role}:${msg.ts || ''}:${msg.text || ''}`
  }

  function hasLocalOptimisticRows(messages: ChatMessage[]): boolean {
    return messages.some(msg => msg.restoredFromHistory !== true)
  }

  function responseCanonicalComplete(data: ChatHistoryResponse): boolean | null {
    const value = data.canonical_complete ?? data.canonicalComplete
    return typeof value === 'boolean' ? value : historyState.value.canonicalComplete
  }

  function responseCanonicalAvailable(data: ChatHistoryResponse): boolean | null {
    const value = data.canonical_available ?? data.canonicalAvailable
    return typeof value === 'boolean' ? value : historyState.value.canonicalAvailable
  }

  function updateHistoryState(
    data: ChatHistoryResponse,
    prepend: boolean,
    initialLoadError = false,
  ) {
    const nextOldestCursor = data.oldest_cursor ?? data.oldestCursor ?? null
    const requestedCursor = prepend ? historyState.value.oldestCursor : null
    const cursorAdvanced = !prepend || nextOldestCursor !== requestedCursor
    const preserveLoadedBoundary = !prepend && hasLoadedEarlier
    historyState.value = {
      hasMore: preserveLoadedBoundary
        ? historyState.value.hasMore
        : Boolean(data.has_more ?? data.hasMore) && cursorAdvanced,
      oldestCursor: preserveLoadedBoundary ? historyState.value.oldestCursor : nextOldestCursor,
      newestCursor: prepend
        ? historyState.value.newestCursor
        : data.newest_cursor ?? data.newestCursor ?? null,
      historyScope: data.history_scope ?? data.historyScope ?? '',
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
    }
  }

  function resetForSession(key: string): boolean {
    if (historySessionKey.value === key) return false
    cancelAnchorStabilization()
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
    }
    return crossedSession
  }

  function callHistory<T>(
    request: Record<string, unknown>,
    bootstrap: SessionBootstrapPhaseContext,
    nonReconnecting = false,
  ): Promise<T> {
    const callOptions = {
      ...phaseCallOptions(bootstrap, 'chat.history'),
      // History is background content. A slow read may fail independently,
      // without recycling a Gateway that advertises concurrent reads. Legacy
      // serial Gateways still need a fresh connection to escape a stuck read.
      ...(nonReconnecting
        ? nonReconnectingHistoryActions()
        : historyTerminationActions(options.rpc)),
      onSent: (socketGeneration: number) => {
        bootstrap.markHistoryRequestSent?.(socketGeneration)
      },
    }
    const response = options.rpc.call<T>(
      'chat.history',
      request,
      callOptions,
    )
    return response
  }

  async function runHistoryLoad(
    params: HistoryLoadParams = {},
    bootstrap: SessionBootstrapPhaseContext,
  ): Promise<SessionPhaseResult | void> {
    if (!options.sessionKey.value) return
    const key = options.sessionKey.value
    const crossedSession = resetForSession(key)
    cancelAnchorStabilization()
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
    const isCurrentRequest = () => key === options.sessionKey.value && requestSeq === historyRequestSeq
    const restoreSilentBackgroundState = () => {
      failedHistoryRequest = failedHistoryRequestBeforeLoad
      historyState.value = historyStateBeforeLoad
    }
    try {
      await options.rpc.waitForConnection(
        phaseTimeoutMs(bootstrap, 'chat.history'),
        bootstrap.signal,
        nonReconnecting
          ? nonReconnectingHistoryActions()
          : historyTerminationActions(options.rpc),
      )
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
      const request: Record<string, unknown> = {
        sessionKey: key,
        limit: !params.prepend && options.messages.value.length > 50
          ? Math.min(200, options.messages.value.length)
          : 50,
        includeCanonical: true,
        includeSummaries: true,
      }
      if (params.before != null) request.before = params.before
      const data = await callHistory<ChatHistoryResponse>(request, bootstrap, nonReconnecting)
      if (!isCurrentRequest()) return { ok: false, cancelled: true }
      const msgs = data.messages || []
      const canonicalAvailable = data.canonical_available ?? data.canonicalAvailable
      if (canonicalAvailable === false) {
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

      const summaryIds = summaryCompactionIds(data)
      let mapped = attachHistoryTurnOutcomes(
        msgs.map(message => mapHistoryMessage(message, summaryIds)),
        data,
      )
      const previousMessages = crossedSession ? [] : options.messages.value
      const previousMaintenance = previousMessages.filter(isHistoryMaintenance)
      const previousTranscript = previousMessages.filter(message => !isHistoryMaintenance(message))
      const maintenanceMessages = compactionSummaryMessages(data)
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
        let finalBridgeData: ChatHistoryResponse | null = null
        let bridgeComplete = responseCanonicalComplete(data)
        let bridgePageCount = 0
        let bridgeTruncated = false

        while (true) {
          const afterKey = String(after)
          if (visitedCursors.has(afterKey)) {
            throw new Error('History forward pagination stalled')
          }
          visitedCursors.add(afterKey)

          const bridgeData = await callHistory<ChatHistoryResponse>(
            {
              sessionKey: key,
              limit: 200,
              after,
              includeCanonical: true,
              includeSummaries: true,
            },
            bootstrap,
            nonReconnecting,
          )
          if (!isCurrentRequest()) return { ok: false, cancelled: true }
          const bridgeAvailable = bridgeData.canonical_available ?? bridgeData.canonicalAvailable
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

          const page = attachHistoryTurnOutcomes(
            (bridgeData.messages || []).map(message =>
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
          const pageComplete = bridgeData.canonical_complete ?? bridgeData.canonicalComplete
          if (pageComplete === false) bridgeComplete = false

          const hasMore = Boolean(bridgeData.has_more ?? bridgeData.hasMore)
          const nextCursor = bridgeData.newest_cursor ?? bridgeData.newestCursor ?? null
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
          newest_cursor: finalBridgeData.newest_cursor ?? finalBridgeData.newestCursor ?? null,
          canonical_available: true,
          canonical_complete: bridgeComplete ?? undefined,
        }
      }

      if (canonicalAvailable !== false) failedHistoryRequest = null
      // Gate the full-session error on explicit coverage metadata. Older
      // Gateways used canonical_available=false for a legitimate empty WebChat
      // session but did not yet publish canonical_complete.
      const initialCanonicalLoadFailed = isInitialLoad
        && !params.prepend
        && !hasLoadedEarlier
        && mapped.length === 0
        && canonicalAvailable === false
        && (data.canonical_complete ?? data.canonicalComplete) === false
      updateHistoryState(
        historyData,
        Boolean(params.prepend),
        initialCanonicalLoadFailed,
      )
      if (params.prepend && params.before != null) {
        hasLoadedEarlier = true
        loadedEarlierCursors.add(String(params.before))
      }
      const preserveLiveTail = !crossedSession && (
        Boolean(options.preserveLiveTail?.value)
        || preserveLocalTailGeneration > acknowledgedPreserveLocalTailGeneration
      )

      const acknowledgePreservedLocalTail = () => {
        if (
          !crossedSession
          && !params.prepend
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
          : !crossedSession && hasLocalOptimisticRows(previousTranscript)
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
        acknowledgePreservedLocalTail()
        flushPendingHistorySync()
        return { ok: true }
      }

      const prependContainer = params.prepend ? options.threadRef?.value ?? null : null
      const prependAnchor = captureVisibleMessageAnchor(prependContainer)
      const prependFallbackHeight = prependAnchor ? 0 : prependContainer?.scrollHeight ?? 0
      if (params.prepend) {
        const existing = new Set(previousTranscript.map(messageKey))
        const transcript = interleaveHistoryModelCallSegments(
          rehomePromotedSteerRows(
            dedupeSyntheticUsageBarrierErrors([
              ...mapped.filter(msg => !existing.has(messageKey(msg))),
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
        if (preserveLiveTail) {
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
            dedupeSyntheticUsageBarrierErrors(
              reconcileClientTerminalNotices(previousTranscript, nextMessages),
            ),
          ),
        )
        options.messages.value = mergeHistoryMaintenance(
          transcript,
          [...previousMaintenance, ...maintenanceMessages],
        )
      }

      options.lastHeaderRole.value = ''
      options.lastHeaderDay.value = ''

      if (params.prepend) {
        await nextTick()
        if (prependAnchor) {
          restoreMessageAnchor(prependAnchor)
          stopAnchorStabilization = stabilizeMessageAnchor(prependAnchor, {
            isCurrent: () => options.sessionKey.value === key
              && historySessionKey.value === key
              && historyRequestSeq === requestSeq,
          })
        } else if (prependContainer) {
          applyProgrammaticScroll(prependContainer, () => {
            prependContainer.scrollTop += Math.max(
              0,
              prependContainer.scrollHeight - prependFallbackHeight,
            )
          })
        }
      } else if (options.autoScroll?.value ?? true) {
        await nextTick()
        options.scrollToBottom()
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
      return { ok: true }
    } catch (error: unknown) {
      // History endpoint may not exist yet.
      if (isCurrentRequest()) {
        if (nonReconnecting) {
          restoreSilentBackgroundState()
          return {
            ok: false,
            error,
            cancelled: bootstrap.signal.aborted || isRpcAbort(error),
          }
        }
        const initialLoadFailed = isInitialLoad && !bridgeAttempted
        failedHistoryRequest = bridgeAttempted
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
    if (activeHistory) {
      if (
        activeHistory.key === key
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
      cancelActiveHistory()
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

    const request = runHistoryLoad(params, boundedContext)
    const tracked = request.finally(() => {
      parentSignal?.removeEventListener('abort', relayAbort)
      if (activeHistory?.promise === tracked) {
        activeHistory = null
        flushPendingHistorySync()
      }
    })
    activeHistory = {
      key,
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
    const failed = failedHistoryRequest
    if (failed?.key === options.sessionKey.value) {
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
    cancelAnchorStabilization()
    historyState.value = {
      ...historyState.value,
      loading: false,
      loadingEarlier: false,
      retrying: false,
    }
  }

  function cleanup() {
    cancelActiveHistory()
    historySyncPending = false
    loadEarlierPending = false
    cancelAnchorStabilization()
  }

  return {
    historySessionKey,
    historyState,
    loadHistory,
    loadEarlierHistory,
    retryHistory,
    scheduleHistorySync,
    cancelAnchorStabilization,
    cancelActiveHistory,
    cleanup,
  }
}
