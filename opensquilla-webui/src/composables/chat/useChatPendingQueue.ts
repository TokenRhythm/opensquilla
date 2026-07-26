import { computed, nextTick, ref, watch, type Ref } from 'vue'
import type { Attachment, ChatPendingItem } from '@/types/chat'

const MAX_PENDING = 5

export type BusySendMode = 'queue' | 'steer'

export interface PendingQueueOwner {
  ownerRequestId?: string
}

export interface PendingQueueOwnerContext {
  sessionKey: string
  ownerRequestId: string
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
  dispatchHiddenControl?: (providerText: string, displayText: string) => void
}

export function useChatPendingQueue(options: UseChatPendingQueueOptions) {
  const pendingQueue = ref<ChatPendingItem[]>([])
  const parkedQueues = new Map<string, ChatPendingItem[]>()
  let pendingDrainTimer: ReturnType<typeof setTimeout> | null = null
  let deferredDrainRequested = false

  const canQueueMore = computed(() => pendingQueue.value.length < MAX_PENDING)

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

  function resolveOwnerRequestId(owner?: PendingQueueOwner): string | undefined {
    if (owner?.ownerRequestId) return owner.ownerRequestId
    const context = options.ownerContext?.value
    return context?.sessionKey === options.sessionKey.value
      ? context.ownerRequestId
      : undefined
  }

  function enqueuePendingInput(text: string, owner?: PendingQueueOwner) {
    if (pendingQueue.value.length >= MAX_PENDING) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return false
    }
    const ownerRequestId = resolveOwnerRequestId(owner)
    pendingQueue.value.push({
      text,
      attachments: options.pendingAttachments.value.map(a => ({ ...a })),
      intent: options.pendingSessionIntent.value,
      ownerSessionKey: options.sessionKey.value,
      ...(ownerRequestId ? { ownerRequestId } : {}),
    })
    options.inputText.value = ''
    options.pendingAttachments.value = []
    options.pendingSessionIntent.value = null
    options.autoResizeTextarea()
    flushDeferredPendingDrain()
    return true
  }

  function enqueueHiddenControl(
    item: { text: string; displayText: string },
    owner?: PendingQueueOwner,
  ) {
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
    })
    flushDeferredPendingDrain()
    return true
  }

  function removePendingChip(index: number) {
    pendingQueue.value.splice(index, 1)
  }

  function clearPendingQueue() {
    clearPendingDrainAfterTerminalTimer()
    pendingQueue.value = []
  }

  function switchPendingQueue(targetSessionKey: string) {
    clearPendingDrainAfterTerminalTimer()
    const restored = (parkedQueues.get(targetSessionKey) || [])
      .filter(item => !item.hiddenControl)
    parkedQueues.delete(targetSessionKey)
    // Explicit navigation keeps its historical behavior of discarding the
    // active session's queue. Only items parked during an automatic response
    // handoff can be restored when their parent is selected again.
    pendingQueue.value = restored
  }

  function adoptPendingQueue(targetSessionKey: string, ownerRequestId: string) {
    clearPendingDrainAfterTerminalTimer()
    const sourceSessionKey = options.sessionKey.value
    const carried: ChatPendingItem[] = []
    const stayingVisible: ChatPendingItem[] = []
    for (const item of pendingQueue.value) {
      if (
        ownerRequestId
        && item.ownerSessionKey === sourceSessionKey
        && item.ownerRequestId === ownerRequestId
      ) {
        carried.push({
          ...item,
          ownerSessionKey: targetSessionKey,
          ownerRequestId: undefined,
        })
      } else if (!item.hiddenControl) {
        // A hidden control is scoped to the run that created it. Carry the
        // matching run's controls, but never resurrect an older confirmation
        // after a later manual navigation back to the parent session.
        stayingVisible.push(item)
      }
    }
    if (stayingVisible.length > 0) {
      parkedQueues.set(sourceSessionKey, [
        ...(parkedQueues.get(sourceSessionKey) || []).filter(item => !item.hiddenControl),
        ...stayingVisible,
      ])
    }
    const targetItems = (parkedQueues.get(targetSessionKey) || [])
      .filter(item => !item.hiddenControl)
    parkedQueues.delete(targetSessionKey)
    pendingQueue.value = [...targetItems, ...carried]
  }

  function popPendingTail() {
    // Skip hidden-control sends: they never belong in the composer.
    let tailIndex = pendingQueue.value.length - 1
    while (tailIndex >= 0 && pendingQueue.value[tailIndex]?.hiddenControl) tailIndex--
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
    // Hidden-control sends stay queued (they bypass the composer); only the
    // visible drafts are pulled back in.
    const visible = pendingQueue.value.filter(p => !p.hiddenControl)
    const hidden = pendingQueue.value.filter(p => p.hiddenControl)
    if (visible.length === 0) return false
    const queuedTexts = visible.map(p => p.text).filter(Boolean)
    const queuedAttachments = visible.flatMap(p => p.attachments || [])
    const headIntent = visible[0]?.intent
    const current = options.inputText.value || ''
    const joined = [current, ...queuedTexts].filter(Boolean).join('\n')
    pendingQueue.value = hidden
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
    const head = pendingQueue.value.shift()
    if (head?.hiddenControl) {
      // Hidden-control sends bypass the composer entirely.
      const providerText = head.text || ''
      const displayText = head.displayTextOverride || ''
      nextTick(() => options.dispatchHiddenControl?.(providerText, displayText))
      return
    }
    options.inputText.value = head?.text || ''
    options.pendingAttachments.value = head?.attachments || []
    options.pendingSessionIntent.value = head?.intent || null
    nextTick(() => options.sendCurrentInput())
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
    armPendingDrainTimer()
  }

  function armPendingDrainTimer() {
    cancelPendingDrainTimer()
    pendingDrainTimer = setTimeout(() => {
      pendingDrainTimer = null
      if (pendingQueue.value.length === 0) {
        deferredDrainRequested = false
        return
      }
      if (options.isStreaming.value || options.isBlocked()) return
      deferredDrainRequested = false
      drainQueueHead()
    }, 50)
  }

  function flushDeferredPendingDrain() {
    if (!deferredDrainRequested || pendingQueue.value.length === 0) return
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
    enqueuePendingInput,
    enqueueHiddenControl,
    removePendingChip,
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
