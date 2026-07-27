import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatPendingQueue } from './useChatPendingQueue'
import type { Attachment, ChatPendingItem } from '@/types/chat'

function makeQueue(
  dispatchPendingItem?: (item: ChatPendingItem) => Promise<
    'accepted' | 'deferred' | 'not_sent' | 'retryable_failure'
  >,
  isBlocked: () => boolean = () => false,
) {
  const sessionKey = ref('agent:main:webchat:test')
  const inputText = ref('')
  const pendingAttachments = ref<Attachment[]>([])
  const pendingSessionIntent = ref<string | null>(null)
  const isStreaming = ref(false)
  const sendCurrentInput = vi.fn()
  const queue = useChatPendingQueue({
    sessionKey,
    inputText,
    pendingAttachments,
    pendingSessionIntent,
    isStreaming,
    isBlocked,
    autoResizeTextarea: vi.fn(),
    sendCurrentInput,
    resetInputHistory: vi.fn(),
    hasComposer: () => true,
    dispatchPendingItem,
  })

  return { inputText, queue, sendCurrentInput }
}

describe('useChatPendingQueue delivery state', () => {
  it('leases one item for steer and consumes it only after confirmed acceptance', () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'send this now'
    queue.enqueuePendingInput(inputText.value)
    inputText.value = 'must wait'
    queue.enqueuePendingInput(inputText.value)

    const item = queue.beginPendingDelivery(0)
    expect(item?.deliveryState).toBe('steering')
    expect(queue.beginPendingDelivery(0)).toBeNull()
    expect(queue.beginPendingDelivery(1)).toBeNull()

    queue.settlePendingDelivery(item!, 'retryable_failure')
    expect(queue.pendingQueue.value[0]?.deliveryState).toBe('retryable')
    expect(queue.beginPendingDelivery(1)).toBeNull()

    expect(queue.beginPendingDelivery(0)).toBe(item)
    queue.settlePendingDelivery(item!, 'accepted')
    expect(queue.pendingQueue.value.map(pending => pending.text)).toEqual(['must wait'])
    queue.cleanup()
  })

  it('settles an accepted steer after its queue was parked by response handoff', () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'belongs to another run'
    queue.enqueuePendingInput(inputText.value, { ownerRequestId: 'owner-a' })
    const item = queue.beginPendingDelivery(0)

    queue.adoptPendingQueue('agent:main:webchat:child', 'owner-b')
    expect(queue.pendingQueue.value).toEqual([])

    queue.settlePendingDelivery(item!, 'accepted')
    queue.switchPendingQueue('agent:main:webchat:test')
    expect(queue.pendingQueue.value).toEqual([])
    queue.cleanup()
  })

  it.each(['steering', 'retryable'] satisfies Array<
    Exclude<ChatPendingItem['deliveryState'], undefined>
  >)('defers automatic drain for any %s item and resumes after the state clears', async (state) => {
    vi.useFakeTimers()
    const { inputText, queue, sendCurrentInput } = makeQueue()
    try {
      inputText.value = 'queue head'
      queue.enqueuePendingInput(inputText.value)
      inputText.value = 'delivery barrier'
      queue.enqueuePendingInput(inputText.value)
      queue.pendingQueue.value[1]!.deliveryState = state

      queue.schedulePendingDrainAfterTerminal()
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)

      expect(queue.pendingQueue.value.map(item => item.text))
        .toEqual(['queue head', 'delivery barrier'])
      expect(sendCurrentInput).not.toHaveBeenCalled()

      queue.pendingQueue.value[1]!.deliveryState = undefined
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['delivery barrier'])
      expect(inputText.value).toBe('queue head')
      expect(sendCurrentInput).toHaveBeenCalledOnce()
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('auto-drains through the composer-preserving dispatcher after a steer settles', async () => {
    vi.useFakeTimers()
    const dispatchPendingItem = vi.fn(async () => 'accepted' as const)
    const { inputText, queue } = makeQueue(dispatchPendingItem)
    try {
      inputText.value = 'explicit steer'
      queue.enqueuePendingInput(inputText.value)
      inputText.value = 'next queued item'
      queue.enqueuePendingInput(inputText.value)
      const steering = queue.beginPendingDelivery(0)
      inputText.value = 'draft written while steering'

      queue.schedulePendingDrainAfterTerminal()
      queue.settlePendingDelivery(steering!, 'accepted')
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(dispatchPendingItem).toHaveBeenCalledWith(expect.objectContaining({
        text: 'next queued item',
      }))
      expect(inputText.value).toBe('draft written while steering')
      expect(queue.pendingQueue.value).toEqual([])
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('keeps a deferred auto-drain live until transient attachment work clears', async () => {
    vi.useFakeTimers()
    const attachmentBusy = ref(false)
    let callCount = 0
    const dispatchPendingItem = vi.fn(async () => {
      callCount += 1
      if (callCount === 1) {
        attachmentBusy.value = true
        return 'deferred' as const
      }
      return 'accepted' as const
    })
    const { inputText, queue } = makeQueue(
      dispatchPendingItem,
      () => attachmentBusy.value,
    )
    try {
      inputText.value = 'send after attachment work'
      queue.enqueuePendingInput(inputText.value)
      queue.schedulePendingDrainAfterTerminal()

      await vi.advanceTimersByTimeAsync(50)
      await nextTick()
      expect(dispatchPendingItem).toHaveBeenCalledOnce()
      expect(queue.pendingQueue.value).toHaveLength(1)

      // The deferred signal must survive the blocked timer without spinning.
      await vi.advanceTimersByTimeAsync(50)
      expect(dispatchPendingItem).toHaveBeenCalledOnce()

      attachmentBusy.value = false
      queue.flushDeferredPendingDrain()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(dispatchPendingItem).toHaveBeenCalledTimes(2)
      expect(queue.pendingQueue.value).toEqual([])
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('does not remove a steering item through remove or clear', () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'in flight'
    queue.enqueuePendingInput(inputText.value)
    inputText.value = 'not started'
    queue.enqueuePendingInput(inputText.value)
    queue.pendingQueue.value[0]!.deliveryState = 'steering'

    expect(queue.removePendingChip(0)).toBe(false)
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['in flight', 'not started'])

    queue.clearPendingQueue()
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['in flight'])
    expect(queue.pendingQueue.value[0]?.deliveryState).toBe('steering')
    queue.cleanup()
  })

  it('keeps steer-owned items out of composer recovery paths', () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'ordinary follow-up'
    queue.enqueuePendingInput(inputText.value)
    inputText.value = 'ambiguous steer'
    queue.enqueuePendingInput(inputText.value)
    queue.pendingQueue.value[1]!.deliveryState = 'retryable'

    expect(queue.popPendingTail()).toBe(true)
    expect(inputText.value).toBe('ordinary follow-up')
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['ambiguous steer'])

    inputText.value = 'existing draft'
    expect(queue.popAllPendingIntoComposer()).toBe(false)
    expect(inputText.value).toBe('existing draft')
    expect(queue.pendingQueue.value[0]?.deliveryState).toBe('retryable')
    queue.cleanup()
  })
})
