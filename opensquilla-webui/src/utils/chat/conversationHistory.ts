import type { ChatMessage, ChatTimelineSegment, ChatUsagePayload, RawToolCallPayload } from '@/types/chat'
import type { StatusPart } from '@/types/parts'
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'
import type { SessionReadCompactionSummary, SessionReadHistoryPage, SessionReadMessage, SessionReadTurnContext, SessionReadTurnOutcome } from '@/modules/sessionReadLifecycle'
import { copySelectedSkills } from '@/types/selectedSkills'
import { copyLocalPathReferences } from '@/types/localPathReferences'
import { pageAnnotationSnapshots } from '@/types/pageContext'
import { normalizeDisplayAttachments } from '@/utils/chat/attachments'
import { planRevisionsFromToolSegments } from '@/utils/chat/plans'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import { activityReasoningBlocks, activitySnapshotMatchesMessage } from '@/utils/chat/activitySnapshot'
import { localizedChatErrorMessage } from '@/utils/chat/errors'
import { dedupeTerminalErrorNotices, hasTerminalErrorNotice } from '@/utils/chat/terminalErrorNotices'
import { isUsageAccountingBarrier } from '@/utils/chat/usageAccountingFailure'
import { normalizePromptAnnotationSnapshot } from '@/utils/chat/promptAnnotationHistory'
import { interleaveHistoryModelCallSegments } from '@/utils/chat/historyModelCallSegments'
import { stripChatTimePrefix } from '@/utils/chat/timePrefix'

// Canonical history is projected identically for every conversation surface.
// Session creation, ownership and pagination stay with their controllers.
function recordArray<T extends Record<string, unknown>>(value: unknown): T[] {
  return Array.isArray(value)
    ? value.filter((item): item is T => !!item && typeof item === 'object' && !Array.isArray(item))
    : []
}

function usagePayload(value: unknown): ChatUsagePayload | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as ChatUsagePayload
}

function historyTurnId(value: SessionReadTurnContext | null): string | undefined {
  if (!value) return undefined
  if (historyContextText(value, 'disposition') === 'promoted') {
    return value.promotedTurnId
      ?? value.turnId
      ?? historyContextText(value, 'targetTurnId')
  }
  return value.turnId ?? undefined
}

function camelHistoryKey(key: string): string {
  return key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase())
}

function historyContextText(
  value: SessionReadTurnContext | null,
  key: string,
): string | undefined {
  if (!value) return undefined
  const camelKey = camelHistoryKey(key)
  const raw = value.additional?.[key] ?? value.additional?.[camelKey]
  return typeof raw === 'string' && raw ? raw : undefined
}

