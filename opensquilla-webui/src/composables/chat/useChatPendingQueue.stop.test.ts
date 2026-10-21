import { nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useChatPendingQueue } from './useChatPendingQueue'
import { createPendingQueuePolicy } from '@/utils/chat/pendingQueuePolicy'
import type { ChatPendingItem } from '@/types/chat'

afterEach(() => vi.useRealTimers())

function setup(policy = createPendingQueuePolicy(null)) {
  const isStreaming = ref(true)
  const sessionKey = ref('chat-stop')
  const deliveryIdentity = ref<string | null>('gateway-owner')
  const send = vi.fn(async (_item: ChatPendingItem, _key: string) => 'accepted' as const)
  const persistenceError = vi.fn()
  const queue = useChatPendingQueue({
    sessionKey, deliveryIdentity, pendingQueuePolicy: policy,
    inputText: ref(''), pendingAttachments: ref([]), pendingSessionIntent: ref(null),
    isStreaming, isBlocked: () => false, hasComposer: () => true,
    autoResizeTextarea: vi.fn(), resetInputHistory: vi.fn(), sendCurrentInput: vi.fn(),
    dispatchPendingItem: async (item, key, guard) => guard?.() === false ? 'deferred' : send(item, key),
    onPendingPersistenceError: persistenceError,
  })
  const items: ChatPendingItem[] = ['C', 'D'].map(text => ({ pendingUiId: text, text, attachments: [], intent: null }))
  queue.pendingQueue.value = items
  return { queue, isStreaming, sessionKey, deliveryIdentity, send, persistenceError }
}

