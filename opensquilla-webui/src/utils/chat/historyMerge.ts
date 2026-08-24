import type { ChatMessage } from '@/types/chat'
import type { StatusPart } from '@/types/parts'
import { isUsageAccountingBarrier } from '@/utils/chat/usageAccountingFailure'

const TERMINAL_STEER_DISPOSITIONS = new Set([
  'applied',
  'promoted',
  'cancelled',
  'rejected',
])

function isPromotedSteerRow(message: ChatMessage): boolean {
  return message.role === 'user'
    && message.inputDisposition === 'promoted'
    && Boolean(message.turnId)
    && Boolean(message.promotedFromTurnId)
    && message.turnId !== message.promotedFromTurnId
}

/**
 * Promotion changes a durable steer row's causal turn, but it cannot change
 * the row's original transcript sequence. Rehome those rows deterministically
 * so the completed turn keeps all of its output before the promoted follow-up,
 * while multiple promoted rows retain FIFO order ahead of the new turn.
 */
export function rehomePromotedSteerRows(messages: ChatMessage[]): ChatMessage[] {
  const promoted = messages.filter(isPromotedSteerRow)
  if (promoted.length === 0) return messages

  const ordered = messages.filter(message => !isPromotedSteerRow(message))
  for (const message of promoted) {
    const promotedTurnId = message.turnId!
    let insertionIndex = ordered.findIndex(candidate => candidate.turnId === promotedTurnId)
    if (insertionIndex >= 0) {
      while (
        insertionIndex < ordered.length
        && ordered[insertionIndex]?.turnId === promotedTurnId
        && isPromotedSteerRow(ordered[insertionIndex]!)
      ) {
        insertionIndex++
      }
    } else {
      let completedTurnTail = -1
      for (let index = 0; index < ordered.length; index++) {
        if (ordered[index]?.turnId === message.promotedFromTurnId) {
          completedTurnTail = index
        }
      }
      insertionIndex = completedTurnTail >= 0 ? completedTurnTail + 1 : ordered.length
    }
    ordered.splice(insertionIndex, 0, message)
  }
  return ordered
}

// Live-only fields are written onto a row AFTER it is pushed (reasoning seconds
// from the done backfill, routerSettled from the router runtime, interrupted
// from a local Stop) and are absent from a fresh history map. Re-apply them
// when the server snapshot lacks a richer value, keyed strictly by messageId so
// a synthetic-key collision can never graft one turn's state onto another.
interface LiveFieldMergeOptions {
  preserveTurnIdentity?: boolean
}