function historyTurnPresentationProvenance(value: SessionReadTurnContext | null): {
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

function historyHasSteerEvidence(value: SessionReadTurnContext | null): boolean {
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
    || Boolean(value?.promotedTurnId)
    || Boolean(historyContextText(value, 'promoted_from_turn_id'))
    || Boolean(historyContextText(value, 'model_call_id'))
    || historyContextInteger(value, 'applied_iteration') !== undefined
}

function historyInputDisposition(value: SessionReadTurnContext | null): ChatMessage['inputDisposition'] {
  const disposition = historyContextText(value, 'disposition')
  if (!historyHasSteerEvidence(value)) return undefined
  return ['steering', 'applied', 'promoted', 'cancelled', 'rejected'].includes(
    disposition || '',
  )
    ? disposition as NonNullable<ChatMessage['inputDisposition']>
    : undefined
}

function historyDispositionRevision(value: SessionReadTurnContext | null): number | undefined {
  if (
    !historyHasSteerEvidence(value)
    || !value
  ) return undefined
  const revision = Number(value.additional?.revision)
  return Number.isInteger(revision) && revision >= 0 ? revision : undefined
}

function historyContextInteger(
  value: SessionReadTurnContext | null,
  key: string,
): number | undefined {
  if (!value) return undefined
  if (key === 'applied_iteration' || key === 'appliedIteration') {
    return value.appliedIteration ?? undefined
  }
  const camelKey = camelHistoryKey(key)
  const raw = value.additional?.[key] ?? value.additional?.[camelKey]
  if (typeof raw !== 'number' && typeof raw !== 'string') return undefined
  if (typeof raw === 'string' && !raw.trim()) return undefined
  const number = Number(raw)
  return Number.isInteger(number) && number >= 0 ? number : undefined
}

function historyActivityMarkers(
  value: SessionReadTurnContext | null,
  suppressedCompactionIds: ReadonlySet<string> = new Set(),
): StatusPart[] {
  if (!value) return []
  return (value.activityMarkers || []).flatMap((marker): StatusPart[] => {
    if (!marker || typeof marker !== 'object' || Array.isArray(marker)) return []
    const data = marker as Record<string, unknown>
    if (data.kind !== 'context_compaction') return []
    const id = String(data.id || '').trim()
    if (!id || suppressedCompactionIds.has(id)) return []
    const rawStatus = String(data.status || 'completed').toLowerCase()
    const state = rawStatus === 'completed' || rawStatus === 'emergency_ephemeral'
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
      durability: rawStatus === 'emergency_ephemeral'
        ? 'request_scoped'
        : String(data.durability || 'durable'),
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
  summary: SessionReadCompactionSummary,
  canonicalComplete: boolean | null,
): ChatMessage | null {
  const summaryId = summaryStableValue(summary.id)
  const compactionId = summaryStableValue(summary.compactionId)
  const compactionIndex = summaryStableValue(summary.compactionIndex)
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
    ts: normalizedEpochMilliseconds(summary.createdAt),
    messageId: `maintenance:context-compaction:${identity}`,
    restoredFromHistory: true,
    maintenance: {
      kind: 'context_compaction',
      compactionId: compactionId || identity,
      source: String(summary.triggerReason || '').trim().toLowerCase() === 'manual'
        ? 'manual'
        : 'automatic',
      state: 'completed',
      durability: 'durable',
      removedCount: summaryCount(summary.removedCount),
      keptCount: summaryCount(summary.keptCount),
      historyArchived: true,
      canonicalComplete,
    },
  }
}

export function compactionSummaryMessages(data: SessionReadHistoryPage): ChatMessage[] {
  return (data.compactionSummaries || []).flatMap((summary) => {
    const message = compactionSummaryMessage(summary, data.canonicalComplete)
    return message ? [message] : []
  })
}

export function summaryCompactionIds(data: SessionReadHistoryPage): Set<string> {
  return new Set(
    compactionSummaryMessages(data).map(message => message.maintenance!.compactionId),
  )
}

export function isHistoryMaintenance(message: ChatMessage): boolean {
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
export function mergeHistoryMaintenance(
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

export function turnOutcomeRecord(outcome: SessionReadTurnOutcome): Record<string, unknown> {
  return {
    ...outcome.additional,
    turnId: outcome.turnId,
    taskId: outcome.taskId ?? undefined,
    status: outcome.status,
    statusSource: 'task',
    startedAt: outcome.startedAt ?? undefined,
    finishedAt: outcome.finishedAt ?? undefined,
    outcome: outcome.outcome,
    errorClass: outcome.errorClass ?? undefined,
    ...(Object.prototype.hasOwnProperty.call(outcome, 'httpStatus') ? { httpStatus: outcome.httpStatus } : {}),
    retryable: outcome.retryable ?? undefined,
    activitySnapshot: outcome.activitySnapshot ?? undefined,
    usage: outcome.usage ?? undefined,
    usageCallIndex: outcome.replayProof.usageCallIndex ?? undefined,
    noPriorProviderDispatch: outcome.replayProof.noPriorProviderDispatch ?? undefined,
    replaySafe: outcome.replayProof.replaySafe ?? undefined,
    retryAfterMs: outcome.replayProof.retryAfterMs ?? undefined,
    userMessageId: outcome.replayProof.userMessageId ?? undefined,
    terminalMessage: outcome.replayProof.terminalMessage ?? undefined,
  }
}

function isLegacyTerminalError(message: SessionReadMessage): boolean {
  if (message.role !== 'system') return false
  // Cron/channel and imported system rows are user-authored content even when
  // they happen to begin with “Error:”. Only unscoped engine receipts are
  // eligible for the safe terminal projection.
  if (message.provenance?.kind || message.provenance?.sourceTool || message.provenance?.sourceSessionKey) return false
  const text = (message.text || '').trim()
  // These are engine-owned transcript receipts. Their prose supplies no
  // classification: absent a durable outcome they receive the safe unknown.
  return text.startsWith('Error:')
    || /^The provider stopped because the output limit was reached before the task finished\.(?: \(ref: [0-9a-f]{8}\))?$/.test(text)
}

export function attachHistoryTurnOutcomes(
  messages: ChatMessage[],
  data: SessionReadHistoryPage,
): ChatMessage[] {
  const outcomes =
    (data.turnOutcomes || [])
      .map(outcome => normalizeTurnOutcome(turnOutcomeRecord(outcome)))
      .filter(outcome => outcome !== undefined)
  const byTurnId = new Map(outcomes.map(outcome => [outcome.turnId, outcome] as const))
  if (byTurnId.size === 0) return dedupeTerminalErrorNotices(messages)
  const enriched = messages.map(message => {
    const outcome = message.turnId ? byTurnId.get(message.turnId) : undefined
    if (!outcome) return message
    const activitySnapshot = message.role === 'assistant'
      ? outcome.activitySnapshot
      : undefined
    const snapshotReasoningBlocks = activitySnapshot?.complete
      ? activityReasoningBlocks(activitySnapshot, message.reasoning?.text ?? '', message.historyPayloadPreview?.reasoningUtf16Length)
      : undefined
    const activitySnapshotComplete = Boolean(
      activitySnapshot?.complete
      && snapshotReasoningBlocks !== undefined
      && activitySnapshotMatchesMessage(activitySnapshot, message),
    )
    const acceptedActivitySnapshot = activitySnapshot
      ? activitySnapshotComplete
        ? activitySnapshot
        : { ...activitySnapshot, complete: false }
      : undefined
    const durableLocalizedError = message.role === 'error'
    return {
      ...message,
      ...(durableLocalizedError
        ? {
            role: 'error',
            text: localizedChatErrorMessage(
              outcome.errorClass,
              outcome.terminalMessage || message.text,
              outcome.replaySafe === true,
              outcome.failureKind,
              outcome.status,
              { reason: outcome.reason, cancellationSource: outcome.cancellationSource, outcomeKind: outcome.kind },
            ),
            errorCode: outcome.errorClass,
            terminalNotice: true,
          }
        : {}),
      ...(message.role === 'assistant' && acceptedActivitySnapshot
        ? {
            activitySnapshot: acceptedActivitySnapshot,
            activitySnapshotIncomplete: !activitySnapshotComplete,
            ...(activitySnapshotComplete
              ? {
                  statusHistory: (outcome.statusHistory || []).map(entry => ({ ...entry })),
                  reasoningBlocks: snapshotReasoningBlocks?.map(block => ({ ...block })),
                }
              : {}),
          }
        : message.role === 'assistant' && outcome.statusHistory?.length
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

  // A terminal task may finish before an assistant transcript row exists.
  // Materialize one activity-only assistant row so refresh and reconnect keep
  // the durable route/activity trace inspectable.
  for (const outcome of outcomes) {
    const legacyUsageActivity = isUsageAccountingBarrier(outcome.errorClass)
      && Boolean(outcome.statusHistory?.length)
    if (!outcome.activitySnapshot && !legacyUsageActivity) continue
    if (enriched.some(message => message.turnId === outcome.turnId && message.role === 'assistant')) continue
    const turnIndexes = enriched.flatMap((message, index) =>
      message.turnId === outcome.turnId ? [index] : [],
    )
    if (!turnIndexes.length) continue
    const firstTerminalIndex = turnIndexes.find(index => enriched[index]?.role === 'error')
    const insertionIndex = firstTerminalIndex ?? Math.max(...turnIndexes) + 1
    const activityOnlyMessage: ChatMessage = {
      role: 'assistant',
      text: '',
      ts: outcome.finishedAt ?? null,
      turnId: outcome.turnId,
      turnOutcome: outcome,
      statusHistory: (outcome.statusHistory || []).map(entry => ({ ...entry })),
      messageId: `terminal-activity:${outcome.taskId || outcome.turnId}`,
      restoredFromHistory: true,
    }
    if (outcome.activitySnapshot) {
      const snapshotReasoningBlocks = outcome.activitySnapshot.complete
        ? activityReasoningBlocks(outcome.activitySnapshot, '')
        : undefined
      const snapshotComplete = Boolean(
        outcome.activitySnapshot.complete
        && snapshotReasoningBlocks !== undefined
        && activitySnapshotMatchesMessage(outcome.activitySnapshot, activityOnlyMessage),
      )
      activityOnlyMessage.activitySnapshot = snapshotComplete
        ? outcome.activitySnapshot
        : { ...outcome.activitySnapshot, complete: false }
      activityOnlyMessage.activitySnapshotIncomplete = !snapshotComplete
      if (snapshotComplete) {
        activityOnlyMessage.reasoningBlocks = snapshotReasoningBlocks?.map(block => ({ ...block }))
      }
    }
    enriched.splice(insertionIndex, 0, activityOnlyMessage)
  }

  // The task outcome is the durable authority for these terminal failures.
  // Transcript error rows are best-effort and may be absent from a compacted
  // or paginated window, so recover the notice when its identified turn is
  // present but has no matching error row in this page.
  for (const outcome of outcomes) {
    if (!hasTerminalErrorNotice(outcome)) continue
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
        outcome.failureKind,
        outcome.status,
        { reason: outcome.reason, cancellationSource: outcome.cancellationSource, outcomeKind: outcome.kind },
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
  return dedupeTerminalErrorNotices(enriched)
}

type AcceptedEnsembleMode = 'ensemble' | 'llm_ensemble'

function acceptedEnsembleMode(message: ChatMessage): AcceptedEnsembleMode | null {
  if (message.role !== 'router' || !message.routerDecision) return null
  const raw = String(
    message.routerDecision.accepted_routing_mode || '',
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
export function preserveAcceptedEnsembleRouterRows(
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

export function projectConversationHistoryMessage(
  msg: SessionReadMessage,
  suppressedCompactionIds: ReadonlySet<string> = new Set(),
  stripTimePrefix: (text: string) => string = stripChatTimePrefix,
): ChatMessage {
  // History rows carry the turn's reasoning text but not the measured
  // thinking duration; live turn records re-fill seconds after sync.
  const rawReasoningText = typeof msg.reasoningContent === 'string'
    ? msg.reasoningContent
    : ''
  // v2 reasoning entries address the canonical transcript in UTF-16 units.
  // Use trim only to decide whether the field is empty; never shift a valid
  // snapshot's offsets by rewriting its leading or trailing whitespace.
  const reasoningText = rawReasoningText.trim() ? rawReasoningText : ''
  const messageId = msg.messageId || msg.id || ''
  const steerContext = historyHasSteerEvidence(msg.turnContext)
  const turnProvenance = historyTurnPresentationProvenance(msg.turnContext)
  const terminalError = msg.role === 'error' || isLegacyTerminalError(msg)
  return {
    role: terminalError ? 'error' : msg.role || 'assistant',
    text: terminalError
      ? localizedChatErrorMessage(undefined, '')
      : msg.role === 'user' ? stripTimePrefix(msg.text || '') : msg.text || '',
    ...(terminalError ? { terminalNotice: true } : {}),
    ts: msg.createdAt,
    reasoning: reasoningText ? { text: reasoningText, seconds: 0 } : undefined,
    routerDecision: msg.routerDecision,
    artifacts: [...(msg.artifacts || [])],
    tool_calls: recordArray<RawToolCallPayload>(msg.toolCalls),
    planRevisions: planRevisionsFromToolSegments(msg.toolCalls),
    timeline: recordArray<ChatTimelineSegment>(msg.timeline),
    attachments: normalizeDisplayAttachments([...(msg.attachments || [])], { messageId }),
    ...(msg.selectedSkills?.length ? { selectedSkills: copySelectedSkills(msg.selectedSkills) } : {}),
    ...(msg.localPathReferences?.length ? { localPathReferences: copyLocalPathReferences(msg.localPathReferences, msg.text) } : {}),
    promptAnnotations: msg.pageContext
      ? pageAnnotationSnapshots(msg.pageContext)
      : (msg.promptAnnotations || [])
          .map(normalizePromptAnnotationSnapshot)
          .filter((item): item is PromptAnnotationSnapshot => item !== null)
          .sort((left, right) => (left.sentOrder || 0) - (right.sentOrder || 0)),
    provenanceKind: msg.provenance?.kind || '',
    provenanceSourceSessionKey: msg.provenance?.sourceSessionKey || '',
    provenanceSourceTool: msg.provenance?.sourceTool || '',
    turnId: historyTurnId(msg.turnContext),
    turnInputMode: turnProvenance.inputMode,
    turnRunKind: turnProvenance.runKind,
    inputDisposition: historyInputDisposition(msg.turnContext),
    inputDispositionRevision: historyDispositionRevision(msg.turnContext),
    steerClientRequestId: steerContext
      ? historyContextText(msg.turnContext, 'client_request_id')
      : undefined,
    steerClientMessageId: steerContext
      ? historyContextText(msg.turnContext, 'client_message_id')
      : undefined,
    steerModelCallId: steerContext
      ? historyContextText(msg.turnContext, 'model_call_id')
      : undefined,
    steerAppliedIteration: steerContext
      ? historyContextInteger(msg.turnContext, 'applied_iteration')
      : undefined,
    promotedFromTurnId: steerContext
      ? historyContextText(msg.turnContext, 'promoted_from_turn_id')
      : undefined,
    usage: usagePayload(msg.usage),
    model: msg.model || undefined,
    input: msg.inputTokens ?? undefined,
    output: msg.outputTokens ?? undefined,
    statusHistory: msg.role === 'assistant'
      ? historyActivityMarkers(msg.turnContext, suppressedCompactionIds)
      : undefined,
    messageId,
    ...(msg.contentRef ? { contentRef: { ...msg.contentRef } } : {}),
    ...(msg.previewComplete !== undefined ? { previewComplete: msg.previewComplete } : {}),
    ...(msg.historyPayloadPreview ? { historyPayloadPreview: msg.historyPayloadPreview } : {}),
    ...(msg.contentRevision ? { contentRevision: msg.contentRevision } : {}),
    ...(msg.contentAvailability ? { contentAvailability: msg.contentAvailability } : {}),
    ...(msg.contentUnavailableReason
      ? { contentUnavailableReason: msg.contentUnavailableReason }
      : {}),
    restoredFromHistory: true,
  }
}


export function projectConversationHistoryPage(data: SessionReadHistoryPage): ChatMessage[] {
  const suppressed = summaryCompactionIds(data)
  const messages = attachHistoryTurnOutcomes(
    (data.messages || []).map(message => projectConversationHistoryMessage(message, suppressed)), data,
  )
  return mergeHistoryMaintenance(
    interleaveHistoryModelCallSegments(messages), compactionSummaryMessages(data),
  )
}
