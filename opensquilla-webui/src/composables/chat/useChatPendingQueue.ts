import { computed, nextTick, ref, watch, type Ref } from 'vue'
import type {
  Attachment,
  ChatPendingItem,
  HiddenControlDispatchResult,
} from '@/types/chat'

const MAX_PENDING = 5

export type BusySendMode = 'queue' | 'steer'
export type PendingDeliveryOutcome =
  | 'accepted'
  | 'deferred'
  | 'not_sent'
  | 'retryable_failure'

export interface PendingQueueOwner {
  ownerRequestId?: string
}

export interface PendingQueueOwnerContext {
  sessionKey: string
  ownerRequestId: string
}

export interface PendingQueuePayload {
  text: string
  attachments?: Attachment[]
  intent?: string | null
}

export interface PendingSteerRetryPayload {
  text: string
  clientRequestId: string
  clientMessageId: string
  expectedTurnId: string
  visibleCommitted: boolean
}

export interface UseChatPendingQueueOptions {
  sessionKey: Ref<string>
  ownerContext?: Readonly<Ref<PendingQueueOwnerContext | null>>
  inputText: Ref<string>
  pendingAttachments: Ref<Attachment[]>
  pendingSessionIntent: Ref<string | null>
  isStreaming: Ref<boolean>
  isBlocked: () => boolean
  autoResizeTextarea: () => void
  sendCurrentInput: () => void
  resetInputHistory: () => void
  hasComposer: () => boolean
  // Drain a queued hidden-control send (e.g. meta-preflight confirmation)
  // directly through the dedicated hidden-send path instead of the composer.
  dispatchHiddenControl?: (
    item: ChatPendingItem,
    ownerSessionKey: string,
  ) => Promise<PendingDeliveryOutcome>
  // Returning false for an explicit discard keeps the chip queued. This lets
  // the caller fail closed when it cannot persist the cancellation tombstone.
  onHiddenControlDispatchResult?: (result: HiddenControlDispatchResult) => void | boolean
  // The WebUI drains visible queue items through the same composer-preserving
  // transport used by explicit Steer. The legacy callback remains as a
  // fallback for isolated composable consumers.
  dispatchPendingItem?: (
    item: ChatPendingItem,
    ownerSessionKey: string,
  ) => Promise<PendingDeliveryOutcome>
}