export function mergeLiveOnlyFields(
  prev: ChatMessage,
  server: ChatMessage,
  options: LiveFieldMergeOptions = {},
): ChatMessage {
  const merged: ChatMessage = { ...server }

  // Keep the optimistic row identity after the backend assigns a durable
  // message id. Per-turn render keys use it to avoid remounting live surfaces
  // during the first authoritative history replacement.
  if (!server.clientId && prev.clientId) merged.clientId = prev.clientId

  // Older history projections do not carry turn_context. Once the caller has
  // proved that the canonical row is the same live row, retain its turn id so
  // canonical reconciliation cannot split one logical turn into two frontend
  // identities. A server-provided turn id always remains authoritative.
  if (options.preserveTurnIdentity && !server.turnId && prev.turnId) {
    merged.turnId = prev.turnId
  }

  // reasoning: server wins if it measured seconds; else keep the live seconds.
  const serverSeconds = prev.role === 'assistant' ? server.reasoning?.seconds ?? 0 : 0
  if (serverSeconds <= 0 && (prev.reasoning?.seconds ?? 0) > 0) {
    merged.reasoning = prev.reasoning
  }
  // History currently persists the canonical concatenated reasoning text but
  // not its physical-call boundaries. Preserve the just-finished structured
  // blocks after this function has already proved both rows are the same turn.
  if (
    !server.activitySnapshot?.complete
    && !server.reasoningBlocks?.length
    && prev.reasoningBlocks?.length
  ) {
    merged.reasoningBlocks = prev.reasoningBlocks.map(block => ({ ...block }))
  }

  // The fold's phase snapshot supplies an exact same-session activity start.
  // History does not persist it, so retain the local snapshot across the first
  // authoritative refresh; a cold reload still correctly falls back to counts.
  if (server.activitySnapshot?.complete && !server.activitySnapshotIncomplete) {
    merged.statusHistory = (server.statusHistory ?? []).map(entry => ({ ...entry }))
  } else if ((prev.statusHistory?.length ?? 0) > 0 || (server.statusHistory?.length ?? 0) > 0) {
    const serverRows = server.statusHistory ?? []
    const previousRows = prev.statusHistory ?? []
    const serverHasTaskPhases = serverRows.some(entry => entry.category !== 'maintenance')
    // A persisted task-phase snapshot is authoritative when one exists. A
    // server response containing only durable maintenance markers is not a
    // task-phase snapshot, though: keep the richer live phase history and
    // merge those markers into it instead of collapsing Activity to a single
    // "Context organized" row after the first history refresh.
    const rows: StatusPart[] = serverHasTaskPhases
      ? serverRows.filter(entry => entry.category !== 'maintenance')
      : previousRows.filter(entry => entry.category !== 'maintenance')
    const maintenanceById = new Map<string, number>()
    for (const entry of previousRows) {
      if (entry.category !== 'maintenance' || !entry.id) continue
      maintenanceById.set(entry.id, rows.length)
      rows.push(entry)
    }
    // Durable server markers win lifecycle fields while retaining the first
    // observed timestamp, which keeps the event anchored at the point where
    // compaction actually appeared in the live Activity timeline.
    for (const entry of serverRows) {
      if (entry.category !== 'maintenance') continue
      const id = entry.id
      const index = id ? maintenanceById.get(id) : undefined
      if (index === undefined) {
        if (id) maintenanceById.set(id, rows.length)
        rows.push(entry)
      } else {
        rows[index] = { ...rows[index], ...entry, at: rows[index]!.at }
      }
    }
    // Legacy/no-v2 rows keep their established timestamp merge behavior.
    // Complete v2 snapshots return from the authoritative branch above and
    // are never sorted by display timestamps.
    merged.statusHistory = rows.sort((left, right) => left.at - right.at)
  }

  // routerSettled is sticky: once a strip has settled it stays settled.
  if (prev.routerSettled) merged.routerSettled = true

  // interrupted: keep the local abort flag until the server persists its own.
  if (server.interrupted === undefined && prev.interrupted) {
    merged.interrupted = prev.interrupted
  }

  const previousRevision = prev.inputDispositionRevision
  const serverRevision = server.inputDispositionRevision
  const staleServerDisposition = previousRevision !== undefined
    && (
      serverRevision === undefined
      || serverRevision < previousRevision
      || (
        serverRevision === previousRevision
        && TERMINAL_STEER_DISPOSITIONS.has(prev.inputDisposition || '')
        && server.inputDisposition !== prev.inputDisposition
      )
    )
  if ((!server.inputDisposition || staleServerDisposition) && prev.inputDisposition) {
    merged.inputDisposition = prev.inputDisposition
    merged.inputDispositionRevision = prev.inputDispositionRevision
    if (prev.turnId) merged.turnId = prev.turnId
  }
  if (!server.turnOutcome && prev.turnOutcome) merged.turnOutcome = prev.turnOutcome
  if (!server.activitySnapshot && prev.activitySnapshot) {
    merged.activitySnapshot = prev.activitySnapshot
    merged.activitySnapshotIncomplete = prev.activitySnapshotIncomplete
  }
  if (!server.turnInputMode && prev.turnInputMode) {
    merged.turnInputMode = prev.turnInputMode
  }
  if (!server.turnRunKind && prev.turnRunKind) {
    merged.turnRunKind = prev.turnRunKind
  }
  if (!server.steerClientRequestId && prev.steerClientRequestId) {
    merged.steerClientRequestId = prev.steerClientRequestId
  }
  if (!server.steerClientMessageId && prev.steerClientMessageId) {
    merged.steerClientMessageId = prev.steerClientMessageId
  }
  if (!server.promotedFromTurnId && prev.promotedFromTurnId) {
    merged.promotedFromTurnId = prev.promotedFromTurnId
  }
  if (prev.steerRestored) merged.steerRestored = true

  // Older gateways may return the canonical answer without the ordered local
  // timeline. Keep the just-finished snapshot so intermediate/answer roles and
  // their grouped call ids survive the immediate history replacement. A
  // non-empty server timeline remains authoritative.
  if (
    !server.activitySnapshot?.complete
    && (server.timeline?.length ?? 0) === 0
    && (prev.timeline?.length ?? 0) > 0
  ) {
    merged.timeline = prev.timeline?.map(segment => ({ ...segment }))
    if ((prev.tool_calls?.length ?? 0) > 0) {
      merged.tool_calls = prev.tool_calls?.map(call => ({ ...call }))
    }
  }

  // Approval/clarify interrupts are live event metadata. Canonical transcript
  // rows currently persist the surrounding text/tools but not these decisions,
  // so carry the in-flow timeline snapshot across the immediate history sync.
  if ((prev.interrupts?.length ?? 0) > 0 && (server.interrupts?.length ?? 0) === 0) {
    merged.interrupts = prev.interrupts
    if (prev.timeline?.some(segment => segment.type === 'interrupt')) {
      merged.timeline = prev.timeline
    }
  }

  return merged
}

