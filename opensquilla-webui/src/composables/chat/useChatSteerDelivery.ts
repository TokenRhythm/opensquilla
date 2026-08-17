import { watch, type Ref } from 'vue'

import type {
  ChatMessage,
  ChatPendingItem,
  ChatSteerDisposition,
  PendingSteerAttempt,
  PendingSteerPhase,
} from '@/types/chat'
import type { SessionSteerV2Params } from '@/types/rpc'
import { rehomePromotedSteerRows } from '@/utils/chat/historyMerge'

export interface SteerDeliveryIdentity {
  clientRequestId: string
  clientMessageId: string
  expectedTurnId: string
}

export interface SteerDeliveryEvidence extends Partial<SteerDeliveryIdentity> {
  disposition?: ChatSteerDisposition
  revision?: number
  userMessageId?: string
  turnId?: string
  promotedTurnId?: string
  promotedFromTurnId?: string
  appliedIteration?: number
  modelCallId?: string
}

export interface SteerDeliveryProjection {
  message?: ChatMessage
  pending?: ChatPendingItem
  text: string
  created: boolean
  stale: boolean
}

export interface UseChatSteerDeliveryOptions {
  messages: Ref<ChatMessage[]>
  pendingQueue: Ref<ChatPendingItem[]>
  checkpointForUserMessage?: (turnId: string) => void
  scheduleHistorySync: () => void
  removePendingItem?: (item: ChatPendingItem) => void
  restoreSteerIntoComposer?: (text: string) => void
  onProjected?: () => void
}

export interface ChatSteerDeliveryApi {
  attemptForItem: (item: ChatPendingItem) => PendingSteerAttempt | null
  begin: (item: ChatPendingItem, request?: SessionSteerV2Params) => PendingSteerAttempt | null
  markRetryable: (
    item: ChatPendingItem,
    phase: Exclude<PendingSteerPhase, 'submitting'>,
    error?: { code?: string; retryAfterMs?: number },
  ) => void
  accept: (
    evidence: SteerDeliveryEvidence,
    item?: ChatPendingItem,
  ) => SteerDeliveryProjection
  disposition: (
    evidence: SteerDeliveryEvidence,
    recovery?: { retryable?: boolean; hint?: string },
  ) => SteerDeliveryProjection
  fallback: (item: ChatPendingItem) => void
  reject: (item: ChatPendingItem, restore?: boolean) => void
  acknowledgeAcceptedOffscreen: (item: ChatPendingItem) => void
  markStopRequested: (turnId: string) => void
  reconcileDurableMessages: () => void
}

const TERMINAL_DISPOSITIONS = new Set<ChatSteerDisposition>([
  'applied',
  'promoted',
  'cancelled',
  'rejected',
])

export function snapshotSteerRequest(
  request: SessionSteerV2Params,
): Readonly<SessionSteerV2Params> {
  const source = request._source
    ? Object.freeze({ ...request._source })
    : undefined
  return Object.freeze({
    key: request.key,
    message: request.message,
    expected_turn_id: request.expected_turn_id,
    client_request_id: request.client_request_id,
    client_message_id: request.client_message_id,
    ...(request.pendingInputId ? { pendingInputId: request.pendingInputId } : {}),
    ...(request.requestFingerprint
      ? { requestFingerprint: request.requestFingerprint }
      : {}),
    ...(request.expectedRevision !== undefined
      ? { expectedRevision: request.expectedRevision }
      : {}),
    ...(request.surface_id ? { surface_id: request.surface_id } : {}),
    ...(source ? { _source: source } : {}),
  })
}

function attemptIdentity(attempt: PendingSteerAttempt): SteerDeliveryIdentity {
  return {
    clientRequestId: attempt.request.client_request_id,
    clientMessageId: attempt.request.client_message_id,
    expectedTurnId: attempt.request.expected_turn_id,
  }
}

function matchesIdentity(
  message: ChatMessage,
  identity: Partial<SteerDeliveryIdentity> & { userMessageId?: string },
): boolean {
  return Boolean(
    (identity.clientMessageId && (
      message.clientId === identity.clientMessageId
      || message.steerClientMessageId === identity.clientMessageId
    ))
    || (identity.clientRequestId && message.steerClientRequestId === identity.clientRequestId)
    || (identity.userMessageId && message.messageId === identity.userMessageId),
  )
}

function matchesPending(
  item: ChatPendingItem,
  identity: Partial<SteerDeliveryIdentity>,
): boolean {
  const attempt = item.steerAttempt
  const request = attempt?.request
  return Boolean(attempt && (
    (identity.clientRequestId && request?.client_request_id === identity.clientRequestId)
    || (identity.clientMessageId && request?.client_message_id === identity.clientMessageId)
  ))
}