export function useChatPendingQueue(options: UseChatPendingQueueOptions) {
  const pendingQueue = ref<ChatPendingItem[]>([])
  const parkedQueues = new Map<string, ChatPendingItem[]>()
  let pendingDrainTimer: ReturnType<typeof setTimeout> | null = null
  let deferredDrainRequested = false

  const canQueueMore = computed(() => pendingQueue.value.length < MAX_PENDING)
  // A direct queued delivery owns its item until it succeeds, is removed, or
  // becomes eligible for another explicit retry.
  const hasDeliveryBarrier = computed(() =>
    pendingQueue.value.some(
      item => item.deliveryState === 'steering' || item.deliveryState === 'retryable',
    ),
  )

  // Busy-composer delivery mode: 'queue' holds the message until the turn
  // ends (pending queue), 'steer' sends it immediately into the active run.
  // The choice only applies while a run is active, so it snaps back to the
  // safe default whenever streaming stops.
  const busySendMode = ref<BusySendMode>('queue')
  watch(options.isStreaming, (streaming) => {
    if (!streaming) {
      busySendMode.value = 'queue'
      flushDeferredPendingDrain()
    }
  })
  watch(hasDeliveryBarrier, (blocked, wasBlocked) => {
    if (blocked) {
      cancelPendingDrainTimer()
    } else if (wasBlocked) {
      flushDeferredPendingDrain()
    }
  })

  function resolveOwnerRequestId(owner?: PendingQueueOwner): string | undefined {
    if (owner?.ownerRequestId) return owner.ownerRequestId
    const context = options.ownerContext?.value
    return context?.sessionKey === options.sessionKey.value
      ? context.ownerRequestId
      : undefined
  }

  function enqueuePendingPayload(
    payload: PendingQueuePayload,
    owner?: PendingQueueOwner,
  ) {
    if (pendingQueue.value.length >= MAX_PENDING) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return false
    }
    const ownerRequestId = resolveOwnerRequestId(owner)
    pendingQueue.value.push({
      text: payload.text,
      attachments: (payload.attachments || []).map(a => ({ ...a })),
      intent: payload.intent ?? null,
      ownerSessionKey: options.sessionKey.value,
      ...(ownerRequestId ? { ownerRequestId } : {}),
    })
    flushDeferredPendingDrain()
    return true
  }

  function enqueuePendingInput(text: string, owner?: PendingQueueOwner) {
    const queued = enqueuePendingPayload({
      text,
      attachments: options.pendingAttachments.value,
      intent: options.pendingSessionIntent.value,
    }, owner)
    if (!queued) return false
    options.inputText.value = ''
    options.pendingAttachments.value = []
    options.pendingSessionIntent.value = null
    options.autoResizeTextarea()
    return true
  }

  function enqueueRecoveredInput(text: string, owner?: PendingQueueOwner) {
    const recovered = String(text || '').trim()
    if (!recovered) return true
    if (pendingQueue.value.some(item => !item.hiddenControl && item.text === recovered)) {
      return true
    }
    return enqueuePendingPayload({ text: recovered }, owner)
  }

  function enqueueHiddenControl(
    item: {
      text: string
      displayText: string
      clientRequestId?: string
      sessionKey?: string
      clientMessageId?: string
      visibleCommitted?: boolean
    },
    owner?: PendingQueueOwner,
  ) {
    const stableRequestId = String(item.clientRequestId || '').trim()
    const hiddenControlSessionKey = item.sessionKey || options.sessionKey.value
    if (
      stableRequestId
      && pendingQueue.value.some(candidate => (
        candidate.hiddenControl
        && candidate.clientRequestId === stableRequestId
        && candidate.hiddenControlSessionKey === hiddenControlSessionKey
      ))
    ) return true
    if (pendingQueue.value.length >= MAX_PENDING) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return false
    }
    // A hidden-control send does NOT consume the composer draft/attachments.
    const ownerRequestId = resolveOwnerRequestId(owner)
    pendingQueue.value.push({
      text: item.text,
      attachments: [],
      intent: null,
      ownerSessionKey: options.sessionKey.value,
      ...(ownerRequestId ? { ownerRequestId } : {}),
      hiddenControl: true,
      displayTextOverride: item.displayText,
      clientRequestId: item.clientRequestId,
      hiddenControlSessionKey,
      ...(item.clientRequestId
        ? { hiddenClientRequestId: item.clientRequestId }
        : {}),
      ...(item.clientMessageId
        ? { hiddenClientMessageId: item.clientMessageId }
        : {}),
      ...(item.visibleCommitted ? { hiddenVisibleCommitted: true } : {}),
    })
    flushDeferredPendingDrain()
    return true
  }

  function enqueuePendingSteerRetry(payload: PendingSteerRetryPayload) {
    if (pendingQueue.value.length >= MAX_PENDING) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return false
    }
    pendingQueue.value.push({
      text: payload.text,
      attachments: [],
      intent: null,
      ownerSessionKey: options.sessionKey.value,
      deliveryState: 'retryable',
      steerClientRequestId: payload.clientRequestId,
      steerClientMessageId: payload.clientMessageId,
      steerExpectedTurnId: payload.expectedTurnId,
      steerVisibleCommitted: payload.visibleCommitted,
    })
    return true
  }

  function removePendingChip(index: number) {
    const item = pendingQueue.value[index]
    if (!item || item.deliveryState === 'steering') return false
    if (!notifyDiscardedHiddenControl(item)) return false
    pendingQueue.value.splice(index, 1)
    return true
  }

  function beginPendingDelivery(
    index: number,
    allowHiddenControl = false,
  ): ChatPendingItem | null {
    const item = pendingQueue.value[index]
    if (
      !item
      || (item.hiddenControl && !allowHiddenControl)
      || item.deliveryState === 'steering'
    ) return null
    const otherDelivery = pendingQueue.value.find(
      candidate => candidate !== item && candidate.deliveryState,
    )
    if (otherDelivery) return null
    item.deliveryState = 'steering'
    return item
  }

  function settlePendingDelivery(item: ChatPendingItem, outcome: PendingDeliveryOutcome) {
    let container = pendingQueue.value
    let index = container.indexOf(item)
    if (index < 0) {
      for (const parked of parkedQueues.values()) {
        const parkedIndex = parked.indexOf(item)
        if (parkedIndex < 0) continue
        container = parked
        index = parkedIndex
        break
      }
    }
    // Explicit navigation intentionally discards the old queue. If that
    // happened while an RPC settled, there is no queue ownership left to
    // update.
    if (index < 0) return
    if (outcome === 'accepted') {
      container.splice(index, 1)
      flushDeferredPendingDrain()
      return
    }
    if (outcome === 'deferred') {
      item.deliveryState = undefined
      deferredDrainRequested = true
      flushDeferredPendingDrain()
      return
    }
    item.deliveryState = outcome === 'retryable_failure' ? 'retryable' : undefined
    flushDeferredPendingDrain()
  }

  function clearPendingQueue() {
    clearPendingDrainAfterTerminalTimer()
    pendingQueue.value = pendingQueue.value.filter(
      item => item.deliveryState === 'steering' || !notifyDiscardedHiddenControl(item),
    )
  }

  function notifyDiscardedHiddenControl(item?: ChatPendingItem): boolean {
    if (!item?.hiddenControl || !item.clientRequestId) return true
    const result = options.onHiddenControlDispatchResult?.({
      status: 'rejected',
      reason: 'discarded',
      clientRequestId: item.clientRequestId,
      sessionKey: item.hiddenControlSessionKey || '',
    })
    return result !== false
  }

  function switchPendingQueue(targetSessionKey: string) {
    clearPendingDrainAfterTerminalTimer()
    const sourceSessionKey = options.sessionKey.value
    const sourceHidden = pendingQueue.value.filter(item => item.hiddenControl)
    if (sourceSessionKey && sourceHidden.length > 0) {
      const existing = parkedQueues.get(sourceSessionKey) || []
      parkedQueues.set(sourceSessionKey, [...existing, ...sourceHidden])
    }
    const restored = parkedQueues.get(targetSessionKey) || []
    parkedQueues.delete(targetSessionKey)
    // Explicit navigation keeps its historical behavior of discarding the
    // active session's ordinary queue. Machine controls remain bound to their
    // source session and resume with the same durable request identity.
    pendingQueue.value = restored
  }

  function adoptPendingQueue(targetSessionKey: string, ownerRequestId: string) {
    clearPendingDrainAfterTerminalTimer()
    const sourceSessionKey = options.sessionKey.value
    const carried: ChatPendingItem[] = []
    const stayingVisible: ChatPendingItem[] = []
    const stayingHidden: ChatPendingItem[] = []
    for (const item of pendingQueue.value) {
      if (item.hiddenControl) {
        stayingHidden.push(item)
        continue
      }
      if (
        ownerRequestId
        && item.ownerSessionKey === sourceSessionKey
        && item.ownerRequestId === ownerRequestId
      ) {
        // Keep object identity: an in-flight explicit steer stores its
        // idempotent retry attempt against this exact queue item.
        item.ownerSessionKey = targetSessionKey
        item.ownerRequestId = undefined
        carried.push(item)
      } else {
        stayingVisible.push(item)
      }
    }
    if (stayingVisible.length > 0 || stayingHidden.length > 0) {
      parkedQueues.set(sourceSessionKey, [
        ...(parkedQueues.get(sourceSessionKey) || []),
        ...stayingVisible,
        ...stayingHidden,
      ])
    }
    const targetItems = parkedQueues.get(targetSessionKey) || []
    parkedQueues.delete(targetSessionKey)
    pendingQueue.value = [...targetItems, ...carried]
  }

  function popPendingTail() {
    // Hidden controls and explicit/ambiguous steer deliveries must retain
    // their own transport identity instead of being converted into a fresh
    // composer send.
    let tailIndex = pendingQueue.value.length - 1
    while (
      tailIndex >= 0
      && (
        pendingQueue.value[tailIndex]?.hiddenControl
        || pendingQueue.value[tailIndex]?.deliveryState
      )
    ) tailIndex--
    if (tailIndex < 0) return false
    const [tail] = pendingQueue.value.splice(tailIndex, 1)
    options.inputText.value = tail?.text || ''
    options.pendingAttachments.value = tail?.attachments || []
    options.pendingSessionIntent.value = tail?.intent || null
    options.autoResizeTextarea()
    return true
  }

  function popAllPendingIntoComposer(): boolean {
    clearPendingDrainAfterTerminalTimer()
    if (!options.hasComposer() || pendingQueue.value.length === 0) return false
    // Hidden controls and explicit/ambiguous steer deliveries stay queued;
    // only transport-free visible drafts can safely return to the composer.
    const visible = pendingQueue.value.filter(p => !p.hiddenControl && !p.deliveryState)
    const retained = pendingQueue.value.filter(p => p.hiddenControl || p.deliveryState)
    if (visible.length === 0) return false
    const queuedTexts = visible.map(p => p.text).filter(Boolean)
    const queuedAttachments = visible.flatMap(p => p.attachments || [])
    const headIntent = visible[0]?.intent
    const current = options.inputText.value || ''
    const joined = [current, ...queuedTexts].filter(Boolean).join('\n')
    pendingQueue.value = retained
    options.inputText.value = joined
    options.pendingAttachments.value = [...options.pendingAttachments.value, ...queuedAttachments]
    options.pendingSessionIntent.value = options.pendingSessionIntent.value || headIntent || null
    options.autoResizeTextarea()
    options.resetInputHistory()
    return true
  }

  function drainQueueHead() {
    clearPendingDrainAfterTerminalTimer()
    if (pendingQueue.value.length === 0) return
    const head = pendingQueue.value[0]
    const ownerSessionKey = head?.ownerSessionKey || options.sessionKey.value
    if (ownerSessionKey !== options.sessionKey.value) {
      if (head) head.deliveryState = 'retryable'
      return
    }
    if (head?.hiddenControl) {
      head.deliveryState = 'steering'
      // Hidden-control sends bypass the composer entirely, but retain their
      // queue lease until the transport confirms acceptance.
      nextTick(() => {
        void (async () => {
          let outcome: PendingDeliveryOutcome = 'retryable_failure'
          try {
            if (options.sessionKey.value === ownerSessionKey) {
              outcome = await options.dispatchHiddenControl?.(
                head,
                ownerSessionKey,
              ) ?? 'retryable_failure'
            }
          } catch {
            outcome = 'retryable_failure'
          } finally {
            if (head.clientRequestId) {
              options.onHiddenControlDispatchResult?.({
                status: outcome === 'accepted'
                  ? 'accepted'
                  : outcome === 'not_sent'
                    ? 'rejected'
                    : 'unknown',
                reason: outcome === 'accepted'
                  ? 'accepted'
                  : outcome === 'not_sent'
                    ? 'send_rejected'
                    : 'response_unknown',
                clientRequestId: head.clientRequestId,
                sessionKey: head.hiddenControlSessionKey || ownerSessionKey,
              })
            }
            settlePendingDelivery(head, outcome)
          }
        })()
      })
      return
    }
    if (options.dispatchPendingItem) {
      const item = beginPendingDelivery(0)
      if (!item) return
      nextTick(() => {
        void (async () => {
          let outcome: PendingDeliveryOutcome = 'retryable_failure'
          try {
            if (options.sessionKey.value === ownerSessionKey) {
              outcome = await options.dispatchPendingItem!(item, ownerSessionKey)
            }
          } catch {
            // Keep the queue item as an explicit idempotent retry. The send
            // layer normally converts transport errors to this outcome, but
            // the queue must also fail closed if an unexpected error escapes.
            outcome = 'retryable_failure'
          } finally {
            settlePendingDelivery(item, outcome)
          }
        })()
      })
      return
    }
    if (!head) return
    head.deliveryState = 'steering'
    nextTick(() => {
      if (
        options.sessionKey.value !== ownerSessionKey
        || pendingQueue.value[0] !== head
      ) return
      pendingQueue.value.shift()
      options.inputText.value = head.text || ''
      options.pendingAttachments.value = head.attachments || []
      options.pendingSessionIntent.value = head.intent || null
      options.sendCurrentInput()
    })
  }

  function schedulePendingDrainAfterTerminal() {
    if (pendingQueue.value.length === 0) {
      // A terminal subscription replay can arrive while response handoff is
      // still hydrating, before the matching follow-up reaches the queue.
      // Preserve that terminal signal until the blocker releases.
      deferredDrainRequested = options.isBlocked()
      return
    }
    deferredDrainRequested = true
    if (hasDeliveryBarrier.value) return
    armPendingDrainTimer()
  }

  function armPendingDrainTimer() {
    cancelPendingDrainTimer()
    if (hasDeliveryBarrier.value) return
    pendingDrainTimer = setTimeout(() => {
      pendingDrainTimer = null
      if (pendingQueue.value.length === 0) {
        deferredDrainRequested = false
        return
      }
      if (options.isStreaming.value || options.isBlocked() || hasDeliveryBarrier.value) return
      deferredDrainRequested = false
      drainQueueHead()
    }, 50)
  }

  function flushDeferredPendingDrain() {
    if (
      !deferredDrainRequested
      || pendingQueue.value.length === 0
      || hasDeliveryBarrier.value
    ) return
    armPendingDrainTimer()
  }

  function cancelPendingDrainTimer() {
    if (pendingDrainTimer) {
      clearTimeout(pendingDrainTimer)
      pendingDrainTimer = null
    }
  }

  function clearPendingDrainAfterTerminalTimer() {
    cancelPendingDrainTimer()
    deferredDrainRequested = false
  }

  function cleanup() {
    clearPendingDrainAfterTerminalTimer()
    parkedQueues.clear()
  }

  return {
    pendingQueue,
    canQueueMore,
    busySendMode,
    maxPending: MAX_PENDING,
    enqueuePendingPayload,
    enqueuePendingInput,
    enqueueRecoveredInput,
    enqueueHiddenControl,
    enqueuePendingSteerRetry,
    removePendingChip,
    beginPendingDelivery,
    settlePendingDelivery,
    clearPendingQueue,
    switchPendingQueue,
    adoptPendingQueue,
    popPendingTail,
    popAllPendingIntoComposer,
    schedulePendingDrainAfterTerminal,
    flushDeferredPendingDrain,
    clearPendingDrainAfterTerminalTimer,
    cleanup,
  }
}