// Server-authoritative merge: ordering and membership are exactly the incoming
// (server) window — rows the server dropped (e.g. via compaction) are not kept.
// For each server row that matches a prior row by a REAL messageId, ride the
// prior row's live-only fields along. Rows without a messageId, or with only a
// synthetic fallback key, take the server value verbatim (today's behavior).
export function reconcileHistoryMessages(prev: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  if (prev.length === 0) return incoming
  const prevById = new Map<string, ChatMessage>()
  for (const msg of prev) {
    if (msg.messageId) prevById.set(msg.messageId, msg)
  }
  const liveInterruptCandidates = prev.filter(message =>
    !message.messageId
    && (message.interrupts?.length ?? 0) > 0,
  )
  const incomingSignatureCounts = new Map<string, number>()
  for (const message of incoming) {
    const signature = `${message.role}\u0000${message.text}`
    incomingSignatureCounts.set(signature, (incomingSignatureCounts.get(signature) ?? 0) + 1)
  }
  return incoming.map(server => {
    const prior = server.messageId ? prevById.get(server.messageId) : undefined
    if (prior) {
      return mergeLiveOnlyFields(prior, server, { preserveTurnIdentity: true })
    }

    // The terminal stream row is optimistic and does not yet know the durable
    // message id. Graft only on a unique exact role/text match, which avoids
    // attaching one turn's approvals to repeated assistant content.
    const signature = `${server.role}\u0000${server.text}`
    if (incomingSignatureCounts.get(signature) !== 1) return server
    const candidates = liveInterruptCandidates.filter(candidate =>
      candidate.role === server.role && candidate.text === server.text)
    return candidates.length === 1
      ? mergeLiveOnlyFields(candidates[0], server)
      : server
  })
}

function turnEndIndex(messages: ChatMessage[], userIndex: number): number {
  const turnId = messages[userIndex]?.turnId
  let index = userIndex + 1
  while (index < messages.length) {
    const message = messages[index]
    if (
      message?.role === 'user'
      && (!turnId || !message.turnId || message.turnId !== turnId)
    ) break
    index++
  }
  return index
}