export function useChatSteerDelivery(
  options: UseChatSteerDeliveryOptions,
): ChatSteerDeliveryApi {
  const restoredAttemptIds = new Set<string>()
  const checkpointedAttemptIds = new Set<string>()

  function checkpointOnce(attempt: PendingSteerAttempt) {
    const request = attempt.request
    const key = request.client_request_id || request.client_message_id
    if (!key || checkpointedAttemptIds.has(key)) return
    options.checkpointForUserMessage?.(request.expected_turn_id)
    checkpointedAttemptIds.add(key)
  }

  function attemptForItem(item: ChatPendingItem): PendingSteerAttempt | null {
    return item.steerAttempt || null
  }

  function begin(
    item: ChatPendingItem,
    request?: SessionSteerV2Params,
  ): PendingSteerAttempt | null {
    const existing = item.steerAttempt
    const source = request
      ? snapshotSteerRequest(request)
      : existing?.request
    if (!source) return null
    const attempt: PendingSteerAttempt = {
      phase: 'submitting',
      request: source,
      ...(existing?.stopRequested ? { stopRequested: true } : {}),
    }
    item.steerAttempt = attempt
    // `deliveryState` remains the generic queue/hidden-control lease. Once an
    // item is a Steer, its phase is the single source of delivery truth.
    item.deliveryState = undefined
    // `item` can be the raw object that was just inserted into a Vue array;
    // reconcile explicitly instead of relying only on proxy observation.
    reconcileDurableMessages()
    return attempt
  }

  function markRetryable(
    item: ChatPendingItem,
    phase: Exclude<PendingSteerPhase, 'submitting'>,
    error: { code?: string; retryAfterMs?: number } = {},
  ) {
    const attempt = item.steerAttempt
    if (!attempt) return
    item.steerAttempt = {
      ...attempt,
      phase,
      ...(error.code ? { errorCode: error.code } : {}),
      ...(error.retryAfterMs !== undefined ? { retryAfterMs: error.retryAfterMs } : {}),
    }
    item.deliveryState = undefined
  }

  function restoreOnce(item: ChatPendingItem, message?: ChatMessage) {
    const request = item.steerAttempt?.request
    const key = request?.client_request_id || request?.client_message_id || ''
    restoreTextOnce(key, item.text, message)
  }

  function restoreTextOnce(key: string, text: string, message?: ChatMessage) {
    if (!key || restoredAttemptIds.has(key) || message?.steerRestored) return
    if (text) options.restoreSteerIntoComposer?.(text)
    restoredAttemptIds.add(key)
    if (message) message.steerRestored = true
  }

  function removePending(item: ChatPendingItem) {
    if (options.removePendingItem) {
      options.removePendingItem(item)
      return
    }
    const index = options.pendingQueue.value.indexOf(item)
    if (index >= 0) options.pendingQueue.value.splice(index, 1)
  }

  function accept(
    evidence: SteerDeliveryEvidence,
    suppliedItem?: ChatPendingItem,
  ): SteerDeliveryProjection {
    const pending = suppliedItem
      || options.pendingQueue.value.find(item => matchesPending(item, evidence))
    const attempt = pending?.steerAttempt || null
    const pendingIdentity = attempt ? attemptIdentity(attempt) : null
    const identity: SteerDeliveryIdentity = {
      clientRequestId: evidence.clientRequestId || pendingIdentity?.clientRequestId || '',
      clientMessageId: evidence.clientMessageId || pendingIdentity?.clientMessageId || '',
      expectedTurnId: evidence.expectedTurnId || pendingIdentity?.expectedTurnId || '',
    }
    let message = options.messages.value.find(candidate => matchesIdentity(candidate, {
      ...identity,
      userMessageId: evidence.userMessageId,
    }))
    let created = false
    if (!message && pending && (identity.clientRequestId || identity.clientMessageId)) {
      if (attempt) checkpointOnce(attempt)
      message = {
        role: 'user',
        text: pending.text,
        ts: new Date().toISOString(),
        clientId: identity.clientMessageId || undefined,
        turnId: identity.expectedTurnId || undefined,
        inputDisposition: evidence.disposition || 'steering',
        steerClientRequestId: identity.clientRequestId || undefined,
        steerClientMessageId: identity.clientMessageId || undefined,
        ...(attempt?.stopRequested ? { steerStopRequested: true } : {}),
      }
      options.messages.value.push(message)
      created = true
    }

    const rawRevision = Number(evidence.revision)
    const incomingRevision = Number.isInteger(rawRevision) && rawRevision >= 0
      ? rawRevision
      : undefined
    const currentRevision = message?.inputDispositionRevision
    const currentTerminal = message?.inputDisposition
      ? TERMINAL_DISPOSITIONS.has(message.inputDisposition)
      : false
    const disposition = evidence.disposition || 'steering'
    const stale = Boolean(message) && (
      (
        currentRevision !== undefined
        && (incomingRevision === undefined || incomingRevision < currentRevision)
      )
      || (
        currentTerminal
        && disposition !== message?.inputDisposition
        && (
          incomingRevision === undefined
          || (currentRevision !== undefined && incomingRevision <= currentRevision)
        )
      )
    )

    if (message && !stale) {
      message.inputDisposition = disposition
      if (incomingRevision !== undefined) message.inputDispositionRevision = incomingRevision
      if (evidence.userMessageId) message.messageId = evidence.userMessageId
      if (identity.clientRequestId) message.steerClientRequestId = identity.clientRequestId
      if (identity.clientMessageId) message.steerClientMessageId = identity.clientMessageId
      const targetTurnId = evidence.promotedTurnId
        || evidence.turnId
        || identity.expectedTurnId
      if (targetTurnId) message.turnId = targetTurnId
      if (evidence.appliedIteration !== undefined) {
        message.steerAppliedIteration = evidence.appliedIteration
      }
      if (evidence.modelCallId) message.steerModelCallId = evidence.modelCallId
      if (disposition === 'promoted') {
        message.promotedFromTurnId = evidence.promotedFromTurnId
          || identity.expectedTurnId
          || undefined
        options.messages.value = rehomePromotedSteerRows(options.messages.value)
      }
      if (TERMINAL_DISPOSITIONS.has(disposition)) message.steerStopRequested = false
    }

    // The exact durable row/event/accepted response supersedes pending UI even
    // when its revision is older than a row already on screen.
    if (pending) removePending(pending)
    if (pending && (disposition === 'cancelled')) restoreOnce(pending, message)
    if ((message || pending) && (!stale || pending)) {
      options.scheduleHistorySync()
      options.onProjected?.()
    }
    return {
      message,
      pending,
      text: pending?.text || message?.text || '',
      created,
      stale,
    }
  }

  function fallback(item: ChatPendingItem) {
    delete item.steerAttempt
    item.deliveryState = undefined
  }

  function disposition(
    evidence: SteerDeliveryEvidence,
    recovery: { retryable?: boolean; hint?: string } = {},
  ): SteerDeliveryProjection {
    const pending = options.pendingQueue.value.find(item => matchesPending(item, evidence))
    const state = evidence.disposition
    const projection = accept(evidence, pending)
    if (!projection.message && !projection.pending) {
      // The event can beat both local pending hydration and canonical history.
      // Pull history so the durable row is still rendered after reconnect.
      options.scheduleHistorySync()
    }
    const shouldRestore = state === 'cancelled'
      || (
        state === 'rejected'
        && (
          recovery.retryable
          || /retry|resend|restore/i.test(recovery.hint || '')
        )
      )
    if (shouldRestore) {
      if (pending) restoreOnce(pending, projection.message)
      else {
        restoreTextOnce(
          evidence.clientRequestId
            || evidence.clientMessageId
            || evidence.userMessageId
            || '',
          projection.message?.text || '',
          projection.message,
        )
      }
    }
    return projection
  }

  function reject(item: ChatPendingItem, restore = true) {
    if (restore) restoreOnce(item)
    removePending(item)
  }

  function acknowledgeAcceptedOffscreen(item: ChatPendingItem) {
    // Durable acceptance belongs to the source session. Remove its local
    // transport projection without creating a bubble in the newly selected
    // session; canonical history will render it when the source is revisited.
    removePending(item)
  }

  function markStopRequested(turnId: string) {
    for (const item of options.pendingQueue.value) {
      const attempt = item.steerAttempt
      if (!attempt) continue
      if (turnId && attempt.request.expected_turn_id !== turnId) continue
      item.steerAttempt = { ...attempt, stopRequested: true }
    }
  }

  function reconcileDurableMessages() {
    for (const item of [...options.pendingQueue.value]) {
      const attempt = item.steerAttempt
      if (!attempt) continue
      const message = options.messages.value.find(candidate => (
        matchesIdentity(candidate, attemptIdentity(attempt))
      ))
      if (!message) continue
      checkpointOnce(attempt)
      removePending(item)
    }
  }

  // Reconciliation is symmetric: reconnect hydration may restore history
  // before a parked pending queue is reattached, or vice versa.
  watch(
    [options.messages, options.pendingQueue],
    reconcileDurableMessages,
    { deep: true, flush: 'sync' },
  )

  return {
    attemptForItem,
    begin,
    markRetryable,
    accept,
    disposition,
    fallback,
    reject,
    acknowledgeAcceptedOffscreen,
    markStopRequested,
    reconcileDurableMessages,
  }
}
