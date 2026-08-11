import { effectScope, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import type { ChatMessage, ChatPendingItem, PendingSteerPhase } from '@/types/chat'
import type { SessionSteerV2Params } from '@/types/rpc'
import { useChatSteerDelivery } from './useChatSteerDelivery'

const REQUEST: SessionSteerV2Params = {
  key: 'agent:main:webchat:test',
  message: 'make it longer',
  expected_turn_id: 'turn-current',
  client_request_id: 'request-steer',
  client_message_id: 'client-steer',
  surface_id: 'webui',
  _source: { elevated: 'enabled', runMode: 'safe' },
}

function createHarness() {
  const messages = ref<ChatMessage[]>([])
  const pendingQueue = ref<ChatPendingItem[]>([])
  const checkpointForUserMessage = vi.fn()
  const scheduleHistorySync = vi.fn()
  const restoreSteerIntoComposer = vi.fn()
  const onProjected = vi.fn()
  const scope = effectScope()
  const api = scope.run(() => useChatSteerDelivery({
    messages,
    pendingQueue,
    checkpointForUserMessage,
    scheduleHistorySync,
    restoreSteerIntoComposer,
    onProjected,
  }))!

  function addPending(): ChatPendingItem {
    const item: ChatPendingItem = {
      text: REQUEST.message,
      attachments: [],
      intent: null,
      ownerSessionKey: REQUEST.key,
    }
    pendingQueue.value.push(item)
    expect(api.begin(item, REQUEST)).not.toBeNull()
    return item
  }

  return {
    api,
    messages,
    pendingQueue,
    checkpointForUserMessage,
    scheduleHistorySync,
    restoreSteerIntoComposer,
    onProjected,
    addPending,
    stop: () => scope.stop(),
  }
}

describe('useChatSteerDelivery', () => {
  it.each([
    ['retryable_rejected', 'KNOWN_REJECTION'],
    ['acceptance_unknown', 'RESPONSE_LOST'],
  ] as const)(
    'keeps %s pending without projecting an unproven transcript row',
    (phase: Exclude<PendingSteerPhase, 'submitting'>, code: string) => {
      const harness = createHarness()
      try {
        const item = harness.addPending()
        harness.api.markRetryable(item, phase, { code, retryAfterMs: 250 })

        expect(harness.messages.value).toEqual([])
        expect(item.deliveryState).toBeUndefined()
        expect(item.steerAttempt).toMatchObject({
          phase,
          errorCode: code,
          retryAfterMs: 250,
          request: REQUEST,
        })
        expect(Object.isFrozen(item.steerAttempt?.request)).toBe(true)
        expect(Object.isFrozen(item.steerAttempt?.request._source)).toBe(true)
      } finally {
        harness.stop()
      }
    },
  )

  it('projects accepted evidence once and checkpoints before the durable user row', () => {
    const harness = createHarness()
    try {
      const item = harness.addPending()
      const projection = harness.api.accept({
        clientRequestId: REQUEST.client_request_id,
        clientMessageId: REQUEST.client_message_id,
        expectedTurnId: REQUEST.expected_turn_id,
        userMessageId: 'user-steer',
        disposition: 'steering',
        revision: 1,
      }, item)

      expect(projection.created).toBe(true)
      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith('turn-current')
      expect(harness.pendingQueue.value).toEqual([])
      expect(harness.messages.value).toMatchObject([{
        role: 'user',
        text: 'make it longer',
        clientId: 'client-steer',
        messageId: 'user-steer',
        inputDisposition: 'steering',
        inputDispositionRevision: 1,
      }])
    } finally {
      harness.stop()
    }
  })

  it('treats a known permanent rejection as not admitted and restores no transcript row', () => {
    const harness = createHarness()
    try {
      const item = harness.addPending()
      harness.api.reject(item)

      expect(harness.pendingQueue.value).toEqual([])
      expect(harness.messages.value).toEqual([])
      expect(harness.restoreSteerIntoComposer).toHaveBeenCalledOnce()
      expect(harness.restoreSteerIntoComposer).toHaveBeenCalledWith(REQUEST.message)
      expect(harness.checkpointForUserMessage).not.toHaveBeenCalled()
    } finally {
      harness.stop()
    }
  })

  it('turns fallback-safe rejection back into an ordinary queued follow-up', () => {
    const harness = createHarness()
    try {
      const item = harness.addPending()
      harness.api.fallback(item)

      expect(harness.pendingQueue.value).toEqual([item])
      expect(item).not.toHaveProperty('steerAttempt')
      expect(item.deliveryState).toBeUndefined()
      expect(harness.messages.value).toEqual([])
    } finally {
      harness.stop()
    }
  })

  it('drops offscreen durable acceptance without projecting into the selected transcript', () => {
    const harness = createHarness()
    try {
      const item = harness.addPending()
      harness.api.acknowledgeAcceptedOffscreen(item)

      expect(harness.pendingQueue.value).toEqual([])
      expect(harness.messages.value).toEqual([])
      expect(harness.checkpointForUserMessage).not.toHaveBeenCalled()
    } finally {
      harness.stop()
    }
  })

  it('lets an event that arrives before the RPC response win by revision without duplication', () => {
    const harness = createHarness()
    try {
      const item = harness.addPending()
      harness.api.disposition({
        clientRequestId: REQUEST.client_request_id,
        clientMessageId: REQUEST.client_message_id,
        disposition: 'applied',
        revision: 2,
        turnId: REQUEST.expected_turn_id,
        appliedIteration: 1,
      })
      const lateResponse = harness.api.accept({
        clientRequestId: REQUEST.client_request_id,
        clientMessageId: REQUEST.client_message_id,
        disposition: 'steering',
        revision: 1,
        turnId: REQUEST.expected_turn_id,
      }, item)

      expect(lateResponse.stale).toBe(true)
      expect(harness.messages.value).toHaveLength(1)
      expect(harness.messages.value[0]).toMatchObject({
        inputDisposition: 'applied',
        inputDispositionRevision: 2,
        steerAppliedIteration: 1,
      })
      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()
    } finally {
      harness.stop()
    }
  })

  it.each([
    ['cancelled', false, 'restore_to_composer'],
    ['rejected', true, 'resend_after_queue_drains'],
  ] as const)('projects durable %s evidence and restores exactly once', (
    disposition,
    retryable,
    hint,
  ) => {
    const harness = createHarness()
    try {
      harness.addPending()
      const evidence = {
        clientRequestId: REQUEST.client_request_id,
        clientMessageId: REQUEST.client_message_id,
        disposition,
        revision: 3,
        turnId: REQUEST.expected_turn_id,
      }
      harness.api.disposition(evidence, { retryable, hint })
      harness.api.disposition(evidence, { retryable, hint })

      expect(harness.pendingQueue.value).toEqual([])
      expect(harness.messages.value).toHaveLength(1)
      expect(harness.messages.value[0]?.inputDisposition).toBe(disposition)
      expect(harness.restoreSteerIntoComposer).toHaveBeenCalledOnce()
    } finally {
      harness.stop()
    }
  })

  it('reconciles a history-restored durable row and checkpoints the attempt only once', () => {
    const harness = createHarness()
    try {
      harness.addPending()
      const durable: ChatMessage = {
        role: 'user',
        text: REQUEST.message,
        ts: 'durable',
        turnId: REQUEST.expected_turn_id,
        messageId: 'user-steer',
        inputDisposition: 'applied',
        steerClientRequestId: REQUEST.client_request_id,
        steerClientMessageId: REQUEST.client_message_id,
      }

      harness.messages.value = [durable]
      harness.messages.value = [{ ...durable }]

      expect(harness.pendingQueue.value).toEqual([])
      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith('turn-current')
    } finally {
      harness.stop()
    }
  })

  it('reconciles a parked attempt restored after its durable history row', () => {
    const harness = createHarness()
    try {
      harness.messages.value = [{
        role: 'user',
        text: REQUEST.message,
        ts: 'durable-first',
        turnId: REQUEST.expected_turn_id,
        messageId: 'user-steer',
        inputDisposition: 'applied',
        steerClientRequestId: REQUEST.client_request_id,
        steerClientMessageId: REQUEST.client_message_id,
      }]

      harness.addPending()

      expect(harness.pendingQueue.value).toEqual([])
      expect(harness.messages.value).toHaveLength(1)
      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith('turn-current')
    } finally {
      harness.stop()
    }
  })

  it('requests canonical history when a durable event beats both pending and row hydration', () => {
    const harness = createHarness()
    try {
      harness.api.disposition({
        clientRequestId: 'request-event-first',
        clientMessageId: 'client-event-first',
        disposition: 'applied',
        revision: 1,
        turnId: 'turn-event-first',
      })

      expect(harness.messages.value).toEqual([])
      expect(harness.scheduleHistorySync).toHaveBeenCalledOnce()
    } finally {
      harness.stop()
    }
  })
})