function assistantIndexesForTurn(messages: ChatMessage[], userIndex: number): number[] {
  const end = turnEndIndex(messages, userIndex)
  const indexes: number[] = []
  for (let index = userIndex + 1; index < end; index++) {
    if (messages[index]?.role === 'assistant') indexes.push(index)
  }
  return indexes
}

// A freshly completed assistant row has no durable id yet, so an id-only merge
// cannot preserve its live activity snapshot. Associate it only through the
// exact persisted user id that owns both turn slices, and only when each slice
// has one unambiguous assistant. This keeps the server authoritative while
// avoiding text/timestamp guesses across unrelated turns.
function reconcileOptimisticTurnFields(
  prev: ChatMessage[],
  incoming: ChatMessage[],
  consumedOptimisticRows?: Set<ChatMessage>,
): ChatMessage[] {
  const reconciled = reconcileHistoryMessages(prev, incoming)
  const merged = reconciled === incoming ? incoming.slice() : reconciled
  const previousUserIndexById = new Map<string, number>()

  prev.forEach((message, index) => {
    if (message.role === 'user' && message.messageId) {
      previousUserIndexById.set(message.messageId, index)
    }
  })

  incoming.forEach((message, incomingUserIndex) => {
    if (message.role !== 'user' || !message.messageId) return
    const previousUserIndex = previousUserIndexById.get(message.messageId)
    if (previousUserIndex === undefined) return

    const previousAssistants = assistantIndexesForTurn(prev, previousUserIndex)
      .filter(index => {
        const assistant = prev[index]
        return assistant?.restoredFromHistory !== true && !assistant?.messageId
      })
    const incomingAssistants = assistantIndexesForTurn(incoming, incomingUserIndex)
      .filter(index => {
        const assistant = incoming[index]
        return assistant?.restoredFromHistory === true && Boolean(assistant?.messageId)
      })
    if (previousAssistants.length !== 1 || incomingAssistants.length !== 1) return

    const previousAssistant = prev[previousAssistants[0]]
    const incomingAssistantIndex = incomingAssistants[0]
    const serverAssistant = merged[incomingAssistantIndex]
    merged[incomingAssistantIndex] = mergeLiveOnlyFields(
      previousAssistant,
      serverAssistant,
      { preserveTurnIdentity: true },
    )
    consumedOptimisticRows?.add(previousAssistant)
  })

  // Automatic Goal/heartbeat turns have no durable user row of their own, so
  // the user-owned turn heuristic above cannot associate their completed live
  // assistant with the canonical history row. Done and history both carry the
  // same server-issued turn id: use that identity plus exact role/text, but
  // only for a unique one-to-one match. The uniqueness fence deliberately
  // keeps repeated same-text rows within one turn rather than guessing, while
  // distinct turn ids remain independent even when their text is identical.
  const optimisticBySignature = new Map<string, ChatMessage[]>()
  const incomingSignatureCounts = new Map<string, number>()
  const assistantSignature = (message: ChatMessage): string | null => {
    if (message.role !== 'assistant' || !message.turnId) return null
    return `${message.turnId}\u0000${message.role}\u0000${message.text}`
  }

  for (const message of prev) {
    if (
      message.messageId
      || message.restoredFromHistory === true
      || consumedOptimisticRows?.has(message)
    ) continue
    const signature = assistantSignature(message)
    if (!signature) continue
    const candidates = optimisticBySignature.get(signature) ?? []
    candidates.push(message)
    optimisticBySignature.set(signature, candidates)
  }
  for (const message of incoming) {
    if (!message.messageId || message.restoredFromHistory !== true) continue
    const signature = assistantSignature(message)
    if (!signature) continue
    incomingSignatureCounts.set(
      signature,
      (incomingSignatureCounts.get(signature) ?? 0) + 1,
    )
  }
  incoming.forEach((message, index) => {
    if (!message.messageId || message.restoredFromHistory !== true) return
    const signature = assistantSignature(message)
    if (!signature || incomingSignatureCounts.get(signature) !== 1) return
    const candidates = optimisticBySignature.get(signature) ?? []
    if (candidates.length !== 1) return
    const optimistic = candidates[0]
    merged[index] = mergeLiveOnlyFields(optimistic, merged[index])
    consumedOptimisticRows?.add(optimistic)
  })

  return merged
}

