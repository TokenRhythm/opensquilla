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
  const sessionKey = ref('agent:main:webchat:test')
  const activeTurnId = ref('')
  const messages = ref<ChatMessage[]>([])
  const pendingQueue = ref<ChatPendingItem[]>([])
  const checkpointForUserMessage = vi.fn()
  const acknowledgeSteerBoundary = vi.fn()
  const scheduleHistorySync = vi.fn()
  const restoreSteerIntoComposer = vi.fn()
  const onProjected = vi.fn()
  const scope = effectScope()
  const api = scope.run(() => useChatSteerDelivery({
    sessionKey,
    activeTurnId,
    messages,
    pendingQueue,
    checkpointForUserMessage,
    acknowledgeSteerBoundary,
    scheduleHistorySync,
    restoreSteerIntoComposer,
    onProjected,
  }))!

  function addPending(): ChatPendingItem {
    const item: ChatPendingItem = {
      pendingUiId: `pending-ui-${pendingQueue.value.length}`,
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
    sessionKey,
    activeTurnId,
    messages,
    pendingQueue,
    checkpointForUserMessage,
    acknowledgeSteerBoundary,
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
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith(
        'turn-current',
        'client-steer',
      )
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
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'client-steer',
        '',
        1,
      )
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

  it('reconciles a history-restored durable row and checkpoints the attempt', () => {
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
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith(
        'turn-current',
        'client-steer',
      )
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'client-steer',
        '',
        0,
      )
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
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith(
        'turn-current',
        'client-steer',
      )
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

  it('checkpoints an orphan applied boundary before history hydration and repositions it later', () => {
    const harness = createHarness()
    try {
      harness.api.disposition({
        userMessageId: 'user-orphan',
        disposition: 'applied',
        turnId: 'turn-orphan',
      })

      expect(harness.messages.value).toEqual([])
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith(
        'turn-orphan',
        'user-orphan',
      )
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'user-orphan',
        '',
        0,
      )

      harness.messages.value = [{
        role: 'user',
        text: 'Use English',
        ts: 'durable',
        messageId: 'user-orphan',
        turnId: 'turn-orphan',
        inputDisposition: 'applied',
      }]

      expect(harness.checkpointForUserMessage).toHaveBeenCalledTimes(2)
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledTimes(2)
      expect(harness.messages.value).toHaveLength(1)
    } finally {
      harness.stop()
    }
  })

  it('discards orphan boundary evidence on an authoritative reset', () => {
    const harness = createHarness()
    try {
      harness.api.disposition({
        userMessageId: 'user-stale-orphan',
        disposition: 'applied',
        turnId: 'turn-stale',
      })
      harness.api.resetTransientBoundaries()
      harness.messages.value = [{
        role: 'user',
        text: 'stale',
        ts: 'durable',
        messageId: 'user-stale-orphan',
        turnId: 'turn-stale',
        inputDisposition: 'applied',
      }]

      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledOnce()
    } finally {
      harness.stop()
    }
  })

  it('does not replay a delayed orphan boundary into a successor turn', () => {
    const harness = createHarness()
    try {
      harness.activeTurnId.value = 'turn-old'
      harness.api.disposition({
        userMessageId: 'user-old-orphan',
        disposition: 'applied',
        turnId: 'turn-old',
      })
      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()

      harness.activeTurnId.value = 'turn-new'
      harness.messages.value = [{
        role: 'user',
        text: 'old steer',
        ts: 'delayed-history',
        messageId: 'user-old-orphan',
        turnId: 'turn-old',
        inputDisposition: 'applied',
      }]

      expect(harness.checkpointForUserMessage).toHaveBeenCalledOnce()
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledOnce()
    } finally {
      harness.stop()
    }
  })

  it('accepts a legacy applied event without revision and keeps the newer revision', () => {
    const harness = createHarness()
    try {
      harness.messages.value = [{
        role: 'user',
        text: REQUEST.message,
        ts: 'durable',
        turnId: REQUEST.expected_turn_id,
        messageId: 'user-steer',
        inputDisposition: 'applied',
        inputDispositionRevision: 3,
        steerClientRequestId: REQUEST.client_request_id,
        steerClientMessageId: REQUEST.client_message_id,
      }]

      const projection = harness.api.disposition({
        clientRequestId: REQUEST.client_request_id,
        clientMessageId: REQUEST.client_message_id,
        expectedTurnId: REQUEST.expected_turn_id,
        disposition: 'applied',
      })

      expect(projection.stale).toBe(false)
      expect(harness.messages.value[0]?.inputDispositionRevision).toBe(3)
      expect(harness.checkpointForUserMessage).toHaveBeenCalledWith(
        'turn-current',
        'client-steer',
      )
      expect(harness.acknowledgeSteerBoundary).toHaveBeenCalledWith(
        'client-steer',
        '',
        0,
      )
    } finally {
      harness.stop()
    }
  })
})
