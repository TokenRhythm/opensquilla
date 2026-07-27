import type { ChatMessage } from '@/types/chat'

// Live-only fields are written onto a row AFTER it is pushed (reasoning seconds
// from the done backfill, routerSettled from the router runtime, interrupted
// from a local Stop) and are absent from a fresh history map. Re-apply them
// when the server snapshot lacks a richer value, keyed strictly by messageId so
// a synthetic-key collision can never graft one turn's state onto another.
export function mergeLiveOnlyFields(prev: ChatMessage, server: ChatMessage): ChatMessage {
  const merged: ChatMessage = { ...server }

  // Keep the optimistic row identity after the backend assigns a durable
  // message id. Per-turn render keys use it to avoid remounting live surfaces
  // during the first authoritative history replacement.
  if (!server.clientId && prev.clientId) merged.clientId = prev.clientId

  // reasoning: server wins if it measured seconds; else keep the live seconds.
  const serverSeconds = prev.role === 'assistant' ? server.reasoning?.seconds ?? 0 : 0
  if (serverSeconds <= 0 && (prev.reasoning?.seconds ?? 0) > 0) {
    merged.reasoning = prev.reasoning
  }

  // The fold's phase snapshot supplies an exact same-session activity start.
  // History does not persist it, so retain the local snapshot across the first
  // authoritative refresh; a cold reload still correctly falls back to counts.
  if (!(server.statusHistory?.length) && (prev.statusHistory?.length ?? 0) > 0) {
    merged.statusHistory = prev.statusHistory
  }

  // routerSettled is sticky: once a strip has settled it stays settled.
  if (prev.routerSettled) merged.routerSettled = true

  // interrupted: keep the local abort flag until the server persists its own.
  if (server.interrupted === undefined && prev.interrupted) {
    merged.interrupted = prev.interrupted
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
  if (prevById.size === 0) return incoming
  return incoming.map(server => {
    if (!server.messageId) return server
    const prior = prevById.get(server.messageId)
    return prior ? mergeLiveOnlyFields(prior, server) : server
  })
}

function turnEndIndex(messages: ChatMessage[], userIndex: number): number {
  let index = userIndex + 1
  while (index < messages.length && messages[index]?.role !== 'user') index++
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
    merged[incomingAssistantIndex] = mergeLiveOnlyFields(previousAssistant, serverAssistant)
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

function findUserByTextOccurrence(messages: ChatMessage[], user: ChatMessage, occurrence: number): number {
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

function assistantHasVisibleOutput(message: ChatMessage): boolean {
  return Boolean(
    String(message.text || '').trim() ||
    message.reasoning?.text ||
    message.attachments?.length ||
    message.artifacts?.length ||
    message.tool_calls?.length ||
    message.timeline?.length ||
    message.statusHistory?.length,
  )
}

function turnHasServerOutputAfterUser(messages: ChatMessage[], userIndex: number): boolean {
  for (let i = userIndex + 1; i < messages.length; i++) {
    const msg = messages[i]
    if (!msg) continue
    if (msg.role === 'user') return false
    if (msg.role === 'router') continue
    if (msg.role === 'assistant' && !assistantHasVisibleOutput(msg)) continue
    return true
  }
  return false
}

function stopNoticeInsertionIndex(messages: ChatMessage[], userIndex: number): number {
  let index = userIndex + 1
  while (index < messages.length && messages[index]?.role === 'router') index++
  return index
}

export function reconcileClientStopNotices(prev: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  if (!prev.some(msg => msg.stopNotice)) return incoming
  const merged = incoming.slice()

  for (let i = 0; i < prev.length; i++) {
    const notice = prev[i]
    if (!notice?.stopNotice) continue
    if (merged.some(msg => sameMessage(msg, notice))) continue

    const priorUserIndex = (() => {
      for (let j = i - 1; j >= 0; j--) {
        if (prev[j]?.role === 'user') return j
      }
      return -1
    })()
    if (priorUserIndex < 0) continue
    const userIndex = findIncomingUserForPreviousNotice(prev, merged, priorUserIndex)
    if (userIndex < 0) continue
    if (turnHasServerOutputAfterUser(merged, userIndex)) continue

    merged.splice(stopNoticeInsertionIndex(merged, userIndex), 0, notice)
  }

  return merged
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

  const liveTail = prev.slice(previousLastUserIndex + 1)
  if (liveTail.length === 0) return reconcileHistoryMessages(prev, incoming)

  const merged = reconcileHistoryMessages(prev, incoming)
  const existingIds = new Set(merged.map(msg => msg.messageId).filter(Boolean))
  const existingFallbackKeys = new Set(merged.map(fallbackMessageKey))
  const tailToPreserve = liveTail.filter(msg => {
    if (msg.messageId) return !existingIds.has(msg.messageId)
    return !existingFallbackKeys.has(fallbackMessageKey(msg))
  })
  if (tailToPreserve.length === 0) return merged

  const insertAfter = insertionIndexForLiveTail(merged, prev[previousLastUserIndex])
  if (insertAfter < 0) return [...merged, ...tailToPreserve]
  return [
    ...merged.slice(0, insertAfter + 1),
    ...tailToPreserve,
    ...merged.slice(insertAfter + 1),
  ]
}