// A background history sync returns only the newest server window. Keep any
// canonical pages the reader already loaded before that window, then replace
// the overlapping suffix with the fresh server rows. This keeps a 200-message
// sync from collapsing a longer transcript back to 200 rows while retaining
// the server-authoritative behavior inside the refreshed window.
export function historyWindowsOverlap(prev: ChatMessage[], incoming: ChatMessage[]): boolean {
  const previousIds = new Set(
    prev
      .filter(message => message.restoredFromHistory === true)
      .map(message => message.messageId)
      .filter(Boolean),
  )
  return incoming.some(message => Boolean(message.messageId && previousIds.has(message.messageId)))
}

export function reconcileHistoryWindow(prev: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  if (prev.length === 0) return incoming
  if (incoming.length === 0) return prev.filter(message => message.restoredFromHistory === true)

  const previousIndexById = new Map<string, number>()
  prev.forEach((message, index) => {
    if (message.restoredFromHistory === true && message.messageId) {
      previousIndexById.set(message.messageId, index)
    }
  })

  let overlapIndex = -1
  for (const message of incoming) {
    if (!message.messageId) continue
    const index = previousIndexById.get(message.messageId)
    if (index !== undefined) {
      overlapIndex = index
      break
    }
  }

  if (overlapIndex >= 0) {
    return [
      ...prev.slice(0, overlapIndex),
      ...reconcileOptimisticTurnFields(prev.slice(overlapIndex), incoming),
    ]
  }

  // With no shared message id there is no proof that these windows are
  // adjacent. Reset to the authoritative latest window instead of silently
  // rendering an omitted middle range as one continuous transcript. Callers
  // can page backwards again from the latest window's oldest cursor.
  return reconcileOptimisticTurnFields(prev, incoming)
}

function fallbackMessageKey(msg: ChatMessage): string {
  return `${msg.role}:${msg.ts || ''}:${msg.text || ''}`
}

function sameMessage(a: ChatMessage, b: ChatMessage): boolean {
  if (a.messageId && b.messageId) return a.messageId === b.messageId
  return fallbackMessageKey(a) === fallbackMessageKey(b)
}

function userTextKey(msg: ChatMessage): string {
  return String(msg.text || '').trim().replace(/\s+/g, ' ')
}

function userOccurrenceByText(messages: ChatMessage[], userIndex: number): number {
  const key = userTextKey(messages[userIndex])
  if (!key) return -1
  let occurrence = 0
  for (let i = 0; i <= userIndex; i++) {
    if (messages[i]?.role === 'user' && userTextKey(messages[i]) === key) occurrence++
  }
  return occurrence
}

function findUserByTextOccurrence(
  messages: ChatMessage[],
  user: ChatMessage,
  occurrence: number,
): number {
  const key = userTextKey(user)
  if (!key || occurrence <= 0) return -1
  let seen = 0
  for (let i = 0; i < messages.length; i++) {
    if (messages[i]?.role !== 'user' || userTextKey(messages[i]) !== key) continue
    seen++
    if (seen === occurrence) return i
  }
  return -1
}

function userOrdinal(messages: ChatMessage[], userIndex: number): number {
  let ordinal = 0
  for (let i = 0; i <= userIndex; i++) {
    if (messages[i]?.role === 'user') ordinal++
  }
  return ordinal
}

function findUserByOrdinal(messages: ChatMessage[], ordinal: number): number {
  if (ordinal <= 0) return -1
  let seen = 0
  for (let i = 0; i < messages.length; i++) {
    if (messages[i]?.role !== 'user') continue
    seen++
    if (seen === ordinal) return i
  }
  return -1
}