describe('ordinary Stop holds automatic follow-ups', () => {
  it('resumes after a captured new user action succeeds and drains when that turn finishes', async () => {
    vi.useFakeTimers()
    const h = setup()
    try {
      h.queue.pausePendingAutoSend()
      const accepted = h.queue.capturePendingAutoSendResume()
      expect(h.queue.autoSendPaused.value).toBe(true)
      accepted()
      expect(h.queue.autoSendPaused.value).toBe(false)
      await vi.advanceTimersByTimeAsync(100)
      expect(h.send).not.toHaveBeenCalled()
      h.isStreaming.value = false
      await nextTick()
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.send).toHaveBeenCalledOnce()
      expect(h.send.mock.calls[0]?.[0].text).toBe('C')
    } finally { h.queue.cleanup() }
  })

  it('does not release a later Stop when an earlier user action finishes', async () => {
    vi.useFakeTimers()
    const h = setup()
    try {
      h.queue.pausePendingAutoSend()
      const accepted = h.queue.capturePendingAutoSendResume()
      h.queue.pausePendingAutoSend()
      accepted()
      h.isStreaming.value = false
      await nextTick()
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.queue.autoSendPaused.value).toBe(true)
      expect(h.send).not.toHaveBeenCalled()
    } finally { h.queue.cleanup() }
  })

  it('keeps the pause and reports a resume persistence failure', async () => {
    vi.useFakeTimers()
    const values = new Map<string, string>()
    const storage = { getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) } }
    const h = setup(createPendingQueuePolicy(storage))
    try {
      h.queue.pausePendingAutoSend()
      const accepted = h.queue.capturePendingAutoSendResume()
      storage.setItem = () => { throw new Error('synthetic quota failure') }
      accepted()
      h.isStreaming.value = false
      await nextTick()
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.queue.autoSendPaused.value).toBe(true)
      expect(h.persistenceError).toHaveBeenCalledExactlyOnceWith('pause_failed')
      expect(h.send).not.toHaveBeenCalled()
    } finally { h.queue.cleanup() }
  })

  it.each(['session', 'identity', 'disposed', 'unpaused'] as const)(
    'does not apply a captured resume after its %s boundary changes', boundary => {
      const policy = createPendingQueuePolicy(null)
      const h = setup(policy)
      try {
        if (boundary !== 'unpaused') h.queue.pausePendingAutoSend()
        const accepted = h.queue.capturePendingAutoSendResume()
        if (boundary === 'session') h.sessionKey.value = 'other-chat'
        else if (boundary === 'identity') h.deliveryIdentity.value = 'other-owner'
        else if (boundary === 'disposed') h.queue.cleanup()
        else h.queue.pausePendingAutoSend()
        accepted()
        expect(policy.read({ sessionKey: 'chat-stop', deliveryIdentity: 'gateway-owner' }).paused).toBe(true)
        expect(h.send).not.toHaveBeenCalled()
      } finally { h.queue.cleanup() }
    },
  )

  it('blocks a prior deferred drain and later ready schedules, preserving both items', async () => {
    vi.useFakeTimers()
    const h = setup()
    try {
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(50)
      h.queue.pausePendingAutoSend()
      h.isStreaming.value = false
      await nextTick()
      h.queue.schedulePendingDrainAfterTerminal()
      h.queue.flushDeferredPendingDrain()
      await vi.advanceTimersByTimeAsync(500)
      expect(h.send).not.toHaveBeenCalled()
      expect(h.queue.pendingQueue.value.map(item => item.text)).toEqual(['C', 'D'])
      expect(h.queue.autoSendPaused.value).toBe(true)
    } finally { h.queue.cleanup() }
  })

  it('allows one explicit item without releasing the remaining queue, then resumes explicitly', async () => {
    vi.useFakeTimers()
    const h = setup()
    try {
      h.queue.pausePendingAutoSend()
      h.isStreaming.value = false
      await nextTick()
      const guard = h.queue.captureFollowupGuard(h.sessionKey.value, true)
      const item = h.queue.beginPendingDelivery('C')!
      expect(guard()).toBe(true)
      h.queue.settlePendingDelivery(item, 'accepted')
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.send).not.toHaveBeenCalled()
      expect(h.queue.pendingQueue.value.map(item => item.text)).toEqual(['D'])
      h.queue.resumePendingAutoSend()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.send).toHaveBeenCalledOnce()
      expect(h.send.mock.calls[0]?.[0].text).toBe('D')
    } finally { h.queue.cleanup() }
  })

  it('invalidates previously captured automatic and explicit sends when Stop arrives', () => {
    const h = setup()
    try {
      const automatic = h.queue.captureFollowupGuard(h.sessionKey.value)
      const explicit = h.queue.captureFollowupGuard(h.sessionKey.value, true)
      expect(automatic()).toBe(true)
      h.queue.pausePendingAutoSend()
      expect(automatic()).toBe(false)
      expect(explicit()).toBe(false)
      expect(h.queue.captureFollowupGuard(h.sessionKey.value, true)()).toBe(true)
    } finally { h.queue.cleanup() }
  })

  it('keeps the pause across remount and blocks a peer timer even without a storage event', async () => {
    vi.useFakeTimers()
    const values = new Map<string, string>()
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
    const h = setup(createPendingQueuePolicy(storage))
    const peer = setup(createPendingQueuePolicy(storage))
    try {
      peer.isStreaming.value = false
      peer.queue.schedulePendingDrainAfterTerminal()
      h.queue.pausePendingAutoSend()
      await vi.advanceTimersByTimeAsync(100)
      expect(peer.send).not.toHaveBeenCalled()
      const restored = setup(createPendingQueuePolicy(storage))
      try {
        expect(restored.queue.autoSendPaused.value).toBe(true)
        restored.sessionKey.value = 'another-chat'
        expect(restored.queue.autoSendPaused.value).toBe(false)
        restored.sessionKey.value = 'chat-stop'
        restored.deliveryIdentity.value = 'another-account'
        expect(restored.queue.autoSendPaused.value).toBe(false)
      } finally { restored.queue.cleanup() }
    } finally { h.queue.cleanup(); peer.queue.cleanup() }
  })

  it('carries Stop with a non-durable response-owned queue without releasing its parent', async () => {
    vi.useFakeTimers()
    const policy = createPendingQueuePolicy(null)
    const h = setup(policy)
    try {
      for (const item of h.queue.pendingQueue.value) {
        item.ownerSessionKey = h.sessionKey.value
        item.ownerRequestId = 'fork-A'
      }
      h.queue.pausePendingAutoSend()
      await h.queue.adoptPendingQueue('child-chat', 'fork-A')
      h.sessionKey.value = 'child-chat'
      h.isStreaming.value = false
      await nextTick()
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.send).not.toHaveBeenCalled()
      expect(h.queue.autoSendPaused.value).toBe(true)
      expect(h.queue.pendingQueue.value.map(item => item.text)).toEqual(['C', 'D'])
      const selected = h.queue.beginPendingDelivery('C')!
      expect(h.queue.captureFollowupGuard('child-chat', true)()).toBe(true)
      h.queue.settlePendingDelivery(selected, 'accepted')
      h.queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(100)
      expect(h.send).not.toHaveBeenCalled()
      expect(h.queue.pendingQueue.value.map(item => item.text)).toEqual(['D'])
      expect(policy.read({ sessionKey: 'chat-stop', deliveryIdentity: 'gateway-owner' }).paused).toBe(true)
    } finally { h.queue.cleanup() }
  })
})