function findIncomingUserForPreviousNotice(
  previousMessages: ChatMessage[],
  incomingMessages: ChatMessage[],
  previousUserIndex: number,
): number {
  const previousUser = previousMessages[previousUserIndex]
  const exact = incomingMessages.findIndex(msg => sameMessage(msg, previousUser))
  if (exact >= 0) return exact

  const byTextOccurrence = findUserByTextOccurrence(
    incomingMessages,
    previousUser,
    userOccurrenceByText(previousMessages, previousUserIndex),
  )
  if (byTextOccurrence >= 0) return byTextOccurrence

  return findUserByOrdinal(incomingMessages, userOrdinal(previousMessages, previousUserIndex))
}

// A terminal replay has no future stream event to re-materialize its failure.
// Keep the client error beside its user turn until server history contains an
// error row for that turn; otherwise the required post-replay history sync
// would erase the only actionable explanation after ~50ms.
export function reconcileClientTerminalNotices(
  prev: ChatMessage[],
  incoming: ChatMessage[],
): ChatMessage[] {
  if (!prev.some(msg => msg.terminalNotice)) return incoming
  const merged = incoming.slice()

  for (let i = 0; i < prev.length; i++) {
    const notice = prev[i]
    if (!notice?.terminalNotice || notice.role !== 'error') continue

    const priorUserIndex = (() => {
      for (let j = i - 1; j >= 0; j--) {
        if (prev[j]?.role === 'user') return j
      }
      return -1
    })()
    if (priorUserIndex < 0) continue
    const userIndex = findIncomingUserForPreviousNotice(prev, merged, priorUserIndex)
    if (userIndex < 0) continue

    let turnEnd = userIndex + 1
    while (turnEnd < merged.length && merged[turnEnd]?.role !== 'user') turnEnd++
    const durableErrorExists = merged
      .slice(userIndex + 1, turnEnd)
      .some(message => message.role === 'error')
    if (durableErrorExists) continue

    // A retryable pre-provider failure can leave a status-only assistant with
    // no durable message id while the terminal task projection is still
    // catching up. Preserve that activity only when both snapshots prove the
    // same durable user id and exact turn id. Once canonical history carries a
    // same-turn status snapshot, it replaces this optimistic row naturally.
    const exactTurnId = notice.turnId?.trim()
    const previousUser = prev[priorUserIndex]
    const exactIncomingUserIndex = exactTurnId && previousUser?.messageId
      ? merged.findIndex(message =>
          message.role === 'user'
          && message.messageId === previousUser.messageId
          && message.turnId === exactTurnId,
        )
      : -1
    if (
      isUsageAccountingBarrier(notice.errorCode)
      && exactIncomingUserIndex >= 0
      && previousUser.turnId === exactTurnId
    ) {
      const optimisticActivities = prev.slice(priorUserIndex + 1, i).filter(message =>
        message.role === 'assistant'
        && message.turnId === exactTurnId
        && !message.messageId
        && (message.statusHistory?.length ?? 0) > 0,
      )
      const durableActivityExists = merged.some(message =>
        message.role === 'assistant'
        && message.turnId === exactTurnId
        && Boolean(message.messageId)
        && (message.statusHistory?.length ?? 0) > 0,
      )
      if (optimisticActivities.length === 1 && !durableActivityExists) {
        merged.splice(turnEnd, 0, optimisticActivities[0]!)
        turnEnd += 1
      }
    }
    merged.splice(turnEnd, 0, notice)
  }

  return merged
}

function lastUserIndex(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'user') return i
  }
  return -1
}

function insertionIndexForLiveTail(
  merged: ChatMessage[],
  previousLastUser: ChatMessage,
): number {
  if (previousLastUser.messageId) {
    const byId = merged.findIndex(msg => msg.messageId === previousLastUser.messageId)
    if (byId >= 0) return byId
  }
  const previousKey = fallbackMessageKey(previousLastUser)
  const byFallback = merged.findIndex(msg => fallbackMessageKey(msg) === previousKey)
  if (byFallback >= 0) return byFallback
  return lastUserIndex(merged)
}

// Running/live history sync is intentionally less server-authoritative than the
// settled reconcile above: a cold transcript snapshot may not yet contain the
// in-flight router strip, tool row, or partial assistant row that replay already
// rebuilt locally. Preserve the local tail after the last user until terminal
// sync makes the transcript authoritative again.
export function reconcileRunningHistoryMessages(
  prev: ChatMessage[],
  incoming: ChatMessage[],
): ChatMessage[] {
  if (prev.length === 0) return incoming
  if (incoming.length === 0) return prev

  const previousLastUserIndex = lastUserIndex(prev)
  if (previousLastUserIndex < 0) return reconcileHistoryMessages(prev, incoming)

  // A same-turn steer is another user row with the same explicit turn id. It
  // must not become the anchor that hides the live Router/tool/assistant tail
  // preceding it. Walk back to the first user row in that causal turn.
  let liveAnchorUserIndex = previousLastUserIndex
  const liveTurnId = prev[previousLastUserIndex]?.turnId
  if (liveTurnId) {
    for (let index = previousLastUserIndex - 1; index >= 0; index--) {
      const candidate = prev[index]
      if (candidate?.role !== 'user') continue
      if (candidate.turnId !== liveTurnId) break
      liveAnchorUserIndex = index
    }
  }

  const liveAnchor = prev[liveAnchorUserIndex]
  const incomingIds = new Set(incoming.map(message => message.messageId).filter(Boolean))
  const preserveConfirmedAnchor = Boolean(
    liveAnchor?.role === 'user'
    && liveAnchor.restoredFromHistory !== true
    && liveAnchor.messageId
    && !incomingIds.has(liveAnchor.messageId),
  )
  const liveTail = prev.slice(liveAnchorUserIndex + 1)
  if (liveTail.length === 0) {
    const merged = reconcileHistoryMessages(prev, incoming)
    // A mutation-confirmed user row can arrive locally after an older,
    // non-empty history request was already in flight. Its durable message id
    // proves that it is not an optimistic draft, so retain it until a later
    // canonical snapshot contains the same row.
    if (preserveConfirmedAnchor) {
      return [...merged, liveAnchor]
    }
    return merged
  }

  const consumedOptimisticRows = new Set<ChatMessage>()
  const merged = reconcileOptimisticTurnFields(prev, incoming, consumedOptimisticRows)
  const existingIds = new Set(merged.map(msg => msg.messageId).filter(Boolean))
  const existingFallbackKeys = new Set(merged.map(fallbackMessageKey))
  const confirmedAnchorToPreserve = (
    preserveConfirmedAnchor
    && liveAnchor.messageId
    && !existingIds.has(liveAnchor.messageId)
  ) ? [liveAnchor] : []
  const tailToPreserve = [...confirmedAnchorToPreserve, ...liveTail.filter(msg => {
    if (consumedOptimisticRows.has(msg)) return false
    if (msg.messageId) return !existingIds.has(msg.messageId)
    return !existingFallbackKeys.has(fallbackMessageKey(msg))
  })]
  if (tailToPreserve.length === 0) return merged
  if (confirmedAnchorToPreserve.length > 0) {
    // The incoming snapshot predates this server-confirmed input. Keep the
    // complete older transcript in place, then append the confirmed input and
    // its live tail in causal order.
    return [...merged, ...tailToPreserve]
  }

  const insertAfter = insertionIndexForLiveTail(merged, prev[liveAnchorUserIndex])
  if (insertAfter < 0) return [...merged, ...tailToPreserve]
  return [
    ...merged.slice(0, insertAfter + 1),
    ...tailToPreserve,
    ...merged.slice(insertAfter + 1),
  ]
}
