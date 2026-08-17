import { describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref, watch } from 'vue'

import { useChatSend, type UseChatSendOptions } from './useChatSend'
import { useChatRpcEventHandlers } from './useChatRpcEventHandlers'
import {
  snapshotSteerRequest,
  useChatSteerDelivery,
} from './useChatSteerDelivery'
import { useChatTaskOwnership } from './useChatTaskOwnership'
import { useChatMessageActions } from './useChatMessageActions'
import type { FoldLiveTurnMode } from './useChatTurnLog'
import type {
  Attachment,
  ChatMessage,
  ChatPendingItem,
  ChatRenderedMessage,
} from '@/types/chat'
import type { CollaborationMode } from '@/types/plans'
import {
  useChatPendingQueue,
  type BusySendMode,
} from '@/composables/chat/useChatPendingQueue'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
} from '@/utils/chat/streamEvents'
import {
  listHiddenControls,
  persistHiddenControl,
  type HiddenControlStorage,
} from '@/utils/chat/hiddenControlOutbox'
import {
  listPendingMetaDiscards,
  persistPendingMetaDiscard,
} from '@/utils/chat/metaDiscardOutbox'
import type {
  PendingInputWal,
  ResponseHandoffWalRecord,
} from '@/utils/chat/pendingInputWal'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

function memoryStorage(): HiddenControlStorage {
  const values = new Map<string, string>()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: key => { values.delete(key) },
  }
}

function memoryHandoffWal(): PendingInputWal {
  const handoffs = new Map<string, ResponseHandoffWalRecord>()
  return {
    put: async () => {},
    list: async () => [],
    delete: async () => {},
    putHandoff: async record => { handoffs.set(record.ownerRequestId, structuredClone(record)) },
    prepareHandoff: async record => {
      const current = handoffs.get(record.ownerRequestId)
      if (current) return { applied: false, record: structuredClone(current) }
      const prepared = structuredClone(record)
      handoffs.set(record.ownerRequestId, prepared)
      return { applied: true, record: structuredClone(prepared) }
    },
    compareAndSwapHandoff: async (
      ownerRequestId,
      expectedWalOwnerId,
      expectedWalRevision,
      record,
    ) => {
      const current = handoffs.get(ownerRequestId)
      if (
        !current
        || current.walOwnerId !== expectedWalOwnerId
        || current.walRevision !== expectedWalRevision
      ) return { applied: false, record: current ? structuredClone(current) : null }
      if (!record) {
        handoffs.delete(ownerRequestId)
        return { applied: true, record: null }
      }
      const next = structuredClone(record)
      handoffs.set(ownerRequestId, next)
      return { applied: true, record: structuredClone(next) }
    },
    listHandoffs: async () => [...handoffs.values()].map(record => structuredClone(record)),
    acceptHandoff: async (ownerRequestId, acceptedSessionKey) => {
      const record = handoffs.get(ownerRequestId)
      if (!record) throw new Error('missing handoff')
      const handoff = {
        ...record,
        state: 'accepted' as const,
        acceptedSessionKey,
        updatedAt: Date.now(),
      }
      handoffs.set(ownerRequestId, handoff)
      return { handoff, records: [] }
    },
    deleteHandoff: async ownerRequestId => { handoffs.delete(ownerRequestId) },
    close: () => {},
  }
}

function makeOptions(overrides: Partial<UseChatSendOptions> = {}) {
  const rpc = {
    call: vi.fn().mockResolvedValue({ sessionKey: 'agent:main:webchat:test' }),
  }
  const stream: UseChatSendOptions['stream'] = {
    isStreaming: ref(false),
    streamBubble: ref(false),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(),
    endStreaming: vi.fn(),
    checkpointForUserMessage: vi.fn(),
    appendDelta: vi.fn(),
    scheduleRender: vi.fn(),
    appendToolCall: vi.fn(),
    appendToolDelta: vi.fn(),
    appendToolEnd: vi.fn(),
    appendToolResult: vi.fn(),
    appendArtifact: vi.fn(),
    reconcileFinalText: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(),
    appendFrame: vi.fn(),
    useReducer: ref<FoldLiveTurnMode>(false),
  }
  const messages = overrides.messages ?? ref<ChatMessage[]>([])
  const pendingQueue = ref<ChatPendingItem[]>([])
  const scheduleHistorySync = overrides.scheduleHistorySync ?? vi.fn()
  const steerDelivery = overrides.steerDelivery ?? useChatSteerDelivery({
    messages,
    pendingQueue,
    checkpointForUserMessage: stream.checkpointForUserMessage,
    scheduleHistorySync,
    restoreSteerIntoComposer: overrides.restoreSteerIntoComposer,
  })
  const enqueuePendingSteerAttempt = overrides.enqueuePendingSteerAttempt
    ?? ((payload) => {
      const item: ChatPendingItem = {
        pendingUiId: `pending-ui-${pendingQueue.value.length}`,
        text: payload.request.message,
        attachments: [],
        intent: null,
        ownerSessionKey: payload.request.key,
        steerAttempt: {
          phase: payload.phase || 'submitting',
          request: snapshotSteerRequest(payload.request),
        },
      }
      pendingQueue.value.push(item)
      return item
    })
  const options: UseChatSendOptions = {
    rpc,
    inputText: ref('hello'),
    messages,
    sessionKey: ref('agent:main:webchat:test'),
    pendingQueueOwnerContext: ref(null),
    hasPendingQueueWork: () => false,
    pendingInputWal: memoryHandoffWal(),
    busySendMode: ref<BusySendMode>('queue'),
    modelRoutingMode: ref<'off'>('off'),
    modelRoutingSettingsBusy: ref(false),
    elevatedMode: ref(''),
    runMode: ref('safe'),
    pendingAttachments: ref<Attachment[]>([]),
    pendingSessionIntent: ref(null),
    initialCollaborationMode: ref<CollaborationMode>('default'),
    pendingForkBeforeMessageId: ref(null),
    aborted: ref(false),
    activeStreamTaskId: ref(''),
    activeStreamSessionKey: ref(''),
    autoScroll: ref(false),
    stream,
    normalizeElevatedMode: mode => mode,
    adoptResponseSession: vi.fn(),
    scheduleHistorySync,
    schedulePendingDrainAfterTerminal: vi.fn(),
    flushDeferredPendingDrain: vi.fn(),
    isCompactInFlightForCurrentSession: () => false,
    hasPendingAttachmentWork: () => false,
    enqueuePendingInput: vi.fn(() => true),
    enqueuePendingSteerAttempt,
    steerDelivery,
    popAllPendingIntoComposer: vi.fn(() => false),
    hiddenControlStorage: memoryStorage(),
    classifySlashCommand: vi.fn(async () => 'registered' as const),
    executeSlashCommand: vi.fn(async () => false),
    closeSlashMenu: vi.fn(),
    autoResizeTextarea: vi.fn(),
    scrollToBottom: vi.fn(),
    ...overrides,
  }
  return { api: useChatSend(options), options, rpc, stream, pendingQueue }
}

function sameTurnSteerOptions(
  expectedTurnId = 'turn-current',
): Partial<UseChatSendOptions> {
  return {
    supportsMethod: method => method === 'sessions.steer.v2',
    activeSteerCapability: ref({
      mode: 'same_turn',
      expected_turn_id: expectedTurnId,
      input_kinds: ['text'],
    }),
    activeStreamTaskId: ref(expectedTurnId),
  }
}

function usageReplayMessages(): ChatMessage[] {
  return [
    {
      role: 'user',
      text: '/reset',
      ts: null,
      messageId: 'usage-primary',
      turnId: 'usage-turn',
    },
    {
      role: 'user',
      text: 'same-turn steer',
      ts: null,
      messageId: 'usage-steer',
      turnId: 'usage-turn',
    },
    {
      role: 'error',
      text: 'Usage accounting temporarily unavailable.',
      ts: null,
      messageId: 'usage-error',
      turnId: 'usage-turn',
      errorCode: 'usage_accounting_busy',
    },
  ]
}

describe('useChatSend dedicated usage-barrier replay', () => {
  it('atomically admits only one of two cross-tab clicks for the same barrier', async () => {
    const pendingInputWal = memoryHandoffWal()
    let releaseSend!: (value: unknown) => void
    const sendBlocked = new Promise(resolve => { releaseSend = resolve })
    const first = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    const second = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    first.rpc.call.mockImplementation(() => sendBlocked)
    second.rpc.call.mockImplementation(() => sendBlocked)

    const results = [first, second].map(harness => harness.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    }))
    await vi.waitFor(() => expect(
      first.rpc.call.mock.calls.length + second.rpc.call.mock.calls.length,
    ).toBe(1))
    releaseSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'cross-tab-winner',
    })

    expect((await Promise.all(results)).sort()).toEqual([false, true])
    const wire = (first.rpc.call.mock.calls[0] || second.rpc.call.mock.calls[0])?.[1]
    expect(wire).toMatchObject({
      forkBeforeMessageId: 'usage-primary',
      attachments: [],
    })
    expect(wire?.clientRequestId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
  })

  it('reuses the server receipt after winner cleanup when another tab still shows the card', async () => {
    const pendingInputWal = memoryHandoffWal()
    const first = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    const second = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    first.rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'first-accepted-task',
    })
    second.rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'same-receipt-task',
      replayed: true,
    })

    expect(await first.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(await second.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)

    expect(second.rpc.call.mock.calls[0]?.[1]).toMatchObject({
      clientRequestId: first.rpc.call.mock.calls[0]?.[1]?.clientRequestId,
      clientMessageId: first.rpc.call.mock.calls[0]?.[1]?.clientMessageId,
      message: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
  })

  it('lets another tab safely recover a rejected winner with the same stable receipt', async () => {
    const pendingInputWal = memoryHandoffWal()
    const first = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    const second = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    first.rpc.call.mockRejectedValue(Object.assign(new Error('busy'), {
      accepted: false,
      retryable: true,
    }))

    expect(await first.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(false)
    const rejectedWire = first.rpc.call.mock.calls[0]?.[1]

    await second.api.recoverResponseHandoffs()
    expect(second.rpc.call).not.toHaveBeenCalled()

    second.rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'recovered-winner',
    })
    expect(await second.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(second.rpc.call.mock.calls[0]?.[1]).toMatchObject({
      clientRequestId: rejectedWire?.clientRequestId,
      clientMessageId: rejectedWire?.clientMessageId,
      forkBeforeMessageId: 'usage-primary',
    })
  })

  it('isolates stable receipts by session and primary barrier without changing ordinary sends', async () => {
    const pendingInputWal = memoryHandoffWal()
    const sessionA = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    const sessionB = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
      sessionKey: ref('agent:main:webchat:other'),
    })
    const otherBarrierMessages = usageReplayMessages().map(message => (
      message.messageId === 'usage-primary'
        ? { ...message, messageId: 'usage-primary-2' }
        : message
    ))
    const barrierB = makeOptions({
      messages: ref(otherBarrierMessages),
      pendingInputWal,
    })
    const ordinary = makeOptions({
      inputText: ref('ordinary send'),
      pendingInputWal,
    })

    expect(await sessionA.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(await sessionB.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(await barrierB.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary-2',
    })).toBe(true)
    await ordinary.api.onSend()
    expect(ordinary.rpc.call).toHaveBeenCalledOnce()

    const requestIds = [sessionA, sessionB, barrierB, ordinary].map(
      harness => harness.rpc.call.mock.calls[0]?.[1]?.clientRequestId,
    )
    expect(new Set(requestIds).size).toBe(4)
  })

  it.each([
    'x'.repeat(513),
    'usage-primary\nforged',
  ])('rejects an unsafe durable primary identity before coordination: %j', async (messageId) => {
    const messages = usageReplayMessages().map(message => (
      message.messageId === 'usage-primary' ? { ...message, messageId } : message
    ))
    const { api, rpc } = makeOptions({ messages: ref(messages) })

    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: messageId,
    })).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('sends literal slash text with an exact fork and empty attachments without touching the draft', async () => {
    const messages = ref(usageReplayMessages())
    const inputText = ref('unrelated Goal/Replan draft')
    const pendingForkBeforeMessageId = ref<string | null>('draft-fork')
    const pendingSessionIntent = ref<string | null>('new_chat')
    const pendingAttachments = ref<Attachment[]>([
      {
        kind: 'inline',
        local_id: 1,
        name: 'draft-inline.txt',
        mime: 'text/plain',
        data: 'aW5saW5l',
      },
      {
        kind: 'staged',
        local_id: 2,
        name: 'draft-staged.pdf',
        mime: 'application/pdf',
        file_uuid: 'draft-upload',
      },
      {
        kind: 'failed',
        local_id: 3,
        name: 'draft-failed.txt',
        mime: 'text/plain',
        error: 'keep me',
      },
    ])
    const { api, options, rpc } = makeOptions({
      messages,
      inputText,
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
    })
    rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'usage-replay-task',
      user_message_id: 'usage-replay-message',
    })

    const accepted = await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })

    expect(accepted).toBe(true)
    expect(options.classifySlashCommand).not.toHaveBeenCalled()
    expect(options.executeSlashCommand).not.toHaveBeenCalled()
    expect(options.enqueuePendingInput).not.toHaveBeenCalled()
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: '/reset',
      forkBeforeMessageId: 'usage-primary',
      queueMode: 'followup',
      attachments: [],
    }))
    expect(inputText.value).toBe('unrelated Goal/Replan draft')
    expect(pendingAttachments.value).toHaveLength(3)
    expect(pendingAttachments.value.map(attachment => attachment.local_id)).toEqual([1, 2, 3])
    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(pendingForkBeforeMessageId.value).toBe('draft-fork')
    expect(messages.value).toMatchObject([{
      role: 'user',
      text: '/reset',
      clientId: expect.any(String),
      messageId: 'usage-replay-message',
      turnId: 'usage-replay-task',
    }])
  })

  it.each([
    'streaming',
    'authoritative work',
    'compaction',
    'pending queue',
    'pending queue ownership',
  ])('fails closed during %s instead of queuing or dropping the fork anchor', async (blockedBy) => {
    const messages = ref(usageReplayMessages())
    const inputText = ref('keep draft')
    const pendingForkBeforeMessageId = ref<string | null>('keep-draft-fork')
    const pendingQueueOwnerContext = ref<UseChatSendOptions['pendingQueueOwnerContext']['value']>(null)
    const taskOwnership = blockedBy === 'authoritative work'
      ? {
          hydrationResolved: ref(true),
          hasAuthoritativeWork: ref(true),
        } as unknown as NonNullable<UseChatSendOptions['taskOwnership']>
      : undefined
    const { api, rpc, stream, options } = makeOptions({
      messages,
      inputText,
      pendingForkBeforeMessageId,
      pendingQueueOwnerContext,
      ...(taskOwnership ? { taskOwnership } : {}),
      ...(blockedBy === 'compaction'
        ? { isCompactInFlightForCurrentSession: () => true }
        : {}),
      ...(blockedBy === 'pending queue'
        ? { hasPendingQueueWork: () => true }
        : {}),
    })
    if (blockedBy === 'streaming') stream.isStreaming.value = true
    if (blockedBy === 'pending queue ownership') {
      pendingQueueOwnerContext.value = {
        sessionKey: options.sessionKey.value,
        ownerRequestId: 'pending-owner',
      }
    }

    const accepted = await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })

    expect(accepted).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.enqueuePendingInput).not.toHaveBeenCalled()
    expect(messages.value).toEqual(usageReplayMessages())
    expect(inputText.value).toBe('keep draft')
    expect(pendingForkBeforeMessageId.value).toBe('keep-draft-fork')
  })

  it('fails closed behind an existing fork handoff without sending a second request', async () => {
    let resolveFirst!: (value: unknown) => void
    const firstResponse = new Promise(resolve => { resolveFirst = resolve })
    const messages = ref<ChatMessage[]>([
      ...usageReplayMessages(),
      { role: 'user', text: 'ordinary fork', ts: null, messageId: 'ordinary-anchor' },
    ])
    const pendingForkBeforeMessageId = ref<string | null>('ordinary-anchor')
    const { api, rpc } = makeOptions({
      messages,
      inputText: ref('ordinary fork'),
      pendingForkBeforeMessageId,
    })
    rpc.call.mockImplementationOnce(() => firstResponse)

    const ordinarySend = api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledTimes(1))
    const accepted = await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })

    expect(accepted).toBe(false)
    expect(rpc.call).toHaveBeenCalledTimes(1)
    resolveFirst({ sessionKey: 'agent:main:webchat:test' })
    await ordinarySend
  })

  it('keeps history and composer unchanged after rejection, then retries the exact receipt', async () => {
    const messages = ref(usageReplayMessages())
    const originalMessages = [...messages.value]
    const inputText = ref('draft survives')
    const pendingForkBeforeMessageId = ref<string | null>('draft-fork')
    const { api, rpc } = makeOptions({ messages, inputText, pendingForkBeforeMessageId })
    rpc.call
      .mockRejectedValueOnce(Object.assign(new Error('busy'), {
        accepted: false,
        retryable: true,
      }))
      .mockResolvedValueOnce({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'usage-retry-task',
        user_message_id: 'usage-retry-message',
      })

    const first = await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
    const firstParams = rpc.call.mock.calls[0]?.[1]

    expect(first).toBe(false)
    expect(messages.value).toEqual(originalMessages)
    expect(inputText.value).toBe('draft survives')
    expect(pendingForkBeforeMessageId.value).toBe('draft-fork')

    const second = await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })

    expect(second).toBe(true)
    expect(rpc.call.mock.calls[1]?.[1]).toMatchObject({
      clientRequestId: firstParams?.clientRequestId,
      clientMessageId: firstParams?.clientMessageId,
      message: '/reset',
      forkBeforeMessageId: 'usage-primary',
      attachments: [],
    })
    expect(inputText.value).toBe('draft survives')
    expect(pendingForkBeforeMessageId.value).toBe('draft-fork')
  })

  it('never restores a definitely rejected protocol replay into the composer from handoff WAL', async () => {
    let retained: ResponseHandoffWalRecord | null = null
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff: async record => {
        if (retained) return { applied: false, record: structuredClone(retained) }
        retained = structuredClone(record)
        return { applied: true, record: structuredClone(record) }
      },
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const messages = ref(usageReplayMessages())
    const inputText = ref('do not replace this draft')
    const pendingForkBeforeMessageId = ref<string | null>('draft-fork')
    const { api, rpc } = makeOptions({
      messages,
      inputText,
      pendingForkBeforeMessageId,
      pendingInputWal,
    })
    rpc.call.mockRejectedValue(Object.assign(new Error('rejected'), {
      accepted: false,
      retryable: false,
    }))

    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(false)
    expect(retained).toMatchObject({
      state: 'failed',
      restoreComposerOnFailure: false,
    })

    await api.recoverResponseHandoffs()

    expect(retained).toBeNull()
    expect(inputText.value).toBe('do not replace this draft')
    expect(pendingForkBeforeMessageId.value).toBe('draft-fork')
    expect(messages.value).toEqual(usageReplayMessages())
  })

  it('rechecks the session after delayed attachment preparation before creating an attempt', async () => {
    let releasePreparation!: (ready: boolean) => void
    const preparation = new Promise<boolean>(resolve => { releasePreparation = resolve })
    const prepareAttachmentsForSend = vi.fn(() => preparation)
    const messages = ref(usageReplayMessages())
    const inputText = ref('keep draft')
    const sessionKey = ref('agent:main:webchat:test')
    const { api, rpc } = makeOptions({
      messages,
      inputText,
      sessionKey,
      prepareAttachmentsForSend,
    })

    const replay = api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
    await vi.waitFor(() => expect(prepareAttachmentsForSend).toHaveBeenCalledOnce())
    sessionKey.value = 'agent:main:webchat:other'
    releasePreparation(true)

    expect(await replay).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(messages.value).toEqual(usageReplayMessages())
    expect(inputText.value).toBe('keep draft')
  })

  it('never recovers an unarmed write-after-throw and safely retries the same receipt', async () => {
    let retained: ResponseHandoffWalRecord | null = null
    let failPrepareAfterWrite = true
    let failDelete = true
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff: async record => {
        if (retained) return { applied: false, record: structuredClone(retained) }
        retained = structuredClone(record)
        if (failPrepareAfterWrite) {
          failPrepareAfterWrite = false
          throw new Error('storage reported failure after write')
        }
        return { applied: true, record: structuredClone(record) }
      },
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        if (!record && failDelete) throw new Error('delete unavailable')
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { throw new Error('legacy delete unavailable') },
      close: () => {},
    }
    const { api, rpc } = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })

    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(retained).toMatchObject({ state: 'preparing', walRevision: 1 })
    const firstParams = structuredClone(retained!.params)

    await api.recoverResponseHandoffs()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(retained).toMatchObject({ state: 'preparing', walRevision: 1 })

    failDelete = false
    rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'retry-after-unarmed',
    })
    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: firstParams.clientRequestId,
      clientMessageId: firstParams.clientMessageId,
      forkBeforeMessageId: 'usage-primary',
    }))
  })

  it('does not dispatch when arming the prepared handoff fails', async () => {
    let retained: ResponseHandoffWalRecord | null = null
    let failArm = true
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff: async record => {
        if (retained) return { applied: false, record: structuredClone(retained) }
        retained = structuredClone(record)
        return { applied: true, record: structuredClone(record) }
      },
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        if (record?.state === 'submitting' && failArm) {
          failArm = false
          throw new Error('arm transaction aborted')
        }
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const { api, rpc } = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })

    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(false)

    expect(rpc.call).not.toHaveBeenCalled()
    expect(retained).toBeNull()
  })

  it.each([
    'session switch',
    'busy work',
    'anchor disappearance',
  ])('rechecks %s after delayed arm and safely retries the same receipt', async (invalidatedBy) => {
    let retained: ResponseHandoffWalRecord | null = null
    let releaseArm!: () => void
    const armBlocked = new Promise<void>(resolve => { releaseArm = resolve })
    let blockFirstArm = true
    let armStarted = false
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff: async record => {
        if (retained) return { applied: false, record: structuredClone(retained) }
        retained = structuredClone(record)
        return { applied: true, record: structuredClone(record) }
      },
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        if (record?.state === 'submitting' && blockFirstArm) {
          blockFirstArm = false
          armStarted = true
          await armBlocked
        }
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const taskOwnership = useChatTaskOwnership()
    const sessionKey = ref('agent:main:webchat:test')
    const messages = ref(usageReplayMessages())
    const { api, rpc } = makeOptions({
      messages,
      pendingInputWal,
      sessionKey,
      taskOwnership,
    })
    rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'post-arm-retry',
    })

    const replay = api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
    await vi.waitFor(() => expect(armStarted).toBe(true))
    const firstParams = structuredClone(retained!.params)
    if (invalidatedBy === 'session switch') {
      sessionKey.value = 'agent:main:webchat:other'
    } else if (invalidatedBy === 'busy work') {
      taskOwnership.runningTaskId.value = 'other-running-task'
    } else {
      messages.value = messages.value.filter(message => message.messageId !== 'usage-primary')
    }
    releaseArm()

    expect(await replay).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(retained).toBeNull()

    sessionKey.value = 'agent:main:webchat:test'
    taskOwnership.runningTaskId.value = ''
    messages.value = usageReplayMessages()
    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: firstParams.clientRequestId,
      clientMessageId: firstParams.clientMessageId,
      forkBeforeMessageId: 'usage-primary',
    }))
  })

  it('lets recovery delete an unarmed record before a concurrent arm without sending it', async () => {
    let retained: ResponseHandoffWalRecord | null = null
    let releasePrepare!: () => void
    const prepareBlocked = new Promise<void>(resolve => { releasePrepare = resolve })
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff: async record => {
        retained = structuredClone(record)
        await prepareBlocked
        return { applied: true, record: structuredClone(record) }
      },
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const live = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
    })
    const recovery = makeOptions({ pendingInputWal })

    const replay = live.api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
    await vi.waitFor(() => expect(retained?.state).toBe('preparing'))
    await recovery.api.recoverResponseHandoffs()
    releasePrepare()

    expect(await replay).toBe(false)
    expect(live.rpc.call).not.toHaveBeenCalled()
    expect(recovery.rpc.call).not.toHaveBeenCalled()
    expect(retained).toBeNull()
  })

  it('does not let stale pre-dispatch cleanup erase a concurrently armed record', async () => {
    let retained: ResponseHandoffWalRecord | null = null
    let releasePrepare!: () => void
    const prepareBlocked = new Promise<void>(resolve => { releasePrepare = resolve })
    const sessionKey = ref('agent:main:webchat:test')
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff: async record => {
        retained = structuredClone(record)
        await prepareBlocked
        return { applied: true, record: structuredClone(record) }
      },
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const { api, rpc } = makeOptions({
      messages: ref(usageReplayMessages()),
      pendingInputWal,
      sessionKey,
    })

    const replay = api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
    await vi.waitFor(() => expect(retained?.state).toBe('preparing'))
    retained = {
      ...retained!,
      state: 'submitting',
      walRevision: retained!.walRevision! + 1,
      updatedAt: Date.now(),
    }
    sessionKey.value = 'agent:main:webchat:other'
    releasePrepare()

    expect(await replay).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(retained).toMatchObject({ state: 'submitting', walRevision: 2 })
  })

  it.each([
    'session switch',
    'busy stream',
    'authoritative work',
    'compaction',
    'pending queue',
    'anchor disappearance',
  ])('rechecks %s after delayed handoff persistence and keeps the same retry receipt', async (invalidatedBy) => {
    let releasePersistence!: () => void
    const persistence = new Promise<void>(resolve => { releasePersistence = resolve })
    let firstPersist = true
    let retained: ResponseHandoffWalRecord | null = null
    const prepareHandoff = vi.fn(async (record: ResponseHandoffWalRecord) => {
      retained = structuredClone(record)
      if (firstPersist) {
        firstPersist = false
        await persistence
      }
      return { applied: true, record: structuredClone(record) }
    })
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      prepareHandoff,
      compareAndSwapHandoff: async (_owner, expectedOwner, expectedRevision, record) => {
        if (
          !retained
          || retained.walOwnerId !== expectedOwner
          || retained.walRevision !== expectedRevision
        ) return { applied: false, record: retained ? structuredClone(retained) : null }
        retained = record ? structuredClone(record) : null
        return { applied: true, record: retained ? structuredClone(retained) : null }
      },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    let pendingQueueWork = false
    let compactInFlight = false
    const taskOwnership = useChatTaskOwnership()
    const messages = ref(usageReplayMessages())
    const sessionKey = ref('agent:main:webchat:test')
    const { api, rpc, stream } = makeOptions({
      messages,
      sessionKey,
      pendingInputWal,
      hasPendingQueueWork: () => pendingQueueWork,
      isCompactInFlightForCurrentSession: () => compactInFlight,
      taskOwnership,
    })
    rpc.call.mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'retried-task',
      user_message_id: 'retried-message',
    })

    const firstReplay = api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })
    await vi.waitFor(() => expect(prepareHandoff).toHaveBeenCalledOnce())
    const firstParams = structuredClone(retained!.params)
    if (invalidatedBy === 'session switch') {
      sessionKey.value = 'agent:main:webchat:other'
    } else if (invalidatedBy === 'busy stream') {
      stream.isStreaming.value = true
    } else if (invalidatedBy === 'authoritative work') {
      taskOwnership.runningTaskId.value = 'other-running-task'
    } else if (invalidatedBy === 'compaction') {
      compactInFlight = true
    } else if (invalidatedBy === 'pending queue') {
      pendingQueueWork = true
    } else {
      messages.value = messages.value.filter(message => message.messageId !== 'usage-primary')
    }
    releasePersistence()

    expect(await firstReplay).toBe(false)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(retained).toBeNull()

    sessionKey.value = 'agent:main:webchat:test'
    stream.isStreaming.value = false
    taskOwnership.runningTaskId.value = ''
    compactInFlight = false
    pendingQueueWork = false
    messages.value = usageReplayMessages()
    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: firstParams.clientRequestId,
      clientMessageId: firstParams.clientMessageId,
      forkBeforeMessageId: 'usage-primary',
      attachments: [],
    }))
  })

  it('tears down its fresh stream when the final pre-RPC guard becomes blocked', async () => {
    let pendingQueueWork = false
    const activeStreamTaskId = ref('')
    const activeStreamSessionKey = ref('')
    const aborted = ref(true)
    const { api, rpc, stream } = makeOptions({
      messages: ref(usageReplayMessages()),
      activeStreamTaskId,
      activeStreamSessionKey,
      aborted,
      hasPendingQueueWork: () => pendingQueueWork,
    })
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
      pendingQueueWork = true
    })
    stream.endStreaming = vi.fn(() => {
      stream.isStreaming.value = false
    })

    expect(await api.sendUsageBarrierReplay({
      text: '/reset',
      forkBeforeMessageId: 'usage-primary',
    })).toBe(false)

    expect(rpc.call).not.toHaveBeenCalled()
    expect(stream.endStreaming).toHaveBeenCalledOnce()
    expect(stream.isStreaming.value).toBe(false)
    expect(activeStreamTaskId.value).toBe('')
    expect(activeStreamSessionKey.value).toBe('')
    expect(aborted.value).toBe(true)
  })
})

describe('useChatSend attachment payloads', () => {
  it('replays a persisted handoff identity after refresh and repairs its owner queue', async () => {
    const parent = 'agent:main:webchat:parent'
    const child = 'agent:main:webchat:child'
    const other = 'agent:main:webchat:other'
    const params = {
      sessionKey: parent,
      clientRequestId: 'fork-refresh-request',
      clientMessageId: 'fork-refresh-message',
      message: 'fork request',
      forkBeforeMessageId: 'fork-before',
      _source: { runMode: 'safe' as const },
    }
    const handoffs = new Map<string, ResponseHandoffWalRecord>([[
      params.clientRequestId,
      {
        schemaVersion: 1,
        ownerRequestId: params.clientRequestId,
        requestSessionKey: parent,
        clientRequestId: params.clientRequestId,
        clientMessageId: params.clientMessageId,
        composerText: 'fork request',
        recoveryAttachments: [],
        params,
        state: 'submitting',
        createdAt: 1,
        updatedAt: 1,
      },
    ]])
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { handoffs.set(record.ownerRequestId, structuredClone(record)) },
      listHandoffs: async () => [...handoffs.values()].map(record => structuredClone(record)),
      deleteHandoff: async ownerRequestId => { handoffs.delete(ownerRequestId) },
      close: () => {},
    }
    const recoverPendingQueueHandoff = vi.fn(async () => {})
    const adoptResponseSession = vi.fn()
    const rpc = {
      call: vi.fn(async () => ({ sessionKey: child, replayed: true })),
    } as unknown as UseChatSendOptions['rpc']
    const { api } = makeOptions({
      rpc,
      sessionKey: ref(other),
      pendingInputWal,
      recoverPendingQueueHandoff,
      adoptResponseSession,
    })

    await api.recoverResponseHandoffs()

    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith('chat.send', params)
    expect(recoverPendingQueueHandoff).toHaveBeenCalledWith(
      parent,
      child,
      params.clientRequestId,
    )
    expect(adoptResponseSession).not.toHaveBeenCalled()
    expect(handoffs.size).toBe(0)
  })

  it('restores a failed handoff draft from durable attachment recovery material', async () => {
    const sessionKey = 'agent:main:webchat:failed-fork'
    const attachment = {
      kind: 'staged' as const,
      local_id: 84,
      name: 'recover.txt',
      mime: 'text/plain',
      file_uuid: 'expired-upload',
    }
    const record: ResponseHandoffWalRecord = {
      schemaVersion: 1,
      ownerRequestId: 'failed-fork-request',
      requestSessionKey: sessionKey,
      clientRequestId: 'failed-fork-request',
      clientMessageId: 'failed-fork-message',
      composerText: 'restore the fork draft',
      recoveryAttachments: [attachment],
      params: {
        sessionKey,
        clientRequestId: 'failed-fork-request',
        clientMessageId: 'failed-fork-message',
        message: 'restore the fork draft',
        forkBeforeMessageId: 'fork-source-message',
      },
      state: 'failed',
      errorCode: 'ATTACHMENT_EXPIRED',
      createdAt: 1,
      updatedAt: 2,
    }
    let retained: ResponseHandoffWalRecord | null = record
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const inputText = ref('')
    const pendingAttachments = ref<Attachment[]>([])
    const pendingForkBeforeMessageId = ref<string | null>(null)
    const { api, rpc } = makeOptions({
      sessionKey: ref(sessionKey),
      inputText,
      pendingAttachments,
      pendingForkBeforeMessageId,
      pendingInputWal,
    })

    await api.recoverResponseHandoffs()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('restore the fork draft')
    expect(pendingAttachments.value).toEqual([attachment])
    expect(pendingForkBeforeMessageId.value).toBe('fork-source-message')
    expect(retained).toBeNull()
  })

  it('refreshes expired handoff attachments only after a definite rejection', async () => {
    const parent = 'agent:main:webchat:expired-fork-parent'
    const child = 'agent:main:webchat:expired-fork-child'
    const file = new File(['durable'], 'durable.txt', { type: 'text/plain' })
    const attachment: Attachment = {
      kind: 'staged',
      local_id: 85,
      name: 'durable.txt',
      mime: 'text/plain',
      size: file.size,
      file_uuid: 'expired-upload',
      expires_at: 1,
      file,
    }
    let retained: ResponseHandoffWalRecord | null = {
      schemaVersion: 1,
      ownerRequestId: 'expired-fork-request',
      requestSessionKey: parent,
      clientRequestId: 'expired-fork-request',
      clientMessageId: 'expired-fork-message',
      composerText: 'retry the fork attachment',
      recoveryAttachments: [attachment],
      params: {
        sessionKey: parent,
        clientRequestId: 'expired-fork-request',
        clientMessageId: 'expired-fork-message',
        message: 'retry the fork attachment',
        forkBeforeMessageId: 'fork-source-message',
        attachments: [{
          type: attachment.mime,
          name: attachment.name,
          mime: attachment.mime,
          file_uuid: 'expired-upload',
        }],
      },
      state: 'submitting',
      createdAt: 1,
      updatedAt: 2,
    }
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      putHandoff: async record => { retained = structuredClone(record) },
      listHandoffs: async () => retained ? [structuredClone(retained)] : [],
      acceptHandoff: async (_ownerRequestId, acceptedSessionKey) => ({
        handoff: { ...retained!, state: 'accepted', acceptedSessionKey },
        records: [],
      }),
      deleteHandoff: async () => { retained = null },
      close: () => {},
    }
    const prepareAttachmentsForSend = vi.fn(async ({ attachments }) => {
      const staged = attachments?.[0]
      if (staged?.kind === 'staged') {
        staged.file_uuid = 'refreshed-upload'
        staged.expires_at = Date.now() + 60_000
      }
      return true
    })
    const recoverPendingQueueHandoff = vi.fn().mockResolvedValue(undefined)
    const { api, rpc } = makeOptions({
      sessionKey: ref('agent:main:webchat:another-session'),
      pendingInputWal,
      prepareAttachmentsForSend,
      recoverPendingQueueHandoff,
    })
    rpc.call
      .mockRejectedValueOnce(Object.assign(new Error('expired'), {
        accepted: false,
        retryable: true,
        code: 'ATTACHMENT_EXPIRED',
      }))
      .mockResolvedValueOnce({ sessionKey: child, task_id: 'task-refreshed' })

    await api.recoverResponseHandoffs()

    expect(prepareAttachmentsForSend).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledTimes(2)
    const replay = rpc.call.mock.calls[1]?.[1] as { attachments?: Array<{ file_uuid?: string }> }
    expect(replay.attachments?.[0]?.file_uuid).toBe('refreshed-upload')
    expect(replay).toMatchObject({
      clientRequestId: 'expired-fork-request',
      clientMessageId: 'expired-fork-message',
      sessionKey: parent,
    })
    expect(recoverPendingQueueHandoff).toHaveBeenCalledWith(
      parent,
      child,
      'expired-fork-request',
    )
    expect(retained).toBeNull()
  })

  it('keeps a follow-up in the composer when fork handoff WAL is unavailable', async () => {
    let resolveSend!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>(resolve => {
        resolveSend = resolve as (value: unknown) => void
      })) as UseChatSendOptions['rpc']['call'],
    }
    const inputText = ref('fork without browser WAL')
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      inputText,
      pendingInputWal: null,
      pendingForkBeforeMessageId: ref('fork-before-message'),
      enqueuePendingInput,
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    inputText.value = 'must remain editable'
    await harness.api.onSend()

    expect(enqueuePendingInput).not.toHaveBeenCalled()
    expect(inputText.value).toBe('must remain editable')
    resolveSend({ sessionKey: 'agent:main:webchat:fork-child' })
    await forkSend
  })

  it('uses a supplied stable ingress id for a resumed hidden control', async () => {
    const { api, rpc } = makeOptions()

    const result = await api.dispatchHiddenSend(
      '/meta meta-short-drama -- original request',
      '/meta meta-short-drama -- original request',
      'provider-handoff-request-1',
    )

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: 'provider-handoff-request-1',
    }))
    expect(result).toEqual({
      status: 'accepted',
      reason: 'accepted',
      clientRequestId: 'provider-handoff-request-1',
      sessionKey: 'agent:main:webchat:test',
    })
  })

  it('materializes a provisional draft when its recovered hidden turn is accepted', async () => {
    const pendingSessionIntent = ref<string | null>('new_chat')
    const { api, rpc } = makeOptions({ pendingSessionIntent })

    await api.dispatchHiddenSend(
      '/meta meta-paper-write -- recovered after reopen',
      '/meta meta-paper-write -- recovered after reopen',
      'recovered-provisional-request',
    )

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: 'recovered-provisional-request',
      intent: 'new_chat',
    }))
    expect(pendingSessionIntent.value).toBeNull()
  })

  it('preserves a resumed hidden control ingress id when it must queue', async () => {
    const enqueueHiddenControl = vi.fn(() => true)
    const { api, stream } = makeOptions({ enqueueHiddenControl })
    stream.isStreaming.value = true

    const result = await api.dispatchHiddenSend(
      '/meta meta-short-drama -- original request',
      '/meta meta-short-drama -- original request',
      'provider-handoff-request-2',
    )

    expect(enqueueHiddenControl).toHaveBeenCalledWith({
      text: '/meta meta-short-drama -- original request',
      displayText: '/meta meta-short-drama -- original request',
      clientRequestId: 'provider-handoff-request-2',
      sessionKey: 'agent:main:webchat:test',
    })
    expect(result.status).toBe('queued')
    expect(result.reason).toBe('queued')
  })

  it('persists a delayed hidden control for its originating session without sending in another', async () => {
    const { api, options, rpc } = makeOptions()
    options.sessionKey.value = 'agent:main:webchat:another'

    const result = await api.dispatchHiddenSend(
      '/meta meta-paper-write -- original request',
      '/meta meta-paper-write -- original request',
      'delayed-origin-request',
      'agent:main:webchat:test',
    )

    expect(result).toMatchObject({
      status: 'queued',
      reason: 'queued',
      sessionKey: 'agent:main:webchat:test',
    })
    expect(rpc.call).not.toHaveBeenCalled()
    expect(listHiddenControls(
      'agent:main:webchat:test',
      options.hiddenControlStorage,
    )).toHaveLength(1)
  })

  it('rejects a hidden control without sending when the pending queue is full', async () => {
    const enqueueHiddenControl = vi.fn(() => false)
    const { api, rpc, stream } = makeOptions({ enqueueHiddenControl })
    stream.isStreaming.value = true

    const result = await api.dispatchHiddenSend(
      '/meta meta-short-drama -- original request',
      '/meta meta-short-drama -- original request',
      'provider-handoff-queue-full',
    )

    expect(result).toEqual({
      status: 'rejected',
      reason: 'queue_full',
      clientRequestId: 'provider-handoff-queue-full',
      sessionKey: 'agent:main:webchat:test',
    })
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('classifies rejected, ambiguous, and accepted RPC failures', async () => {
    const rejected = makeOptions()
    rejected.rpc.call.mockRejectedValue(Object.assign(new Error('Rejected'), { accepted: false }))
    await expect(rejected.api.dispatchHiddenSend('/meta test', '/meta test', 'rejected-id'))
      .resolves.toMatchObject({ status: 'rejected', reason: 'send_rejected' })

    const ambiguous = makeOptions()
    ambiguous.rpc.call.mockRejectedValue(new Error('Connection closed before response'))
    await expect(ambiguous.api.dispatchHiddenSend('/meta test', '/meta test', 'unknown-id'))
      .resolves.toMatchObject({ status: 'unknown', reason: 'response_unknown' })

    const accepted = makeOptions()
    accepted.rpc.call.mockRejectedValue(Object.assign(new Error('Response lost'), { accepted: true }))
    await expect(accepted.api.dispatchHiddenSend('/meta test', '/meta test', 'accepted-id'))
      .resolves.toMatchObject({ status: 'accepted', reason: 'accepted' })
    expect(accepted.stream.endStreaming).not.toHaveBeenCalled()
  })

  it('localizes a rejected hidden send while preserving its dispatch result', async () => {
    const { api, options, rpc } = makeOptions()
    rpc.call.mockRejectedValue(Object.assign(new Error('server fallback text'), {
      accepted: false,
      retryable: false,
      code: 'ensemble_multimodal_unsupported',
    }))

    await expect(api.dispatchHiddenSend(
      '/meta test',
      '/meta test',
      'localized-rejected-id',
    )).resolves.toEqual({
      status: 'rejected',
      reason: 'send_rejected',
      clientRequestId: 'localized-rejected-id',
      sessionKey: 'agent:main:webchat:test',
    })

    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      errorCode: 'ensemble_multimodal_unsupported',
      text: "Ensemble doesn't support image input yet. Under Model routing, choose AI-powered single-model router with an image-capable tier configured, or turn routing Off and select an image-capable model.",
    })
  })

  it('does not send a different payload under an existing hidden-control id', async () => {
    const { api, rpc } = makeOptions()
    rpc.call.mockRejectedValueOnce(new Error('response lost'))
    await expect(api.dispatchHiddenSend('/meta first', '/meta first', 'immutable-id'))
      .resolves.toMatchObject({ status: 'unknown' })

    rpc.call.mockResolvedValue({ sessionKey: 'agent:main:webchat:test' })
    await expect(api.dispatchHiddenSend('/meta second', '/meta second', 'immutable-id'))
      .resolves.toMatchObject({ status: 'rejected', reason: 'outbox_conflict' })
    expect(rpc.call).toHaveBeenCalledOnce()
  })

  it('drops only explicitly permanent hidden-control RPC rejections', async () => {
    const permanent = makeOptions()
    permanent.rpc.call.mockRejectedValue(Object.assign(new Error('invalid'), {
      accepted: false,
      retryable: false,
    }))
    await permanent.api.dispatchHiddenSend('/meta test', '/meta test', 'permanent-id')
    expect(listHiddenControls(
      'agent:main:webchat:test',
      permanent.options.hiddenControlStorage,
    )).toEqual([])

    const retryable = makeOptions()
    retryable.rpc.call.mockRejectedValue(Object.assign(new Error('busy'), {
      accepted: false,
      retryable: true,
    }))
    await retryable.api.dispatchHiddenSend('/meta test', '/meta test', 'retryable-id')
    expect(listHiddenControls(
      'agent:main:webchat:test',
      retryable.options.hiddenControlStorage,
    )).toHaveLength(1)
  })

  it('coalesces concurrent retries with the same session and ingress id', async () => {
    let resolveSend: ((value: unknown) => void) | undefined
    const pendingSend = new Promise(resolve => { resolveSend = resolve })
    const { api, rpc } = makeOptions()
    rpc.call.mockImplementation(() => pendingSend)

    const first = api.dispatchHiddenSend('/meta test', '/meta test', 'same-request')
    const second = api.dispatchHiddenSend('/meta test', '/meta test', 'same-request')

    expect(second).toBe(first)
    expect(rpc.call).toHaveBeenCalledOnce()
    resolveSend?.({ sessionKey: 'agent:main:webchat:test' })
    await expect(first).resolves.toMatchObject({ status: 'accepted' })
  })

  it('restores a queued hidden control after remount and clears only on acceptance', async () => {
    const first = makeOptions({ enqueueHiddenControl: vi.fn(() => true) })
    first.stream.isStreaming.value = true
    await expect(first.api.dispatchHiddenSend(
      '/meta-replay 0123456789abcdef0123456789abcdef',
      'Retry failed step',
      'durable-replay-request',
    )).resolves.toMatchObject({ status: 'queued' })
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toHaveLength(1)

    const remounted = makeOptions({
      hiddenControlStorage: first.options.hiddenControlStorage,
    })
    await remounted.api.restoreHiddenControls('agent:main:webchat:test')
    expect(remounted.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: 'durable-replay-request',
      message: '/meta-replay 0123456789abcdef0123456789abcdef',
    }))
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toEqual([])
  })

  it('stops a multi-control restore when its lifecycle guard becomes stale', async () => {
    const hiddenControlStorage = memoryStorage()
    for (const requestId of ['first-hidden-request', 'second-hidden-request']) {
      expect(persistHiddenControl({
        sessionKey: 'agent:main:webchat:test',
        clientRequestId: requestId,
        providerText: `/meta test -- ${requestId}`,
        displayText: `/meta test -- ${requestId}`,
      }, hiddenControlStorage)).toBe(true)
    }
    let resolveFirst: ((value: unknown) => void) | undefined
    const first = new Promise(resolve => { resolveFirst = resolve })
    const remounted = makeOptions({ hiddenControlStorage })
    remounted.rpc.call
      .mockImplementationOnce(() => first)
      .mockResolvedValue({ sessionKey: 'agent:main:webchat:test' })
    let current = true

    const restoring = remounted.api.restoreHiddenControls(
      'agent:main:webchat:test',
      [],
      () => current,
    )
    await vi.waitFor(() => expect(remounted.rpc.call).toHaveBeenCalledOnce())
    current = false
    resolveFirst?.({ sessionKey: 'agent:main:webchat:test' })
    await restoring

    expect(remounted.rpc.call).toHaveBeenCalledOnce()
    expect(listHiddenControls(
      'agent:main:webchat:test',
      hiddenControlStorage,
    ).map(item => item.clientRequestId)).toEqual(['second-hidden-request'])
  })

  it('does not duplicate a browser fallback already attempted from the server outbox', async () => {
    const first = makeOptions({ enqueueHiddenControl: vi.fn(() => true) })
    first.stream.isStreaming.value = true
    await first.api.dispatchHiddenSend(
      '/meta meta-paper-write -- one durable request',
      '/meta meta-paper-write -- one durable request',
      'shared-server-browser-request',
    )

    const remounted = makeOptions({
      hiddenControlStorage: first.options.hiddenControlStorage,
    })
    await remounted.api.restoreHiddenControls(
      'agent:main:webchat:test',
      ['shared-server-browser-request'],
    )

    expect(remounted.rpc.call).not.toHaveBeenCalled()
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toHaveLength(1)
  })

  it('does not restore an explicitly discarded hidden control after remount', async () => {
    const discardStorage = memoryStorage()
    const first = makeOptions({
      enqueueHiddenControl: vi.fn(() => true),
      metaDiscardStorage: discardStorage,
    })
    first.stream.isStreaming.value = true
    await first.api.dispatchHiddenSend(
      '/meta meta-short-drama -- cancel this request',
      '/meta meta-short-drama -- cancel this request',
      'discarded-meta-request',
    )
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toHaveLength(1)

    first.api.discardHiddenControl('agent:main:webchat:test', 'discarded-meta-request')
    expect(first.rpc.call).toHaveBeenCalledWith('meta.drafts.discard', {
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'discarded-meta-request',
    })

    const remounted = makeOptions({
      hiddenControlStorage: first.options.hiddenControlStorage,
    })
    await remounted.api.restoreHiddenControls('agent:main:webchat:test')
    expect(remounted.rpc.call).not.toHaveBeenCalled()
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toEqual([])
  })

  it('retries a lost queue discard response without launching on remount', async () => {
    const persistentDiscardStorage = memoryStorage()
    const first = makeOptions({
      hiddenControlStorage: memoryStorage(),
      metaDiscardStorage: persistentDiscardStorage,
      enqueueHiddenControl: vi.fn(() => true),
    })
    first.stream.isStreaming.value = true
    await first.api.dispatchHiddenSend(
      '/meta meta-short-drama -- never launch after cancel',
      '/meta meta-short-drama -- never launch after cancel',
      'lost-discard-response',
    )
    first.rpc.call.mockRejectedValueOnce(new Error('response lost'))
    first.api.discardHiddenControl('agent:main:webchat:test', 'lost-discard-response')
    await Promise.resolve()

    const remounted = makeOptions({
      // sessionStorage was lost with the closed Desktop window; only the
      // minimal localStorage cancellation identity survives.
      hiddenControlStorage: memoryStorage(),
      metaDiscardStorage: persistentDiscardStorage,
    })
    remounted.rpc.call.mockResolvedValue({ discarded: true })
    await expect(remounted.api.flushPendingMetaDiscards(
      'agent:main:webchat:test',
    )).resolves.toEqual([])
    await remounted.api.restoreHiddenControls('agent:main:webchat:test')

    expect(remounted.rpc.call).toHaveBeenCalledTimes(1)
    expect(remounted.rpc.call).toHaveBeenCalledWith('meta.drafts.discard', {
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'lost-discard-response',
    })
    expect(remounted.rpc.call).not.toHaveBeenCalledWith(
      'chat.send',
      expect.anything(),
    )
  })

  it('treats an already accepted discard as terminal without replaying it', async () => {
    const persistentDiscardStorage = memoryStorage()
    persistPendingMetaDiscard({
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'already-accepted-discard',
    }, persistentDiscardStorage)
    const remounted = makeOptions({
      hiddenControlStorage: memoryStorage(),
      metaDiscardStorage: persistentDiscardStorage,
    })
    remounted.rpc.call.mockResolvedValue({ discarded: false, accepted: true })

    await expect(remounted.api.flushPendingMetaDiscards(
      'agent:main:webchat:test',
    )).resolves.toEqual([])
    await remounted.api.restoreHiddenControls('agent:main:webchat:test')

    expect(remounted.rpc.call).toHaveBeenCalledTimes(1)
    expect(remounted.rpc.call).not.toHaveBeenCalledWith('chat.send', expect.anything())
    expect(listPendingMetaDiscards(
      'agent:main:webchat:test',
      persistentDiscardStorage,
    )).toEqual([])
  })

  it('retains an ambiguous hidden send for an exact-id reconnect retry', async () => {
    const first = makeOptions()
    first.rpc.call.mockRejectedValue(new Error('response lost'))
    await expect(first.api.dispatchHiddenSend(
      '/meta meta-paper-write -- retained request',
      '/meta meta-paper-write -- retained request',
      'ambiguous-meta-request',
    )).resolves.toMatchObject({ status: 'unknown' })
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toHaveLength(1)

    const reconnected = makeOptions({
      hiddenControlStorage: first.options.hiddenControlStorage,
    })
    await reconnected.api.restoreHiddenControls('agent:main:webchat:test')
    expect(reconnected.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: 'ambiguous-meta-request',
    }))
    expect(listHiddenControls(
      'agent:main:webchat:test',
      first.options.hiddenControlStorage,
    )).toEqual([])
  })

  it('uses sessions.steer.v2 only when the active turn explicitly allows same-turn text', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        accepted: true,
        replayed: false,
        turn_id: 'turn-current',
        user_message_id: 'user-steer-1',
        disposition: 'steering',
      }),
    }
    const { api, options, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith('sessions.steer.v2', {
      key: 'agent:main:webchat:test',
      message: 'hello',
      expected_turn_id: 'turn-current',
      client_request_id: expect.any(String),
      client_message_id: expect.any(String),
      surface_id: 'webui',
      _source: { runMode: 'safe' },
    })
    expect(rpc.call).not.toHaveBeenCalledWith('chat.send', expect.anything())
    expect(rpc.call).not.toHaveBeenCalledWith('chat.abort', expect.anything())
    expect(stream.checkpointForUserMessage).toHaveBeenCalledWith('turn-current')
    expect(options.messages.value).toContainEqual(expect.objectContaining({
      role: 'user',
      text: 'hello',
      messageId: 'user-steer-1',
      turnId: 'turn-current',
      inputDisposition: 'steering',
    }))
  })

  it.each([
    {
      name: 'an old gateway',
      supportsMethod: () => false,
      capability: { mode: 'same_turn' as const, expected_turn_id: 'turn-current' },
    },
    {
      name: 'a queue-only active mode',
      supportsMethod: (method: string) => method === 'sessions.steer.v2',
      capability: { mode: 'queue_only' as const, expected_turn_id: 'turn-current' },
    },
    {
      name: 'an unsupported input-kind snapshot',
      supportsMethod: (method: string) => method === 'sessions.steer.v2',
      capability: {
        mode: 'same_turn' as const,
        expected_turn_id: 'turn-current',
        input_kinds: ['attachment'],
      },
    },
  ])('visibly queues instead of using legacy cancel-style steer for $name', async ({
    supportsMethod,
    capability,
  }) => {
    const enqueuePendingInput = vi.fn(() => true)
    const { api, rpc, stream } = makeOptions({
      supportsMethod,
      activeSteerCapability: ref(capability),
      busySendMode: ref<BusySendMode>('steer'),
      enqueuePendingInput,
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(enqueuePendingInput).toHaveBeenCalledWith('hello', undefined)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('does not expose a stale capability from a different active turn', () => {
    const { api } = makeOptions({
      ...sameTurnSteerOptions('turn-old'),
      activeStreamTaskId: ref('turn-current'),
    })

    expect(api.supportsSameTurnSteer()).toBe(false)
  })

  it.each(['/status', '!pwd'])(
    'keeps busy control input %s in the composer without delayed delivery',
    async input => {
      const enqueuePendingInput = vi.fn(() => true)
      const executeSlashCommand = vi.fn(async () => true)
      const inputText = ref(input)
      const { api, rpc, stream } = makeOptions({
        ...sameTurnSteerOptions(),
        inputText,
        busySendMode: ref<BusySendMode>('steer'),
        enqueuePendingInput,
        executeSlashCommand,
      })
      stream.isStreaming.value = true

      await api.onSend()

      expect(enqueuePendingInput).not.toHaveBeenCalled()
      expect(executeSlashCommand).not.toHaveBeenCalled()
      expect(rpc.call).not.toHaveBeenCalled()
      expect(inputText.value).toBe(input)
    },
  )

  it.each(['/status', '!pwd'])(
    'rejects a direct queued-steer attempt for control input %s without RPC',
    async input => {
      const { api, rpc, stream } = makeOptions({
        ...sameTurnSteerOptions(),
        busySendMode: ref<BusySendMode>('steer'),
      })
      stream.isStreaming.value = true

      await expect(api.sendQueuedSteer({
        pendingUiId: `pending-ui-control-${input}`,
        text: input,
        attachments: [],
        intent: null,
      })).resolves.toBe('not_sent')

      expect(rpc.call).not.toHaveBeenCalled()
    },
  )

  it('keeps a queued literal slash out of the Steer RPC', async () => {
    const { api, rpc, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      busySendMode: ref<BusySendMode>('steer'),
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-literal-steer',
      text: '//coding',
      attachments: [],
      intent: null,
    }
    stream.isStreaming.value = true

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')

    expect(rpc.call).not.toHaveBeenCalled()
  })

  it.each(['//coding', '///usr/bin/env'])(
    'queues a busy literal slash %s instead of attempting Steer',
    async literalText => {
      const enqueuePendingInput = vi.fn(() => true)
      const { api, rpc, stream } = makeOptions({
        ...sameTurnSteerOptions(),
        inputText: ref(literalText),
        busySendMode: ref<BusySendMode>('steer'),
        enqueuePendingInput,
      })
      stream.isStreaming.value = true

      await api.onSend()

      expect(enqueuePendingInput).toHaveBeenCalledWith(literalText, undefined)
      expect(rpc.call).not.toHaveBeenCalled()
    },
  )

  it('falls back safely to the visible pending queue when v2 rejects before admission', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        accepted: false,
        fallback_safe: true,
        failure_code: 'turn_mismatch',
      }),
    }
    const { api, options, stream, pendingQueue } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(pendingQueue.value).toMatchObject([{
      text: 'hello',
      attachments: [],
      intent: null,
    }])
    expect(pendingQueue.value[0]).not.toHaveProperty('steerAttempt')
    expect(options.messages.value).toEqual([])
    expect(rpc.call).not.toHaveBeenCalledWith('chat.send', expect.anything())
  })

  it('preserves the draft and skips project preflight while live delivery is blocked', async () => {
    const attachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'draft.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-draft',
    }
    const pendingAttachments = ref<Attachment[]>([attachment])
    const validateActiveProjectBeforeSend = vi.fn(async () => null)
    const { api, options, rpc } = makeOptions({
      pendingAttachments,
      sendBlockedReason: ref('Live updates are unavailable'),
      validateActiveProjectBeforeSend,
    })

    await api.onSend()

    expect(validateActiveProjectBeforeSend).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.inputText.value).toBe('hello')
    expect(options.pendingAttachments.value).toEqual([attachment])
    expect(options.messages.value).toEqual([])
  })

  it('preserves queued and hidden sends while live delivery is blocked', async () => {
    const blocker = ref<string | null>('Live updates are unavailable')
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-live-blocked',
      text: 'keep this queued',
      attachments: [],
      intent: null,
    }
    const { api, options, rpc } = makeOptions({ sendBlockedReason: blocker })

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('deferred')
    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')
    await api.dispatchHiddenSend('provider confirmation', 'Confirmed')

    expect(rpc.call).not.toHaveBeenCalled()
    expect(queued).toEqual({
      pendingUiId: 'pending-ui-live-blocked',
      text: 'keep this queued',
      attachments: [],
      intent: null,
    })
    expect(options.inputText.value).toBe('hello')
    expect(options.messages.value).toEqual([])
  })

  it('queues an immutable hidden confirmation while live delivery is blocked', async () => {
    const enqueueHiddenControl = vi.fn(() => true)
    const { api, options, rpc } = makeOptions({
      sendBlockedReason: ref('Live updates are unavailable'),
      enqueueHiddenControl,
    })

    await expect(
      api.dispatchHiddenSend('provider confirmation', 'Confirmed'),
    ).resolves.toMatchObject({ status: 'queued', reason: 'queued' })

    expect(enqueueHiddenControl).toHaveBeenCalledWith(
      expect.objectContaining({
        text: 'provider confirmation',
        displayText: 'Confirmed',
        clientRequestId: expect.any(String),
        sessionKey: 'agent:main:webchat:test',
      }),
    )
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.inputText.value).toBe('hello')
    expect(options.messages.value).toEqual([])
  })

  it('retries a hidden queue item with one stable request identity and bubble', async () => {
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('response lost'), {
          retryable: true,
        }))
        .mockResolvedValueOnce({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-hidden',
        }),
    }
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-hidden-retry',
      text: 'provider confirmation',
      displayTextOverride: 'Confirmed',
      attachments: [],
      intent: null,
      hiddenControl: true,
      ownerSessionKey: 'agent:main:webchat:test',
    }
    const { api, options } = makeOptions({ rpc })

    await expect(api.dispatchQueuedHiddenSend(
      queued,
      queued.ownerSessionKey!,
    )).resolves.toBe('retryable_failure')
    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(queued.hiddenClientRequestId).toBe(firstParams.clientRequestId)
    expect(queued.hiddenClientMessageId).toBe(firstParams.clientMessageId)
    expect(queued.hiddenVisibleCommitted).toBe(true)

    await expect(api.dispatchQueuedHiddenSend(
      queued,
      queued.ownerSessionKey!,
    )).resolves.toBe('accepted')

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(options.messages.value.filter(message => (
      message.role === 'user' && message.text === 'Confirmed'
    ))).toHaveLength(1)
  })

  it('keeps an unknown hidden acceptance in the durable outbox', async () => {
    const enqueueHiddenControl = vi.fn(() => true)
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('response lost'), {
        retryable: true,
      })),
    }
    const { api, options } = makeOptions({
      rpc,
      enqueueHiddenControl,
    })

    await expect(
      api.dispatchHiddenSend('provider confirmation', 'Confirmed'),
    ).resolves.toMatchObject({ status: 'unknown', reason: 'response_unknown' })

    expect(enqueueHiddenControl).not.toHaveBeenCalled()
    expect(options.messages.value.filter(message => message.role === 'error')).toHaveLength(1)
  })

  it('rechecks live delivery after active-project validation resolves', async () => {
    const blocker = ref<string | null>(null)
    let finishPreflight!: () => void
    const validateActiveProjectBeforeSend = vi.fn(() => new Promise<string | null>(
      resolve => {
        finishPreflight = () => resolve(null)
      },
    ))
    const { api, options, rpc } = makeOptions({
      sendBlockedReason: blocker,
      validateActiveProjectBeforeSend,
    })

    const send = api.onSend()
    await vi.waitFor(() => expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce())
    blocker.value = 'Live updates are unavailable'
    finishPreflight()
    await send

    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.inputText.value).toBe('hello')
    expect(options.messages.value).toEqual([])
  })

  it('sends the clicked snapshot without clearing edits made during project validation', async () => {
    const originalAttachment: Attachment = {
      kind: 'staged',
      local_id: 31,
      name: 'original.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-original',
    }
    const laterAttachment: Attachment = {
      kind: 'staged',
      local_id: 32,
      name: 'later.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-later',
    }
    const inputText = ref('original prompt')
    const pendingAttachments = ref<Attachment[]>([originalAttachment])
    const composerRevision = ref(1)
    let finishPreflight!: () => void
    const validateActiveProjectBeforeSend = vi.fn(() => new Promise<string | null>(
      resolve => {
        finishPreflight = () => resolve(null)
      },
    ))
    const { api, options, rpc } = makeOptions({
      inputText,
      pendingAttachments,
      composerRevision,
      validateActiveProjectBeforeSend,
    })

    const send = api.onSend()
    await vi.waitFor(() => expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce())
    inputText.value = 'new draft typed while validating'
    pendingAttachments.value = [originalAttachment, laterAttachment]
    composerRevision.value += 1
    finishPreflight()
    await send

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: 'original prompt',
      attachments: [
        expect.objectContaining({ file_uuid: 'file-original' }),
      ],
    }))
    expect(options.messages.value).toContainEqual(expect.objectContaining({
      role: 'user',
      text: 'original prompt',
    }))
    expect(inputText.value).toBe('new draft typed while validating')
    expect(pendingAttachments.value).toEqual([laterAttachment])
  })

  it('cancels an automatic send after any composer edit, even if text is restored', async () => {
    const inputText = ref('automatic prompt')
    const composerRevision = ref(4)
    let finishPreflight!: () => void
    const validateActiveProjectBeforeSend = vi.fn(() => new Promise<string | null>(
      resolve => {
        finishPreflight = () => resolve(null)
      },
    ))
    const { api, options, rpc } = makeOptions({
      inputText,
      composerRevision,
      validateActiveProjectBeforeSend,
    })

    const send = api.onSend({ cancelIfComposerChanged: true })
    await vi.waitFor(() => expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce())
    inputText.value = 'operator takeover'
    composerRevision.value += 1
    inputText.value = 'automatic prompt'
    composerRevision.value += 1
    finishPreflight()
    await send

    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(inputText.value).toBe('automatic prompt')
  })

  it('rechecks live delivery after attachment preparation resolves', async () => {
    const attachment: Attachment = {
      kind: 'staged',
      local_id: 2,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const blocker = ref<string | null>(null)
    const pendingAttachments = ref<Attachment[]>([attachment])
    let finishPreparation!: () => void
    const prepareAttachmentsForSend = vi.fn(() => new Promise<boolean>(
      resolve => {
        finishPreparation = () => resolve(true)
      },
    ))
    const { api, options, rpc } = makeOptions({
      pendingAttachments,
      sendBlockedReason: blocker,
      prepareAttachmentsForSend,
    })

    const send = api.onSend()
    await vi.waitFor(() => expect(prepareAttachmentsForSend).toHaveBeenCalledOnce())
    blocker.value = 'Live updates are unavailable'
    finishPreparation()
    await send

    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.inputText.value).toBe('hello')
    expect(options.pendingAttachments.value).toEqual([attachment])
    expect(options.messages.value).toEqual([])
  })

  it.each(['resolving', 'unavailable', 'removed', 'unknown', 'error'])(
    'does not mutate or call chat.send when project preflight returns %s',
    async reason => {
      const validateActiveProjectBeforeSend = vi.fn(async () => reason)
      const { api, options, rpc } = makeOptions({
        validateActiveProjectBeforeSend,
      })

      await api.onSend()

      expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
      expect(rpc.call).not.toHaveBeenCalledWith('chat.send', expect.anything())
      expect(options.inputText.value).toBe('hello')
      expect(options.messages.value).toEqual([])
    },
  )

  it('sends only after project preflight confirms ready', async () => {
    const validateActiveProjectBeforeSend = vi.fn(async () => null)
    const { api, rpc } = makeOptions({
      validateActiveProjectBeforeSend,
    })

    await api.onSend()

    expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ message: 'hello' }),
    )
  })

  it('blocks hidden control sends when the active project preflight fails', async () => {
    const validateActiveProjectBeforeSend = vi.fn(async () => 'removed')
    const { api, options, rpc } = makeOptions({
      validateActiveProjectBeforeSend,
    })

    await api.dispatchHiddenSend('provider confirmation', 'Confirmed')

    expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
  })

  it('keeps queued delivery owned when project validation blocks it', async () => {
    const validateActiveProjectBeforeSend = vi.fn(async () => 'removed')
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-project-blocked',
      text: 'keep queued',
      attachments: [],
      intent: null,
      ownerSessionKey: 'agent:main:webchat:test',
    }
    const { api, options, rpc } = makeOptions({
      validateActiveProjectBeforeSend,
    })

    await expect(api.sendQueuedFollowup(
      queued,
      'agent:main:webchat:test',
    )).resolves.toBe('deferred')
    await expect(api.sendQueuedSteer(
      queued,
      'agent:main:webchat:test',
    )).resolves.toBe('not_sent')

    expect(validateActiveProjectBeforeSend).toHaveBeenCalledTimes(2)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.inputText.value).toBe('hello')
    expect(queued.text).toBe('keep queued')
  })

  it('rechecks live and session ownership after queued project validation', async () => {
    const blocker = ref<string | null>(null)
    let finishPreflight!: () => void
    const validateActiveProjectBeforeSend = vi.fn(() => new Promise<string | null>(
      resolve => {
        finishPreflight = () => resolve(null)
      },
    ))
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-project-race',
      text: 'queued follow-up',
      attachments: [],
      intent: null,
      ownerSessionKey: 'agent:main:webchat:test',
    }
    const { api, options, rpc } = makeOptions({
      sendBlockedReason: blocker,
      validateActiveProjectBeforeSend,
    })

    const send = api.sendQueuedFollowup(queued, 'agent:main:webchat:test')
    await vi.waitFor(() => expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce())
    blocker.value = 'Live updates are unavailable'
    finishPreflight()

    await expect(send).resolves.toBe('deferred')
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.inputText.value).toBe('hello')

    options.sessionKey.value = 'agent:main:webchat:other'
    blocker.value = null
    await expect(api.sendQueuedFollowup(
      queued,
      'agent:main:webchat:test',
    )).resolves.toBe('not_sent')
    expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
  })

  it('admits only one send while an active-project preflight is pending', async () => {
    let finishPreflight!: () => void
    const validateActiveProjectBeforeSend = vi.fn(() => new Promise<string | null>(
      resolve => {
        finishPreflight = () => resolve(null)
      },
    ))
    const { api, rpc } = makeOptions({
      validateActiveProjectBeforeSend,
    })

    const first = api.onSend()
    const second = api.onSend()
    expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()

    finishPreflight()
    await Promise.all([first, second])

    expect(rpc.call).toHaveBeenCalledTimes(1)
    expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ message: 'hello' }),
    )
  })

  it('binds a new project task to its workspace and preserves that binding on retry', async () => {
    const pendingSessionIntent = ref<string | null>('new_chat')
    const pendingWorkspaceId = ref<string | null>('project-a')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('database busy'), { accepted: false }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test' }),
    }
    const { api } = makeOptions({
      rpc,
      pendingSessionIntent,
      pendingWorkspaceId,
    })

    await api.onSend()

    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toEqual(expect.objectContaining({
      intent: 'new_chat',
      workspaceId: 'project-a',
    }))
    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(pendingWorkspaceId.value).toBe('project-a')

    await api.onSend()

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(expect.objectContaining({
      clientRequestId: firstParams.clientRequestId,
      intent: 'new_chat',
      workspaceId: 'project-a',
    }))
    expect(pendingWorkspaceId.value).toBeNull()
  })

  it('dispatches a server-staged follow-up by its durable identity', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-pending-dispatch',
        message_id: 'message-pending-dispatch',
      }),
    }
    const { api } = makeOptions({ rpc })
    const item: ChatPendingItem = {
      pendingUiId: 'pending-stable-id',
      text: 'dispatch this staged input',
      attachments: [],
      intent: null,
      ownerSessionKey: 'agent:main:webchat:test',
      pendingInputId: 'pending-stable-id',
      pendingClientRequestId: 'request-stable-id',
      pendingClientMessageId: 'message-stable-id',
      pendingRequestFingerprint: 'fingerprint-stable-id',
      pendingPersistenceState: 'staged',
    }

    await expect(api.sendQueuedFollowup(item)).resolves.toBe('accepted')
    expect(rpc.call).toHaveBeenCalledWith('sessions.pending_inputs.dispatch', {
      key: 'agent:main:webchat:test',
      pendingInputId: 'pending-stable-id',
      clientRequestId: 'request-stable-id',
      requestFingerprint: 'fingerprint-stable-id',
    })
  })

  it('reuses IndexedDB-only identities when an older Gateway lacks staged dispatch', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-local-only-dispatch',
        message_id: 'message-local-only-dispatch',
      }),
    }
    const { api } = makeOptions({ rpc })
    const item: ChatPendingItem = {
      pendingUiId: 'pending-local-only-id',
      text: 'dispatch this browser-only input',
      attachments: [],
      intent: null,
      ownerSessionKey: 'agent:main:webchat:test',
      pendingInputId: 'pending-local-only-id',
      pendingClientRequestId: 'request-local-only-id',
      pendingClientMessageId: 'message-local-only-id',
      pendingPersistenceState: 'local_only',
    }

    await expect(api.sendQueuedFollowup(item)).resolves.toBe('accepted')
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      clientRequestId: 'request-local-only-id',
      clientMessageId: 'message-local-only-id',
      message: 'dispatch this browser-only input',
    }))
  })

  it('dispatches server-restored attachment material without an upload UUID', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-pending-material',
        message_id: 'message-pending-material',
      }),
    }
    const { api } = makeOptions({ rpc })
    const item: ChatPendingItem = {
      pendingUiId: 'pending-material-id',
      text: '',
      attachments: [{
        kind: 'staged',
        local_id: -1,
        name: 'restored.txt',
        mime: 'text/plain',
        size: 12,
        durable_material: true,
      }],
      intent: null,
      ownerSessionKey: 'agent:main:webchat:test',
      pendingInputId: 'pending-material-id',
      pendingClientRequestId: 'request-material-id',
      pendingClientMessageId: 'message-material-id',
      pendingRequestFingerprint: 'fingerprint-material-id',
      pendingPersistenceState: 'staged',
    }

    await expect(api.sendQueuedFollowup(item)).resolves.toBe('accepted')
    expect(rpc.call).toHaveBeenCalledWith('sessions.pending_inputs.dispatch', {
      key: 'agent:main:webchat:test',
      pendingInputId: 'pending-material-id',
      clientRequestId: 'request-material-id',
      requestFingerprint: 'fingerprint-material-id',
    })
  })

  it('does not materialize a project draft before chat.send accepts it', async () => {
    const pendingSessionIntent = ref<string | null>('new_chat')
    const pendingWorkspaceId = ref<string | null>('project-a')
    const materializeDraftSession = vi.fn()
    const intentTransitions: Array<string | null> = []
    watch(pendingSessionIntent, value => intentTransitions.push(value))
    let rejectSend!: (reason: unknown) => void
    const rpc = {
      call: vi.fn(() => new Promise((_, reject) => {
        rejectSend = reject
      })) as UseChatSendOptions['rpc']['call'],
    }
    const { api, options, stream } = makeOptions({
      rpc,
      pendingSessionIntent,
      pendingWorkspaceId,
      materializeDraftSession,
    })
    vi.mocked(stream.startStreaming).mockImplementation(() => {
      stream.isStreaming.value = true
    })

    const send = api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    await nextTick()

    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(intentTransitions).toEqual([])
    expect(materializeDraftSession).not.toHaveBeenCalled()

    options.inputText.value = 'follow-up while first send is pending'
    await api.onSend()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(options.enqueuePendingInput).toHaveBeenCalledWith(
      'follow-up while first send is pending',
      undefined,
    )

    rejectSend(Object.assign(new Error('database busy'), { accepted: false }))
    await send
    await nextTick()

    expect(pendingSessionIntent.value).toBe('new_chat')
    expect(pendingWorkspaceId.value).toBe('project-a')
    expect(intentTransitions).toEqual([])
    expect(materializeDraftSession).not.toHaveBeenCalled()
  })

  it('materializes a new project task only after chat.send accepts it', async () => {
    const pendingSessionIntent = ref<string | null>('new_chat')
    const pendingWorkspaceId = ref<string | null>('project-a')
    const materializeDraftSession = vi.fn()
    const { api, options } = makeOptions({
      pendingSessionIntent,
      pendingWorkspaceId,
      materializeDraftSession,
    })

    await api.onSend()

    expect(materializeDraftSession).toHaveBeenCalledWith(options.sessionKey.value)
    expect(pendingSessionIntent.value).toBeNull()
  })

  it('queues a second draft while the first send has not announced steer capability', async () => {
    const pendingSessionIntent = ref<string | null>('new_chat')
    const pendingWorkspaceId = ref<string | null>('project-a')
    const busySendMode = ref<BusySendMode>('steer')
    let rejectFirst!: (reason: unknown) => void
    const rpc = {
      call: vi.fn()
        .mockImplementationOnce(() => new Promise((_, reject) => {
          rejectFirst = reject
        }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test' }),
    }
    const { api, options, stream } = makeOptions({
      rpc,
      pendingSessionIntent,
      pendingWorkspaceId,
      busySendMode,
    })
    vi.mocked(stream.startStreaming).mockImplementation(() => {
      stream.isStreaming.value = true
    })

    const firstSend = api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    options.inputText.value = 'steer while first send is pending'

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledOnce()
    expect(options.enqueuePendingInput).toHaveBeenCalledWith(
      'steer while first send is pending',
      undefined,
    )
    expect(pendingWorkspaceId.value).toBe('project-a')

    rejectFirst(Object.assign(new Error('database busy'), { accepted: false }))
    await firstSend
    expect(pendingWorkspaceId.value).toBe('project-a')
  })

  it('reuses a v2 steer fingerprint while a project binding remains pending', async () => {
    const pendingWorkspaceId = ref<string | null>('project-a')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('response lost'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test' }),
    }
    const { api, stream, pendingQueue } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      busySendMode: ref<BusySendMode>('steer'),
      pendingSessionIntent: ref(null),
      pendingWorkspaceId,
    })
    stream.isStreaming.value = true

    await api.onSend()
    const firstParams = rpc.call.mock.calls[0]?.[1]
    const retry = pendingQueue.value[0]!
    await api.sendQueuedSteer(retry)

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(firstParams).not.toHaveProperty('workspaceId')
    expect(firstParams).not.toHaveProperty('queueMode')
    expect(pendingWorkspaceId.value).toBe('project-a')
  })

  it('sends the selected sandbox run mode as trusted source metadata', async () => {
    const { api, rpc } = makeOptions({
      runMode: ref('safe'),
    } as Partial<UseChatSendOptions>)

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      _source: { runMode: 'safe' },
    }))
  })

  it('sends the policy-default Full hint for a project task', async () => {
    const { api, rpc } = makeOptions({
      runMode: ref('full'),
      pendingSessionIntent: ref('new_chat'),
      pendingWorkspaceId: ref('project-a'),
    })

    await api.onSend()

    const params = rpc.call.mock.calls[0]?.[1]
    expect(params).toMatchObject({
      workspaceId: 'project-a',
      _source: { runMode: 'full' },
    })
  })

  it('sends an explicitly selected Full hint for a project task', async () => {
    const { api, rpc } = makeOptions({
      runMode: ref('full'),
      pendingSessionIntent: ref('new_chat'),
      pendingWorkspaceId: ref('project-a'),
    })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({
        workspaceId: 'project-a',
        _source: { runMode: 'full' },
      }),
    )
  })

  it('serializes only sendable attachments and leaves failed attachments in the composer', async () => {
    const failed: Attachment = {
      kind: 'failed',
      local_id: 1,
      name: 'failed.pdf',
      mime: 'application/pdf',
      error: 'HTTP 500',
      file: new File(['failed'], 'failed.pdf', { type: 'application/pdf' }),
    }
    const ready: Attachment = {
      kind: 'staged',
      local_id: 2,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const pendingAttachments = ref<Attachment[]>([failed, ready])
    const { api, options, rpc } = makeOptions({ pendingAttachments })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { type: 'application/pdf', file_uuid: 'file-ready', mime: 'application/pdf', name: 'ready.pdf' },
      ],
    }))
    expect(options.messages.value[0]).toMatchObject({
      role: 'user',
      text: 'hello',
      attachments: [
        { kind: 'staged', displayId: 'local:2', renderKey: 'local:2', name: 'ready.pdf', mime: 'application/pdf' },
      ],
    })
    expect(JSON.stringify(options.messages.value[0])).not.toContain('file-ready')
    expect(JSON.stringify(options.messages.value[0])).not.toContain('failed.pdf')
    expect(pendingAttachments.value).toEqual([failed])
  })

  it('sends a slash-derived Plan prompt through the normal attachment path', async () => {
    const ready: Attachment = {
      kind: 'staged',
      local_id: 7,
      name: 'architecture.png',
      mime: 'image/png',
      file_uuid: 'file-plan-image',
    }
    const inputText = ref('/plan analyze this architecture')
    const pendingAttachments = ref<Attachment[]>([ready])
    const executeSlashCommand = vi.fn(async () => false)
    const { api, options, rpc } = makeOptions({
      executeSlashCommand,
      inputText,
      pendingAttachments,
    })

    await api.dispatchComposerPrompt(
      'analyze this architecture',
      '/plan analyze this architecture',
    )

    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: 'analyze this architecture',
      displayText: 'analyze this architecture',
      attachments: [
        {
          type: 'image/png',
          file_uuid: 'file-plan-image',
          mime: 'image/png',
          name: 'architecture.png',
        },
      ],
    }))
    expect(options.messages.value[0]).toMatchObject({
      role: 'user',
      text: 'analyze this architecture',
      attachments: [
        {
          kind: 'staged',
          displayId: 'local:7',
          renderKey: 'local:7',
          name: 'architecture.png',
          mime: 'image/png',
        },
      ],
    })
    expect(inputText.value).toBe('')
    expect(pendingAttachments.value).toEqual([])
  })

  it('restores and idempotently retries a slash-derived Plan prompt with attachments', async () => {
    const ready: Attachment = {
      kind: 'staged',
      local_id: 8,
      name: 'diagram.png',
      mime: 'image/png',
      file_uuid: 'file-plan-retry',
    }
    const originalInput = '/plan analyze this diagram'
    const inputText = ref(originalInput)
    const pendingAttachments = ref<Attachment[]>([ready])
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('network down'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-plan-retry',
        }),
    }
    const { api, options } = makeOptions({
      inputText,
      pendingAttachments,
      rpc,
    })

    await api.dispatchComposerPrompt('analyze this diagram', originalInput)

    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(inputText.value).toBe(originalInput)
    expect(pendingAttachments.value).toEqual([ready])
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)

    await api.dispatchComposerPrompt('analyze this diagram', originalInput)

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
    expect(inputText.value).toBe('')
    expect(pendingAttachments.value).toEqual([])
  })

  it('refreshes staged uploads before serializing chat.send attachments', async () => {
    const pendingAttachments = ref<Attachment[]>([
      {
        kind: 'staged',
        local_id: 1,
        name: 'ready.pdf',
        mime: 'application/pdf',
        file_uuid: 'file-expired',
        expires_at: Date.now() / 1000 - 1,
        file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
      },
    ])
    const prepareAttachmentsForSend = vi.fn(async (context?: {
      attachments?: Attachment[]
    }) => {
      const attachments = context?.attachments
      if (!attachments) return false
      attachments[0] = {
        kind: 'staged',
        local_id: 1,
        name: 'ready.pdf',
        mime: 'application/pdf',
        file_uuid: 'file-fresh',
        expires_at: Date.now() / 1000 + 600,
        file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
      }
      return true
    })
    const { api, rpc } = makeOptions({ pendingAttachments, prepareAttachmentsForSend })

    await api.onSend()

    expect(prepareAttachmentsForSend).toHaveBeenCalledTimes(1)
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { type: 'application/pdf', file_uuid: 'file-fresh', mime: 'application/pdf', name: 'ready.pdf' },
      ],
    }))
  })

  it('does not include attachments added while preparing an earlier send', async () => {
    const initialAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'initial.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-initial',
    }
    const addedAttachment: Attachment = {
      kind: 'staged',
      local_id: 2,
      name: 'added-later.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-added-later',
    }
    const pendingAttachments = ref<Attachment[]>([initialAttachment])
    const prepareAttachmentsForSend = vi.fn(async () => {
      pendingAttachments.value = [initialAttachment, addedAttachment]
      return true
    })
    const { api, rpc } = makeOptions({ pendingAttachments, prepareAttachmentsForSend })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { type: 'application/pdf', file_uuid: 'file-initial', mime: 'application/pdf', name: 'initial.pdf' },
      ],
    }))
    expect(pendingAttachments.value).toEqual([addedAttachment])
  })

  it('does not mutate or send when attachment preparation returns false', async () => {
    const inputText = ref('hello')
    const expiredAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-expired',
      expires_at: Date.now() / 1000 - 1,
      file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
    }
    const pendingAttachments = ref<Attachment[]>([expiredAttachment])
    const prepareAttachmentsForSend = vi.fn(async () => false)
    const { api, options, rpc, stream } = makeOptions({
      inputText,
      pendingAttachments,
      prepareAttachmentsForSend,
    })

    await api.onSend()

    expect(prepareAttachmentsForSend).toHaveBeenCalledTimes(1)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toHaveLength(0)
    expect(inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([expiredAttachment])
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('does not mutate or send when session changes during attachment preparation', async () => {
    let resolvePrepare!: (ready: boolean) => void
    let prepareContext: { isCurrent?: () => boolean } | undefined
    const inputText = ref('hello')
    const sessionKey = ref('agent:main:webchat:first')
    const stagedAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
      file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
    }
    const pendingAttachments = ref<Attachment[]>([stagedAttachment])
    const prepareAttachmentsForSend = vi.fn((context?: { isCurrent?: () => boolean }) => new Promise<boolean>(resolve => {
      prepareContext = context
      resolvePrepare = resolve
    }))
    const { api, options, rpc, stream } = makeOptions({
      inputText,
      sessionKey,
      pendingAttachments,
      prepareAttachmentsForSend,
    })

    const send = api.onSend()
    sessionKey.value = 'agent:main:webchat:second'
    expect(prepareContext?.isCurrent?.()).toBe(false)
    resolvePrepare(true)
    await send

    expect(prepareAttachmentsForSend).toHaveBeenCalledTimes(1)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toHaveLength(0)
    expect(inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([stagedAttachment])
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('does not dispatch an empty failed-only attachment draft', async () => {
    const failed: Attachment = {
      kind: 'failed',
      local_id: 1,
      name: 'failed.pdf',
      mime: 'application/pdf',
      error: 'HTTP 500',
      file: new File(['failed'], 'failed.pdf', { type: 'application/pdf' }),
    }
    const pendingAttachments = ref<Attachment[]>([failed])
    const { api, rpc } = makeOptions({
      inputText: ref(''),
      pendingAttachments,
    })

    await api.onSend()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(pendingAttachments.value).toEqual([failed])
  })

  it('restores an unknown-acceptance send for idempotent retry', async () => {
    const ready: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const pendingAttachments = ref<Attachment[]>([ready])
    const pendingSessionIntent = ref<string | null>('NEW')
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const rpc = {
      call: vi.fn().mockRejectedValue(new Error('network down')),
    }
    const { api, options } = makeOptions({
      rpc,
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
    })

    await api.onSend()

    expect(pendingAttachments.value).toEqual([ready])
    expect(options.inputText.value).toBe('hello')
    expect(pendingSessionIntent.value).toBe('NEW')
    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'Send failed: network down',
    })
  })

  it('sends pending fork target and clears it after chat.send is accepted', async () => {
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const { api, rpc } = makeOptions({ pendingForkBeforeMessageId })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      forkBeforeMessageId: 'msg-B',
    }))
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('switches the session lifecycle when a stopped turn is edited into a child session', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const pendingForkBeforeMessageId = ref<string | null>(null)
    const adoptResponseSession = vi.fn()
    const rpcCall = vi.fn(async (method: string) => {
      if (method === 'chat.send') {
        return { sessionKey: childSessionKey, task_id: 'task-child' }
      }
      if (method === 'chat.abort') return { aborted: true }
      return { ok: true }
    })
    const rpc: UseChatSendOptions['rpc'] = {
      call: rpcCall as unknown as UseChatSendOptions['rpc']['call'],
    }
    const { api, options, stream } = makeOptions({
      rpc,
      sessionKey: ref(parentSessionKey),
      activeStreamSessionKey: ref(parentSessionKey),
      pendingForkBeforeMessageId,
      adoptResponseSession,
    })
    stream.isStreaming.value = true
    vi.mocked(stream.endStreaming).mockImplementation(() => {
      stream.isStreaming.value = false
    })

    api.onStop()
    // Stop remains pending until the authoritative terminal is observed.
    stream.endStreaming({ reason: 'aborted' })
    pendingForkBeforeMessageId.value = 'msg-B'
    options.inputText.value = 'edited question'
    await api.onSend()

    expect(rpcCall).toHaveBeenCalledWith('chat.abort', {
      sessionKey: parentSessionKey,
      source: 'webui_stop',
    })
    expect(rpcCall).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      sessionKey: parentSessionKey,
      forkBeforeMessageId: 'msg-B',
      message: 'edited question',
    }))
    expect(adoptResponseSession).toHaveBeenCalledOnce()
    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
  })

  it('binds the accepted user message id so stop then edit sends a real fork', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('original question')
    const messages = ref<ChatMessage[]>([])
    const pendingForkBeforeMessageId = ref<string | null>(null)
    let sendCount = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.abort') return Promise.resolve({ aborted: true }) as Promise<T>
        sendCount += 1
        if (sendCount === 1) {
          return Promise.resolve({
            sessionKey: parentSessionKey,
            task_id: 'task-original',
            user_message_id: 'message-original',
            client_message_id: params?.clientMessageId,
          }) as Promise<T>
        }
        return Promise.resolve({
          sessionKey: childSessionKey,
          task_id: 'task-edited',
          user_message_id: 'message-edited',
          client_message_id: params?.clientMessageId,
        }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      messages,
      pendingForkBeforeMessageId,
      adoptResponseSession,
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    await harness.api.onSend()

    const optimisticUser = messages.value[0]
    expect(optimisticUser?.clientId).toBeTruthy()
    expect(optimisticUser?.messageId).toBe('message-original')
    expect(rpc.call).toHaveBeenNthCalledWith(1, 'chat.send', expect.objectContaining({
      clientMessageId: optimisticUser?.clientId,
    }))

    harness.api.onStop()
    // The message action becomes available only after the stopped turn's
    // terminal closes the live stream.
    harness.stream.endStreaming({ reason: 'aborted' })
    const actions = useChatMessageActions({
      messages,
      inputText,
      isStreaming: harness.stream.isStreaming,
      sanitizeCopyText: text => text,
      stripTimePrefix: text => text,
      autoResizeTextarea: vi.fn(),
      sendCurrentInput: vi.fn(),
      sendUsageBarrierReplay: vi.fn(async () => false),
      focusComposer: vi.fn(),
      pendingForkBeforeMessageId,
    })
    actions.editMessage({
      ...optimisticUser,
      sourceIndex: 0,
    } as ChatRenderedMessage)
    expect(pendingForkBeforeMessageId.value).toBe('message-original')

    inputText.value = 'edited question'
    await harness.api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      sessionKey: parentSessionKey,
      forkBeforeMessageId: 'message-original',
    }))
    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
  })

  it('restores the pending fork target only when chat.send explicitly rejects the attempt', async () => {
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('database busy'), {
        accepted: false,
      })),
    }
    const { api } = makeOptions({ rpc, pendingForkBeforeMessageId })

    await api.onSend()

    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
  })

  it('restores the complete rejected attempt and reuses its id and metadata', async () => {
    const ready: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const inputText = ref('hello')
    const pendingAttachments = ref<Attachment[]>([ready])
    const pendingSessionIntent = ref<string | null>('NEW')
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const elevatedMode = ref('enabled')
    const runMode = ref<'safe' | 'full'>('safe')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('database busy'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test', task_id: 'task-new' }),
    }
    const { api, options } = makeOptions({
      rpc,
      inputText,
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
      elevatedMode,
      runMode,
    })

    await api.onSend()

    expect(inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([ready])
    expect(pendingSessionIntent.value).toBe('NEW')
    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toMatchObject({
      clientRequestId: expect.any(String),
      clientMessageId: expect.any(String),
      message: 'hello',
      sessionKey: 'agent:main:webchat:test',
      intent: 'NEW',
      forkBeforeMessageId: 'msg-B',
      _source: { elevated: 'enabled', runMode: 'safe' },
      attachments: [{ file_uuid: 'file-ready' }],
    })

    // Retrying this recovered attempt must keep its original fingerprint even
    // if ambient composer settings changed after the first send.
    elevatedMode.value = ''
    runMode.value = 'full'
    await api.onSend()

    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(secondParams).toEqual(firstParams)
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
    expect(inputText.value).toBe('')
    expect(pendingAttachments.value).toEqual([])
    expect(pendingSessionIntent.value).toBeNull()
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('moves an ambiguous v2 steer into an exact-id retry instead of resending as follow-up', async () => {
    const inputText = ref('steer this exact turn')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('response lost'), {
          retryable: true,
        }))
        .mockResolvedValueOnce({
          accepted: true,
          turn_id: 'turn-current',
          disposition: 'steering',
        }),
    }
    const { api, options, stream, pendingQueue } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      inputText,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    await api.onSend()
    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toMatchObject({
      key: 'agent:main:webchat:test',
      message: 'steer this exact turn',
      expected_turn_id: 'turn-current',
      client_request_id: expect.any(String),
      client_message_id: expect.any(String),
    })
    expect(firstParams).not.toHaveProperty('queueMode')
    expect(inputText.value).toBe('')
    expect(pendingQueue.value).toHaveLength(1)
    expect(pendingQueue.value[0]?.steerAttempt?.phase).toBe('acceptance_unknown')

    // Even if the active task settles before the retry, the original target
    // and request id are replayed; this must never become chat.send follow-up.
    stream.isStreaming.value = false
    await api.sendQueuedSteer(pendingQueue.value[0]!)

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(rpc.call.mock.calls.map(call => call[0])).toEqual([
      'sessions.steer.v2',
      'sessions.steer.v2',
    ])
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
  })

  it('treats a fulfilled steer response without accepted as unknown despite tempting fields', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        turn_id: 'turn-current',
        user_message_id: 'user-unproven',
        disposition: 'steering',
        revision: 1,
      }),
    }
    const { api, options, stream, pendingQueue } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(options.messages.value).toEqual([])
    expect(pendingQueue.value).toHaveLength(1)
    expect(pendingQueue.value[0]?.steerAttempt?.phase).toBe('acceptance_unknown')
    expect(pendingQueue.value[0]?.steerAttempt?.request).toMatchObject(
      rpc.call.mock.calls[0]?.[1],
    )
    expect(options.scheduleHistorySync).toHaveBeenCalledOnce()
  })

  it.each([
    { accepted: true, expectedLength: 0, expectedPhase: undefined },
    { accepted: false, expectedLength: 1, expectedPhase: undefined },
  ])('settles accepted=$accepted against the source item after navigating away', async ({
    accepted,
    expectedLength,
    expectedPhase,
  }) => {
    let resolveSteer!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>((resolve) => {
        resolveSteer = resolve as (value: unknown) => void
      })) as UseChatSendOptions['rpc']['call'],
    }
    const sessionKey = ref('agent:main:webchat:source')
    const restoreSteerIntoComposer = vi.fn()
    const { api, options, stream, pendingQueue } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      sessionKey,
      busySendMode: ref<BusySendMode>('steer'),
      restoreSteerIntoComposer,
    })
    stream.isStreaming.value = true

    const send = api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    sessionKey.value = 'agent:main:webchat:other'
    resolveSteer(accepted
      ? { accepted: true, disposition: 'steering', turn_id: 'turn-current' }
      : { accepted: false, retryable: false, fallback_safe: false })
    await send

    expect(options.messages.value).toEqual([])
    expect(pendingQueue.value).toHaveLength(expectedLength)
    expect(pendingQueue.value[0]?.steerAttempt?.phase).toBe(expectedPhase)
    expect(restoreSteerIntoComposer).not.toHaveBeenCalled()
  })

  it('re-homes a replayed promoted steer without waiting for its disposition event', async () => {
    const messages = ref<ChatMessage[]>([
      {
        role: 'user',
        text: 'original request',
        ts: 1,
        messageId: 'user-old',
        turnId: 'turn-current',
      },
      {
        role: 'user',
        text: 'late adjustment',
        ts: 2,
        clientId: 'client-steer',
        turnId: 'turn-current',
        inputDisposition: 'steering',
        inputDispositionRevision: 1,
        steerClientRequestId: 'request-steer',
        steerClientMessageId: 'client-steer',
      },
      {
        role: 'assistant',
        text: 'completed old-turn output',
        ts: 3,
        messageId: 'assistant-old',
        turnId: 'turn-current',
      },
      {
        role: 'router',
        text: '',
        ts: 4,
        messageId: 'router-new',
        turnId: 'turn-promoted',
      },
    ])
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-late-adjustment',
      text: 'late adjustment',
      attachments: [],
      intent: null,
      steerAttempt: {
        phase: 'acceptance_unknown',
        request: {
          key: 'agent:main:webchat:test',
          message: 'late adjustment',
          expected_turn_id: 'turn-current',
          client_request_id: 'request-steer',
          client_message_id: 'client-steer',
          surface_id: 'webui',
          _source: { runMode: 'safe' },
        },
      },
    }
    const rpc = {
      call: vi.fn().mockResolvedValue({
        accepted: true,
        replayed: true,
        turn_id: 'turn-promoted',
        promoted_turn_id: 'turn-promoted',
        promoted_from_turn_id: 'turn-current',
        user_message_id: 'user-steer',
        disposition: 'promoted',
        revision: 2,
      }),
    }
    const { api } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      messages,
    })

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('accepted')

    expect(messages.value.map(message => message.messageId || message.clientId)).toEqual([
      'user-old',
      'assistant-old',
      'user-steer',
      'router-new',
    ])
    expect(messages.value[2]).toMatchObject({
      turnId: 'turn-promoted',
      promotedFromTurnId: 'turn-current',
      inputDisposition: 'promoted',
      inputDispositionRevision: 2,
    })
  })

  it('sends a pure-text queued steer without reading or mutating the live composer', async () => {
    const draftAttachment: Attachment = {
      kind: 'staged',
      local_id: 20,
      name: 'draft.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-draft',
    }
    const laterDraftAttachment: Attachment = {
      kind: 'inline',
      local_id: 22,
      name: 'later.txt',
      mime: 'text/plain',
      data: 'bGF0ZXI=',
    }
    const inputText = ref('keep this draft')
    const pendingAttachments = ref<Attachment[]>([draftAttachment])
    const pendingSessionIntent = ref<string | null>('DRAFT')
    const pendingForkBeforeMessageId = ref<string | null>('msg-draft-parent')
    let resolveSend!: (value: unknown) => void
    const rpc = {
      call: vi.fn().mockImplementation(() => new Promise(resolve => {
        resolveSend = resolve
      })),
    }
    const { api, options, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      inputText,
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
    })
    stream.isStreaming.value = true
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-composer-snapshot',
      text: 'steer with the queued snapshot',
      attachments: [],
      intent: null,
    }

    const send = api.sendQueuedSteer(queued)
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())

    expect(inputText.value).toBe('keep this draft')
    expect(pendingAttachments.value).toEqual([draftAttachment])
    expect(pendingSessionIntent.value).toBe('DRAFT')
    expect(pendingForkBeforeMessageId.value).toBe('msg-draft-parent')

    // Edits made after dispatch belong to the live composer and must survive
    // the queued RPC settling as well.
    inputText.value = 'typed while steering'
    pendingAttachments.value = [draftAttachment, laterDraftAttachment]
    pendingSessionIntent.value = 'LATER'
    pendingForkBeforeMessageId.value = 'msg-later-parent'
    resolveSend({
      accepted: true,
      turn_id: 'turn-current',
      disposition: 'steering',
    })

    await expect(send).resolves.toBe('accepted')
    expect(inputText.value).toBe('typed while steering')
    expect(pendingAttachments.value).toEqual([draftAttachment, laterDraftAttachment])
    expect(pendingSessionIntent.value).toBe('LATER')
    expect(pendingForkBeforeMessageId.value).toBe('msg-later-parent')
    expect(options.closeSlashMenu).not.toHaveBeenCalled()
    expect(rpc.call).toHaveBeenCalledWith('sessions.steer.v2', expect.objectContaining({
      message: 'steer with the queued snapshot',
      expected_turn_id: 'turn-current',
      client_request_id: expect.any(String),
      client_message_id: expect.any(String),
    }))
    expect(rpc.call.mock.calls[0]?.[1]).not.toHaveProperty('queueMode')
    expect(rpc.call.mock.calls[0]?.[1]).not.toHaveProperty('attachments')
    expect(rpc.call.mock.calls[0]?.[1]).not.toHaveProperty('intent')
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
  })

  it('atomically steers a durable queued item with its staged identity', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        accepted: true,
        turn_id: 'turn-current',
        disposition: 'steering',
      }),
    }
    const { api, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      supportsMethod: method => (
        method === 'sessions.steer.v2'
        || method === 'sessions.pending_inputs.steer'
      ),
      rpc,
    })
    stream.isStreaming.value = true
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-durable-steer',
      text: 'steer the staged message',
      attachments: [],
      intent: null,
      pendingInputId: 'pending-durable-steer',
      pendingClientRequestId: 'request-durable-steer',
      pendingClientMessageId: 'message-durable-steer',
      pendingRequestFingerprint: 'sha256:durable-steer',
      pendingServerRevision: 3,
      pendingPersistenceState: 'staged',
    }

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('accepted')

    expect(rpc.call).toHaveBeenCalledWith(
      'sessions.pending_inputs.steer',
      expect.objectContaining({
        key: 'agent:main:webchat:test',
        message: 'steer the staged message',
        expected_turn_id: 'turn-current',
        client_request_id: 'request-durable-steer',
        client_message_id: 'message-durable-steer',
        pendingInputId: 'pending-durable-steer',
        requestFingerprint: 'sha256:durable-steer',
        expectedRevision: 3,
      }),
    )
  })

  it('does not steer a durable queued item through an older gateway', async () => {
    const { api, rpc, stream } = makeOptions({
      ...sameTurnSteerOptions(),
    })
    stream.isStreaming.value = true
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-old-gateway',
      text: 'keep this queued',
      attachments: [],
      intent: null,
      pendingInputId: 'pending-old-gateway',
      pendingClientRequestId: 'request-old-gateway',
      pendingClientMessageId: 'message-old-gateway',
      pendingRequestFingerprint: 'sha256:old-gateway',
      pendingServerRevision: 1,
      pendingPersistenceState: 'staged',
    }

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('allows an explicit queued Steer while authoritative A is running', async () => {
    const taskOwnership = useChatTaskOwnership()
    taskOwnership.noteRunning('turn-current')
    const rpc = {
      call: vi.fn().mockResolvedValue({
        accepted: true,
        turn_id: 'turn-current',
        disposition: 'steering',
      }),
    }
    const { api, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      taskOwnership,
      rpc,
    })
    stream.isStreaming.value = true
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-authoritative-running-steer',
      text: 'apply this correction to A',
      attachments: [],
      intent: null,
    }

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('accepted')
    expect(rpc.call).toHaveBeenCalledWith(
      'sessions.steer.v2',
      expect.objectContaining({
        message: 'apply this correction to A',
        expected_turn_id: 'turn-current',
      }),
    )
  })

  it('defers an automatic queued follow-up if another run became active', async () => {
    const inputText = ref('new live draft')
    const { api, rpc, stream } = makeOptions({ inputText })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-active-run',
      text: 'wait for the active run',
      attachments: [],
      intent: null,
    }
    stream.isStreaming.value = true

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('deferred')

    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('new live draft')

    stream.isStreaming.value = false
    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('accepted')
    expect(rpc.call.mock.calls[0]?.[1]).toHaveProperty('queueMode', 'followup')
    expect(inputText.value).toBe('new live draft')
  })

  it('does not start a queued delivery while the composer is refreshing attachments', async () => {
    let resolvePreparation!: (ready: boolean) => void
    let preparing = false
    const prepareAttachmentsForSend = vi.fn(() => {
      preparing = true
      return new Promise<boolean>(resolve => {
        resolvePreparation = resolve
      })
    })
    const { api, rpc } = makeOptions({
      prepareAttachmentsForSend,
      hasPendingAttachmentWork: () => preparing,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-attachment-preparation',
      text: 'wait for composer preparation',
      attachments: [],
      intent: null,
    }

    const composerSend = api.onSend()
    await vi.waitFor(() => expect(prepareAttachmentsForSend).toHaveBeenCalledOnce())
    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')
    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('deferred')
    expect(rpc.call).not.toHaveBeenCalled()

    preparing = false
    resolvePreparation(false)
    await composerSend
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('retries an ambiguous queued steer with the same request identity', async () => {
    let compactInFlight = false
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('response lost'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({
          accepted: true,
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-steer',
          disposition: 'steering',
        }),
    }
    const inputText = ref('unrelated draft')
    const { api, options, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      inputText,
      isCompactInFlightForCurrentSession: () => compactInFlight,
    })
    stream.isStreaming.value = true
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-ambiguous-steer',
      text: 'retry this queued steer',
      attachments: [],
      intent: null,
    }

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('retryable_failure')
    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toMatchObject({
      expected_turn_id: 'turn-current',
      client_request_id: expect.any(String),
      client_message_id: expect.any(String),
    })
    expect(firstParams).not.toHaveProperty('queueMode')
    expect(inputText.value).toBe('unrelated draft')

    compactInFlight = true
    await expect(api.sendQueuedSteer(queued)).resolves.toBe('retryable_failure')
    expect(rpc.call).toHaveBeenCalledOnce()

    // The active run may have ended while the response was lost. The retry
    // still carries the original steer semantics and idempotency fingerprint.
    compactInFlight = false
    stream.isStreaming.value = false
    await expect(api.sendQueuedSteer(queued)).resolves.toBe('accepted')

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
    expect(inputText.value).toBe('unrelated draft')
  })

  it('does not send a queued steer when the gateway exposes no same-turn capability', async () => {
    const { api, options, rpc, stream } = makeOptions()
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-no-capability',
      text: 'send after the prior turn',
      attachments: [],
      intent: null,
    }

    stream.isStreaming.value = false
    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')

    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
  })

  it('does not consume a queued item that still contains an unsendable attachment', async () => {
    const failed: Attachment = {
      kind: 'failed',
      local_id: 31,
      name: 'failed.pdf',
      mime: 'application/pdf',
      error: 'upload failed',
    }
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-failed-attachment',
      text: 'keep the failed attachment recoverable',
      attachments: [failed],
      intent: null,
    }
    const { api, rpc } = makeOptions()

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')

    expect(rpc.call).not.toHaveBeenCalled()
    expect(queued.attachments).toEqual([failed])
  })

  it('keeps a queued image intact while Ensemble routing cannot send it', async () => {
    const image: Attachment = {
      kind: 'staged',
      local_id: 32,
      name: 'queued.png',
      mime: 'image/png',
      file_uuid: 'queued-image',
    }
    const inputText = ref('unrelated live draft')
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-ensemble-image',
      text: 'inspect the queued image',
      attachments: [image],
      intent: null,
    }
    const { api, rpc } = makeOptions({
      inputText,
      modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
    })

    await expect(api.sendQueuedSteer(queued)).resolves.toBe('not_sent')
    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('not_sent')

    expect(rpc.call).not.toHaveBeenCalled()
    expect(queued.attachments).toEqual([image])
    expect(inputText.value).toBe('unrelated live draft')
  })

  it('defers an automatic queued image while routing settings are changing', async () => {
    const image: Attachment = {
      kind: 'staged',
      local_id: 33,
      name: 'queued.webp',
      mime: 'image/webp',
      file_uuid: 'queued-image-busy',
    }
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-routing-update',
      text: 'wait for the routing update',
      attachments: [image],
      intent: null,
    }
    const { api, rpc } = makeOptions({
      modelRoutingMode: ref<'off'>('off'),
      modelRoutingSettingsBusy: ref(true),
    })

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('deferred')

    expect(rpc.call).not.toHaveBeenCalled()
    expect(queued.attachments).toEqual([image])
  })

  it('keeps a recovered fork gated while its canonical child response is pending', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let resolveRetry!: (value: unknown) => void
    let sendCount = 0
    const rpcCall = vi.fn(<T = unknown>(method: string, _params?: Record<string, unknown>) => {
      if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
      sendCount += 1
      if (sendCount === 1) {
        return Promise.reject(Object.assign(new Error('database busy'), {
          accepted: false,
          retryable: true,
        })) as Promise<T>
      }
      if (sendCount === 2) {
        return new Promise<T>((resolve) => {
          resolveRetry = resolve as (value: unknown) => void
        })
      }
      return Promise.resolve({ sessionKey: parentSessionKey }) as Promise<T>
    })
    const rpc = { call: rpcCall as UseChatSendOptions['rpc']['call'] }
    const enqueuePendingInput = vi.fn(() => true)
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      busySendMode: ref<BusySendMode>('steer'),
      enqueuePendingInput,
      adoptResponseSession,
    })

    await harness.api.onSend()
    harness.stream.isStreaming.value = true
    const retry = harness.api.onSend()
    await vi.waitFor(() => expect(sendCount).toBe(2))
    const ownerRequestId = String(rpcCall.mock.calls[1]?.[1]?.clientRequestId)

    inputText.value = 'follow the recovered edit'
    await harness.api.onSend()

    expect(sendCount).toBe(2)
    expect(enqueuePendingInput).toHaveBeenCalledWith('follow the recovered edit', {
      ownerRequestId,
    })

    resolveRetry({ sessionKey: childSessionKey, task_id: 'task-child' })
    await retry
    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, ownerRequestId)
  })

  it('aborts a recovered fork child that resolves after Stop during an ambient run', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let resolveRetry!: (value: unknown) => void
    let sendCount = 0
    let childAbortCalls = 0
    const rpcCall = vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
      if (method === 'chat.abort') {
        if (params?.sessionKey === childSessionKey) {
          childAbortCalls += 1
          if (childAbortCalls === 1) {
            return Promise.reject(new Error('socket closed')) as Promise<T>
          }
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }
      sendCount += 1
      if (sendCount === 1) {
        return Promise.reject(Object.assign(new Error('response lost'), {
          accepted: false,
          retryable: true,
        })) as Promise<T>
      }
      return new Promise<T>((resolve) => {
        resolveRetry = resolve as (value: unknown) => void
      })
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const acceptanceStopPending = ref(false)
    const harness = makeOptions({
      rpc: { call: rpcCall as UseChatSendOptions['rpc']['call'] },
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      acceptanceStopPending,
      reconcileTaskOwnership: vi.fn(async () => {}),
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    await harness.api.onSend()
    harness.stream.isStreaming.value = true
    harness.options.activeStreamSessionKey.value = parentSessionKey
    const retry = harness.api.onSend()
    await vi.waitFor(() => expect(sendCount).toBe(2))

    // The ambient parent run can finish while the idempotent fork retry is
    // still waiting for its canonical child response.
    harness.stream.isStreaming.value = false
    harness.api.onStop()
    resolveRetry({ sessionKey: childSessionKey, task_id: 'task-child' })
    await retry

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(rpcCall).toHaveBeenCalledWith('chat.abort', {
      sessionKey: childSessionKey,
      taskId: 'task-child',
      source: 'webui_stop',
      scope: 'task',
    })
    expect(harness.options.aborted.value).toBe(false)
    expect(harness.options.activeStreamTaskId.value).toBe('task-child')
    expect(harness.options.activeStreamSessionKey.value).toBe(childSessionKey)
    expect(harness.stream.isStreaming.value).toBe(false)
    await vi.waitFor(() => expect(childAbortCalls).toBe(2))
    expect(acceptanceStopPending.value).toBe(false)
    expect(harness.options.messages.value).not.toContainEqual(expect.objectContaining({
      role: 'system',
      text: 'Stop could not reach the server — the run may still be finishing.',
    }))
  })

  it('uses a new id when the user changes a recovered attempt before resending', async () => {
    const inputText = ref('hello')
    const elevatedMode = ref('enabled')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('database busy'), { accepted: false }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test', task_id: 'task-new' }),
    }
    const { api } = makeOptions({ rpc, inputText, elevatedMode })

    await api.onSend()
    inputText.value = 'edited'
    elevatedMode.value = ''
    await api.onSend()

    const firstParams = rpc.call.mock.calls[0]?.[1]
    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(secondParams.clientRequestId).not.toBe(firstParams.clientRequestId)
    expect(secondParams).toMatchObject({ message: 'edited', _source: { runMode: 'safe' } })
  })

  it('uses a new request when the recovered draft collaboration mode changes', async () => {
    const inputText = ref('inspect and plan')
    const pendingSessionIntent = ref<string | null>('new_chat')
    const initialCollaborationMode = ref<CollaborationMode>('plan')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('database busy'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-default',
        }),
    }
    const { api } = makeOptions({
      rpc,
      inputText,
      pendingSessionIntent,
      initialCollaborationMode,
    })

    await api.onSend()
    initialCollaborationMode.value = 'default'
    await api.onSend()

    const firstParams = rpc.call.mock.calls[0]?.[1]
    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(firstParams).toMatchObject({
      collaborationMode: 'plan',
      intent: 'new_chat',
    })
    expect(secondParams.clientRequestId).not.toBe(firstParams.clientRequestId)
    expect(secondParams).not.toHaveProperty('collaborationMode')
  })

  it('replays an unknown-acceptance draft with its original mode and request id', async () => {
    const inputText = ref('inspect and plan')
    const pendingSessionIntent = ref<string | null>('new_chat')
    const initialCollaborationMode = ref<CollaborationMode>('plan')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(new Error('response lost'))
        .mockResolvedValueOnce({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-plan',
        }),
    }
    const { api } = makeOptions({
      rpc,
      inputText,
      pendingSessionIntent,
      initialCollaborationMode,
    })

    await api.onSend()
    initialCollaborationMode.value = 'default'
    await api.onSend()

    const firstParams = rpc.call.mock.calls[0]?.[1]
    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(secondParams.clientRequestId).toBe(firstParams.clientRequestId)
    expect(secondParams).toEqual(firstParams)
    expect(secondParams).toMatchObject({
      collaborationMode: 'plan',
      intent: 'new_chat',
    })
  })

  it('resolves unknown acceptance before sending an edited draft', async () => {
    const inputText = ref('inspect and plan')
    const pendingSessionIntent = ref<string | null>('new_chat')
    const initialCollaborationMode = ref<CollaborationMode>('plan')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(new Error('response lost'))
        .mockResolvedValueOnce({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-plan',
        }),
    }
    const { api } = makeOptions({
      rpc,
      inputText,
      pendingSessionIntent,
      initialCollaborationMode,
    })

    await api.onSend()
    inputText.value = 'a different follow-up'
    initialCollaborationMode.value = 'default'
    await api.onSend()

    const firstParams = rpc.call.mock.calls[0]?.[1]
    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(secondParams).toEqual(firstParams)
    expect(inputText.value).toBe('a different follow-up')
  })

  it('does not restore an attempt explicitly reported as accepted', async () => {
    const inputText = ref('hello')
    const pendingSessionIntent = ref<string | null>('NEW')
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('response lost'), {
        accepted: true,
        retryable: false,
      })),
    }
    const { api } = makeOptions({
      rpc,
      inputText,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
    })

    await api.onSend()

    expect(inputText.value).toBe('')
    expect(pendingSessionIntent.value).toBeNull()
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('ends a fresh stream when an idempotent replay is already terminal', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-old',
        replayed: true,
        task_status: 'succeeded',
      }),
    }
    const { api, options, stream } = makeOptions({ rpc })

    await api.onSend()

    expect(stream.startStreaming).toHaveBeenCalledTimes(1)
    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
    expect(options.activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
    expect(options.activeStreamSessionKey.value).toBe('')
  })

  it('surfaces the backend terminal message when a failed replay is already terminal', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-old',
        replayed: true,
        taskStatus: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'Activation failed; retry this message.',
      }),
    }
    const { api, options, stream } = makeOptions({ rpc })

    await api.onSend()

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'Activation failed; retry this message.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
  })

  it('ends the fresh stream when first acceptance reports activation failure', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-failed-before-activation',
        replayed: false,
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The accepted task could not be activated.',
      }),
    }
    const { api, options, stream } = makeOptions({ rpc })

    await api.onSend()

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
    expect(options.activeStreamSessionKey.value).toBe('')
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The accepted task could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
  })

  it('keeps a child-session activation failure after the session handoff', async () => {
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref('agent:main:webchat:parent')
    const messages = ref<ChatMessage[]>([])
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      messages.value = []
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-child-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The edited question could not be activated.',
      }),
    }
    const { api, options } = makeOptions({
      rpc,
      sessionKey,
      messages,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })

    await api.onSend()

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The edited question could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
  })

  it('keeps a hidden child-session terminal failure after the session handoff', async () => {
    const childSessionKey = 'agent:main:webchat:hidden-child'
    const sessionKey = ref('agent:main:webchat:parent')
    const messages = ref<ChatMessage[]>([])
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      messages.value = []
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-hidden-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The confirmation could not be activated.',
      }),
    }
    const { api, options } = makeOptions({ rpc, sessionKey, messages, adoptResponseSession })

    await api.dispatchHiddenSend('provider confirmation', 'Confirmed')

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The confirmation could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
  })

  it('does not leak a child terminal failure after navigation during the handoff', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const otherSessionKey = 'agent:main:webchat:other'
    const sessionKey = ref(parentSessionKey)
    let finishHandoff!: () => void
    const handoffGate = new Promise<void>((resolve) => {
      finishHandoff = resolve
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      await handoffGate
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-child-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'This failure belongs to the child session.',
      }),
    }
    const { api, options } = makeOptions({ rpc, sessionKey, adoptResponseSession })

    const send = api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledWith(
      childSessionKey,
      expect.any(String),
    ))
    sessionKey.value = otherSessionKey
    finishHandoff()
    await send

    expect(options.messages.value.some(message => message.terminalNotice)).toBe(false)
  })

  it('queues instead of steering a new child input while response handoff hydrates', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let finishHandoff!: () => void
    const handoffGate = new Promise<void>((resolve) => {
      finishHandoff = resolve
    })
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      // The real session runtime resets the parent live-turn state before
      // child subscription/history hydration has completed.
      stream.isStreaming.value = false
      await handoffGate
    })
    let sendCount = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
        sendCount += 1
        if (sendCount === 1) {
          return Promise.resolve({
            sessionKey: childSessionKey,
            task_id: 'task-child-old',
            task_status: 'failed',
            terminal_reason: 'activation_failed',
            terminal_message: 'The edited question could not be activated.',
          }) as Promise<T>
        }
        return Promise.resolve({ sessionKey: childSessionKey }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      adoptResponseSession,
      enqueuePendingInput,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })
    stream.endStreaming = vi.fn(() => {
      stream.isStreaming.value = false
    })

    const oldSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledWith(
      childSessionKey,
      expect.any(String),
    ))

    inputText.value = 'new child question'
    await harness.api.onSend()
    expect(sendCount).toBe(1)
    expect(enqueuePendingInput).toHaveBeenCalledWith('new child question', {
      ownerRequestId: expect.any(String),
    })

    finishHandoff()
    await oldSend

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(stream.isStreaming.value).toBe(false)
    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledTimes(1)
    expect(harness.options.flushDeferredPendingDrain).toHaveBeenCalledOnce()
  })

  it('schedules an adopted follow-up when terminal replay finishes before hydration', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    const activeStreamTaskId = ref('')
    let finishHydration!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHydration = resolve
    })
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      stream.isStreaming.value = false
      // A terminal replay can arrive while the handoff reset has streaming
      // false; the event handler records the FINISHED sentinel and returns.
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      await hydration
      return { authoritativeIdle: true }
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-child-not-replayed',
      }),
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      activeStreamTaskId,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())
    inputText.value = 'follow-up for idle child'
    await harness.api.onSend()
    expect(enqueuePendingInput).toHaveBeenCalledOnce()

    finishHydration()
    await forkSend

    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    expect(harness.options.flushDeferredPendingDrain).toHaveBeenCalledOnce()
  })

  it('waits for a terminal event before draining a legacy child without a task id', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    const activeStreamTaskId = ref('')
    let finishHydration!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHydration = resolve
    })
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      stream.isStreaming.value = false
      activeStreamTaskId.value = ''
      await hydration
      return { authoritativeIdle: true }
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({ sessionKey: childSessionKey }),
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      activeStreamTaskId,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())
    inputText.value = 'follow-up for legacy child'
    await harness.api.onSend()
    expect(enqueuePendingInput).toHaveBeenCalledOnce()

    finishHydration()
    await forkSend

    expect(harness.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(harness.options.flushDeferredPendingDrain).toHaveBeenCalledOnce()
    expect(stream.startStreaming).toHaveBeenCalledTimes(2)
    expect(stream.isStreaming.value).toBe(true)
  })

  it('drains a legacy child when terminal replay is authoritatively complete', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const activeStreamTaskId = ref('')
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      stream.isStreaming.value = false
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      return { authoritativeIdle: true }
    })
    const harness = makeOptions({
      rpc: { call: vi.fn().mockResolvedValue({ sessionKey: childSessionKey }) },
      sessionKey,
      activeStreamTaskId,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })

    await harness.api.onSend()

    expect(stream.startStreaming).toHaveBeenCalledOnce()
    expect(stream.isStreaming.value).toBe(false)
    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
  })

  it('does not treat a failed child subscription as authoritative idle', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let finishHydration!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHydration = resolve
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      await hydration
      return { authoritativeIdle: false }
    })
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc: {
        call: vi.fn().mockResolvedValue({
          sessionKey: childSessionKey,
          task_id: 'task-child-unknown',
        }),
      },
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())
    inputText.value = 'follow-up while subscription is unavailable'
    await harness.api.onSend()
    finishHydration()
    await forkSend

    expect(enqueuePendingInput).toHaveBeenCalledOnce()
    expect(harness.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
  })

  it('queues steer input while a fork send is awaiting its canonical session', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let resolveFork!: (value: unknown) => void
    let sendCount = 0
    const rpcCall = vi.fn(<T = unknown>(method: string, _params?: Record<string, unknown>) => {
      if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
      sendCount += 1
      if (sendCount === 1) {
        return new Promise<T>((resolve) => {
          resolveFork = resolve as (value: unknown) => void
        })
      }
      return Promise.resolve({ sessionKey: parentSessionKey }) as Promise<T>
    })
    const rpc = {
      call: rpcCall as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => true)
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      busySendMode: ref<BusySendMode>('steer'),
      enqueuePendingInput,
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(sendCount).toBe(1))
    const ownerRequestId = String(rpcCall.mock.calls[0]?.[1]?.clientRequestId)

    inputText.value = 'follow the edited question'
    await harness.api.onSend()

    expect(sendCount).toBe(1)
    expect(enqueuePendingInput).toHaveBeenCalledWith('follow the edited question', {
      ownerRequestId,
    })

    resolveFork({ sessionKey: childSessionKey, task_id: 'task-child' })
    await forkSend
  })

  it('does not drain a follow-up over a restored fork draft after rejection', async () => {
    const inputText = ref('edited question')
    let rejectFork!: (reason: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>((_resolve, reject) => {
        rejectFork = reject
      })) as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => {
      inputText.value = ''
      return true
    })
    const harness = makeOptions({
      rpc,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      enqueuePendingInput,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    inputText.value = 'queued follow-up'
    await harness.api.onSend()

    rejectFork(Object.assign(new Error('database busy'), { accepted: false }))
    await forkSend

    expect(inputText.value).toBe('edited question')
    expect(enqueuePendingInput).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(harness.options.flushDeferredPendingDrain).not.toHaveBeenCalled()
    expect(harness.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
  })

  it('does not let an old handoff gate queue input in a newly selected session', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const otherSessionKey = 'agent:main:webchat:other'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let finishHandoff!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHandoff = resolve
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      await hydration
    })
    let sendCount = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
        sendCount += 1
        if (sendCount === 1) {
          return Promise.resolve({ sessionKey: childSessionKey, task_id: 'task-child' }) as Promise<T>
        }
        return Promise.resolve({ sessionKey: params?.sessionKey, task_id: 'task-other' }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())

    sessionKey.value = otherSessionKey
    harness.stream.isStreaming.value = false
    inputText.value = 'question for other session'
    await harness.api.onSend()

    expect(sendCount).toBe(2)
    expect(rpc.call).toHaveBeenLastCalledWith('chat.send', expect.objectContaining({
      sessionKey: otherSessionKey,
      message: 'question for other session',
    }))
    expect(enqueuePendingInput).not.toHaveBeenCalled()

    finishHandoff()
    await forkSend
    expect(harness.options.flushDeferredPendingDrain).not.toHaveBeenCalled()
  })

  it('adopts and aborts a fork child when its response arrives after Stop', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    let resolveFork!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveFork = resolve as (value: unknown) => void
          })
        }
        if (params?.sessionKey === childSessionKey) {
          return Promise.reject(new Error('socket closed')) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))
    const optimisticClientId = harness.options.messages.value[0]?.clientId
    harness.api.onStop()

    resolveFork({
      sessionKey: childSessionKey,
      task_id: 'task-child',
      user_message_id: 'message-child',
    })
    await forkSend

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: childSessionKey,
      taskId: 'task-child',
      source: 'webui_stop',
      scope: 'task',
    })
    expect(harness.options.messages.value.find(
      message => message.clientId === optimisticClientId,
    )?.messageId).toBeUndefined()
    expect(harness.options.aborted.value).toBe(false)
    expect(harness.options.activeStreamTaskId.value).toBe('task-child')
    expect(harness.options.activeStreamSessionKey.value).toBe(childSessionKey)
    expect(harness.stream.isStreaming.value).toBe(true)
  })

  it('still aborts a stopped fork response after navigation to another session', async () => {
    pushToast.mockClear()
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const otherSessionKey = 'agent:main:webchat:other'
    const sessionKey = ref(parentSessionKey)
    let resolveFork!: (value: unknown) => void
    let childAbortCalls = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveFork = resolve as (value: unknown) => void
          })
        }
        if (params?.sessionKey === childSessionKey) {
          childAbortCalls += 1
          if (childAbortCalls === 1) {
            return Promise.reject(new Error('socket closed')) as Promise<T>
          }
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const adoptResponseSession = vi.fn()
    const acceptanceStopPending = ref(false)
    const harness = makeOptions({
      rpc,
      sessionKey,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      acceptanceStopPending,
      reconcileTaskOwnership: vi.fn(async () => {}),
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))
    harness.api.onStop()
    sessionKey.value = otherSessionKey
    harness.options.activeStreamTaskId.value = ''

    resolveFork({ sessionKey: childSessionKey, task_id: 'task-child-late' })
    await forkSend

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: childSessionKey,
      taskId: 'task-child-late',
      source: 'webui_stop',
      scope: 'task',
    })
    await vi.waitFor(() => expect(childAbortCalls).toBe(2))
    expect(adoptResponseSession).not.toHaveBeenCalled()
    expect(sessionKey.value).toBe(otherSessionKey)
    await Promise.resolve()
    expect(harness.options.messages.value).not.toContainEqual(expect.objectContaining({
      role: 'system',
      text: 'Stop could not reach the server — the run may still be finishing.',
    }))
    expect(pushToast).not.toHaveBeenCalledWith(
      'Stop could not reach the server — the run may still be finishing.',
      expect.anything(),
    )
    expect(acceptanceStopPending.value).toBe(false)
  })

  it('binds an orphan message id and reconciles history for an accepted queue error', async () => {
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('queue bookkeeping failed'), {
        accepted: true,
        retryable: false,
        details: { orphan_message_id: 'message-orphan' },
      })),
    }
    const harness = makeOptions({ rpc })

    await harness.api.onSend()

    expect(harness.options.messages.value[0]).toMatchObject({
      role: 'user',
      messageId: 'message-orphan',
    })
    expect(harness.options.scheduleHistorySync).toHaveBeenCalledOnce()
    expect(harness.options.inputText.value).toBe('')
  })

  it('adopts the child without binding its orphan id onto the parent after a dirty fork error', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const messages = ref<ChatMessage[]>([])
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('queue bookkeeping failed'), {
        code: 'QUEUE_FULL_DIRTY',
        accepted: true,
        retryable: false,
        details: {
          session_key: childSessionKey,
          orphan_message_id: 'message-child-orphan',
        },
      })),
    }
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      messages,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })

    await harness.api.onSend()

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(messages.value.find(message => message.role === 'user')?.messageId).toBeUndefined()
    expect(harness.options.scheduleHistorySync).toHaveBeenCalledOnce()
    expect(harness.stream.startStreaming).toHaveBeenCalledOnce()
    expect(harness.stream.endStreaming).toHaveBeenCalledOnce()
    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    expect(sessionKey.value).toBe(childSessionKey)
  })

  it('does not abort unrelated child work for a stopped dirty fork rejection', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    let rejectSend!: (reason: unknown) => void
    const rpcCall = vi.fn(<T = unknown>(method: string) => {
      if (method === 'chat.abort') return Promise.resolve({ aborted: true }) as Promise<T>
      return new Promise<T>((_resolve, reject) => {
        rejectSend = reject
      })
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc: { call: rpcCall as UseChatSendOptions['rpc']['call'] },
      sessionKey,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))
    harness.api.onStop()
    rejectSend(Object.assign(new Error('queue bookkeeping failed'), {
      code: 'QUEUE_FULL_DIRTY',
      accepted: true,
      retryable: false,
      details: {
        session_key: childSessionKey,
        orphan_message_id: 'message-child-orphan',
      },
    }))
    await send

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(rpcCall).not.toHaveBeenCalledWith('chat.abort', expect.objectContaining({
      sessionKey: childSessionKey,
    }))
  })

  it('restores a rejected v2 steer without ending the existing stream', async () => {
    const activeStreamTaskId = ref('task-current')
    const activeStreamSessionKey = ref('agent:main:webchat:test')
    const restoreSteerIntoComposer = vi.fn()
    const rpc = {
      call: vi.fn().mockResolvedValue({
        accepted: false,
        turn_id: 'task-current',
        disposition: 'rejected',
        failure_code: 'activation_failed',
        retryable: false,
        fallback_safe: false,
        recovery: 'inspect_transcript_and_resend',
      }),
    }
    const { api, options, stream } = makeOptions({
      ...sameTurnSteerOptions('task-current'),
      rpc,
      activeStreamTaskId,
      activeStreamSessionKey,
      busySendMode: ref<BusySendMode>('steer'),
      restoreSteerIntoComposer,
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(activeStreamTaskId.value).toBe('task-current')
    expect(activeStreamSessionKey.value).toBe('agent:main:webchat:test')
    expect(options.messages.value).toEqual([])
    expect(restoreSteerIntoComposer).toHaveBeenCalledWith('hello')
    expect(options.scheduleHistorySync).not.toHaveBeenCalled()
  })

  it('does not materialize a stale steer terminal response in the newly selected session', async () => {
    let resolveSend!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>((resolve) => {
        resolveSend = resolve as (value: unknown) => void
      })) as UseChatSendOptions['rpc']['call'],
    }
    const sessionKey = ref('agent:main:webchat:first')
    const { api, options, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      rpc,
      sessionKey,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    const send = api.onSend()
    sessionKey.value = 'agent:main:webchat:second'
    resolveSend({
      accepted: true,
      turn_id: 'turn-current',
      disposition: 'steering',
    })
    await send

    expect(options.messages.value.some(message => message.inputDisposition === 'rejected')).toBe(false)
    expect(options.scheduleHistorySync).not.toHaveBeenCalled()
    expect(stream.endStreaming).not.toHaveBeenCalled()
  })

  it('uses terminal_reason when a terminal replay has no terminal message', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-old',
        replayed: true,
        task_status: 'timeout',
        terminal_reason: 'Provider did not respond; retry is safe.',
      }),
    }
    const { api, options } = makeOptions({ rpc })

    await api.onSend()

    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'Provider did not respond; retry is safe.',
      errorCode: 'timeout',
    })
  })

  it('invalidates the previous task id before a fresh send is accepted', async () => {
    let resolveSend!: (value: unknown) => void
    const call: UseChatSendOptions['rpc']['call'] = <T = unknown>() => new Promise<T>((resolve) => {
      resolveSend = resolve as (value: unknown) => void
    })
    const rpc = {
      call: vi.fn(call) as UseChatSendOptions['rpc']['call'],
    }
    const activeStreamTaskId = ref('task-old')
    const activeStreamSessionKey = ref('')
    const { api } = makeOptions({ rpc, activeStreamTaskId, activeStreamSessionKey })

    const send = api.onSend()

    expect(activeStreamTaskId.value).not.toBe('task-old')
    expect(activeStreamTaskId.value).toBeTruthy()
    expect(activeStreamSessionKey.value).toBe('agent:main:webchat:test')

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-new',
    })
    await send

    expect(activeStreamTaskId.value).toBe('task-new')
  })

  it('binds the accepted task through the event handler boundary', async () => {
    const bindActiveStreamTask = vi.fn()
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-new',
      }),
    }
    const { api } = makeOptions({ rpc, bindActiveStreamTask })

    await api.onSend()

    expect(bindActiveStreamTask).toHaveBeenCalledWith('task-new')
  })

  it('waits for an empty-output Stop terminal instead of synthesizing one locally', async () => {
    let resolveSend!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.abort') {
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return new Promise<T>((resolve) => {
          resolveSend = resolve as (value: unknown) => void
        })
      }) as UseChatSendOptions['rpc']['call'],
    }
    const { api, options, stream } = makeOptions({ rpc })
    stream.startStreaming = vi.fn(() => { stream.isStreaming.value = true })
    stream.endStreaming = vi.fn(() => { stream.isStreaming.value = false })

    const send = api.onSend()
    const user = options.messages.value[0]
    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      source: 'webui_stop',
      scope: 'task',
    })

    expect(user).toMatchObject({ role: 'user' })
    expect(user?.turnOutcome).toBeUndefined()
    expect(options.messages.value).toHaveLength(1)
    expect(options.messages.value.some(message => message.stopNotice)).toBe(false)
    expect(stream.endStreaming).not.toHaveBeenCalled()

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'turn-stopped',
      user_message_id: 'user-stopped',
    })
    await send

    expect(options.messages.value[0]).toMatchObject({
      messageId: 'user-stopped',
      turnId: 'turn-stopped',
    })
    expect(options.messages.value[0]?.turnOutcome).toBeUndefined()
    expect(options.activeStreamTaskId.value).toBe('turn-stopped')
    expect(stream.endStreaming).not.toHaveBeenCalled()
  })

  it('binds a Stop-before-ACK to the accepted task and retries one exact abort', async () => {
    let resolveSend!: (value: unknown) => void
    const abortCalls: Record<string, unknown>[] = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveSend = resolve as (value: unknown) => void
          })
        }
        if (method === 'chat.abort') {
          abortCalls.push(params || {})
          return Promise.resolve({ aborted: Boolean(params?.taskId) }) as Promise<T>
        }
        return Promise.resolve({}) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const taskOwnership = useChatTaskOwnership()
    const bindActiveStreamTask = vi.fn()
    const reconcileTaskOwnership = vi.fn()
    const harness = makeOptions({
      rpc,
      taskOwnership,
      bindActiveStreamTask,
      reconcileTaskOwnership,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.any(Object),
    ))
    harness.api.onStop()
    await vi.waitFor(() => expect(abortCalls).toHaveLength(1))

    expect(abortCalls[0]).toEqual({
      sessionKey: 'agent:main:webchat:test',
      source: 'webui_stop',
      scope: 'task',
    })
    expect(harness.stream.endStreaming).not.toHaveBeenCalled()
    expect(harness.options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    expect(reconcileTaskOwnership).not.toHaveBeenCalled()

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-accepted-after-stop',
      task_status: 'queued',
      user_message_id: 'message-accepted-after-stop',
    })
    await send
    await vi.waitFor(() => expect(abortCalls).toHaveLength(2))

    expect(abortCalls[1]).toEqual({
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-accepted-after-stop',
      source: 'webui_stop',
      scope: 'task',
    })
    expect(taskOwnership.stopRequestedTaskId.value).toBe('task-accepted-after-stop')
    expect(bindActiveStreamTask).toHaveBeenCalledWith('task-accepted-after-stop')
    expect(harness.stream.endStreaming).not.toHaveBeenCalled()
    expect(harness.options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    expect(harness.options.messages.value.every(message => !message.turnOutcome)).toBe(true)
  })

  it('never widens repeated Stop clicks before ACK into an unscoped abort', async () => {
    let resolveSend!: (value: unknown) => void
    const abortCalls: Record<string, unknown>[] = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveSend = resolve as (value: unknown) => void
          })
        }
        if (method === 'chat.abort') {
          abortCalls.push(params || {})
          return Promise.resolve({ aborted: Boolean(params?.taskId) }) as Promise<T>
        }
        return Promise.resolve({}) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const taskOwnership = useChatTaskOwnership()
    const harness = makeOptions({ rpc, taskOwnership })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.any(Object),
    ))

    harness.api.onStop()
    harness.api.onStop()
    await vi.waitFor(() => expect(abortCalls.length).toBeGreaterThan(0))

    expect(abortCalls.every(call => call.scope === 'task')).toBe(true)
    expect(abortCalls.filter(call => !call.taskId)).toHaveLength(1)

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-after-double-stop',
      task_status: 'queued',
    })
    await send
    await vi.waitFor(() => expect(abortCalls.some(
      call => call.taskId === 'task-after-double-stop',
    )).toBe(true))

    expect(abortCalls.every(call => call.scope === 'task')).toBe(true)
  })

  it('stops hydrated running A without aborting pending-acceptance B after its ACK', async () => {
    let resolveSend!: (value: unknown) => void
    const abortCalls: Record<string, unknown>[] = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveSend = resolve as (value: unknown) => void
          })
        }
        if (method === 'chat.abort') {
          abortCalls.push(params || {})
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return Promise.resolve({}) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const taskOwnership = useChatTaskOwnership()
    const activeStreamTaskId = ref('')
    const harness = makeOptions({
      rpc,
      taskOwnership,
      activeStreamTaskId,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    // B crossed the network boundary while this tab still believed the
    // session idle. Before B's ACK arrives, authoritative hydration recovers A
    // as the durable running task.
    const sendB = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.any(Object),
    ))
    taskOwnership.applySnapshot({
      run_status: 'running',
      active_task: { task_id: 'task-A', status: 'running' },
    }, true)
    activeStreamTaskId.value = 'task-A'

    harness.api.onStop()
    await vi.waitFor(() => expect(abortCalls).toHaveLength(1))

    expect(abortCalls[0]).toEqual({
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-A',
      source: 'webui_stop',
      scope: 'task',
    })

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-B',
      task_status: 'queued',
      user_message_id: 'message-B',
    })
    await sendB
    await Promise.resolve()

    expect(abortCalls).toHaveLength(1)
    expect(taskOwnership.stopRequestedTaskId.value).toBe('task-A')
    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect([...taskOwnership.queuedTaskIds.value]).toEqual(['task-B'])
  })

  it.each(['regular', 'hidden'] as const)(
    'rebinds a pending %s B send to running A and replays A output before B queued ACK',
    async (kind) => {
      let resolveSend!: (value: unknown) => void
      const abortCalls: Record<string, unknown>[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.send') {
            return new Promise<T>((resolve) => {
              resolveSend = resolve as (value: unknown) => void
            })
          }
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            return Promise.resolve({ aborted: true }) as Promise<T>
          }
          return Promise.resolve({}) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const taskOwnership = useChatTaskOwnership()
      const activeStreamTaskId = ref('')
      const harness = makeOptions({ rpc, taskOwnership, activeStreamTaskId })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })
      const scope = effectScope()
      const rpcEvents = scope.run(() => useChatRpcEventHandlers({
        sessionKey: harness.options.sessionKey,
        currentEpoch: ref(0),
        lastStreamSeq: ref(0),
        activeTaskGroups: ref(new Set<string>()),
        taskOwnership,
        activeStreamTaskId,
        aborted: harness.options.aborted,
        messages: harness.options.messages,
        pendingQueue: harness.pendingQueue,
        usageAccum: ref({
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          cost: null,
          routedTurns: 0,
          sessionSaved: 0,
        }),
        usageModel: ref(''),
        stream: harness.stream,
        normalizeRunStatus: status => status,
        sessionRunStatus: () => ({ status: 'running', label: 'running', task: null }),
        applySessionRunState: vi.fn(),
        queueRouterDecision: vi.fn(),
        appendEnsembleProgress: vi.fn(),
        markEnsembleHandoff: vi.fn(),
        flushPendingRouterDecision: vi.fn(),
        clearPendingRouterDecision: vi.fn(),
        handleRouterControlReplay: vi.fn(),
        showCompactionToast: vi.fn(),
        showWarningToast: vi.fn(),
        scheduleHistorySync: vi.fn(),
        schedulePendingDrainAfterTerminal: vi.fn(),
        popAllPendingIntoComposer: vi.fn(() => false),
        saveWidgetState: vi.fn(),
        loadCurrentSessionUsage: vi.fn(),
      }))!
      harness.options.bindActiveStreamTask = rpcEvents.bindActiveStreamTask

      const send = kind === 'regular'
        ? harness.api.onSend()
        : harness.api.dispatchHiddenSend(
            'synthetic hidden control',
            'visible confirmation',
            'hidden-ack-race',
          )
      await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
        'chat.send',
        expect.any(Object),
      ))
      expect(activeStreamTaskId.value).toBe(PENDING_STREAM_TASK_ID)

      rpcEvents.handlers.onTaskRunning({
        task_id: 'task-A',
        session_key: 'agent:main:webchat:test',
      })
      rpcEvents.handlers.onTextDelta({
        task_id: 'task-A',
        session_key: 'agent:main:webchat:test',
        stream_seq: 1,
        text: 'A token before B ACK',
      })
      expect(activeStreamTaskId.value).toBe(PENDING_STREAM_TASK_ID)
      expect(harness.stream.appendDelta).not.toHaveBeenCalled()

      resolveSend({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-B',
        task_status: 'queued',
        user_message_id: 'message-B',
      })
      await send

      expect(activeStreamTaskId.value).toBe('task-A')
      expect(harness.stream.appendDelta).toHaveBeenCalledWith('A token before B ACK')
      expect(taskOwnership.runningTaskId.value).toBe('task-A')
      expect([...taskOwnership.queuedTaskIds.value]).toEqual(['task-B'])

      harness.api.onStop()
      await vi.waitFor(() => expect(abortCalls).toHaveLength(1))
      expect(abortCalls[0]).toEqual({
        sessionKey: 'agent:main:webchat:test',
        taskId: 'task-A',
        source: 'webui_stop',
        scope: 'task',
      })
      scope.stop()
    },
  )

  it.each(['network_error', 'unknown_result'] as const)(
    'keeps retrying an exact hidden-control Stop after its first %s',
    async (firstFailure) => {
      let resolveSend!: (value: unknown) => void
      const abortCalls: Record<string, unknown>[] = []
      let exactAbortAttempts = 0
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.send') {
            return new Promise<T>((resolve) => {
              resolveSend = resolve as (value: unknown) => void
            })
          }
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            if (!params?.taskId) {
              return Promise.resolve({ aborted: false, reason: 'task_id_required' }) as Promise<T>
            }
            exactAbortAttempts += 1
            if (exactAbortAttempts === 1) {
              if (firstFailure === 'network_error') {
                return Promise.reject(new Error('response lost')) as Promise<T>
              }
              return Promise.resolve({
                aborted: false,
                reason: 'task_cancel_unknown',
              }) as Promise<T>
            }
            return Promise.resolve({ aborted: true }) as Promise<T>
          }
          return Promise.resolve({}) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const acceptanceStopPending = ref(false)
      const taskOwnership = useChatTaskOwnership()
      const harness = makeOptions({ rpc, acceptanceStopPending, taskOwnership })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const hiddenSend = harness.api.dispatchHiddenSend(
        'synthetic hidden control',
        'visible confirmation',
        `hidden-stop-${firstFailure}`,
      )
      await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
        'chat.send',
        expect.any(Object),
      ))

      harness.api.onStop()
      expect(acceptanceStopPending.value).toBe(true)
      resolveSend({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-hidden-stopped',
        task_status: 'queued',
        user_message_id: 'message-hidden-stopped',
      })
      await hiddenSend

      await vi.waitFor(() => expect(exactAbortAttempts).toBeGreaterThanOrEqual(2), {
        timeout: 2_000,
      })
      const exactCalls = abortCalls.filter(call => call.taskId)
      expect(exactCalls).toEqual([
        {
          sessionKey: 'agent:main:webchat:test',
          taskId: 'task-hidden-stopped',
          source: 'webui_stop',
          scope: 'task',
        },
        {
          sessionKey: 'agent:main:webchat:test',
          taskId: 'task-hidden-stopped',
          source: 'webui_stop',
          scope: 'task',
        },
      ])
    },
  )

  it('replays an unknown stopped hidden acceptance with the identical request and exact-aborts its receipt task', async () => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      const sendParams: Record<string, unknown>[] = []
      const abortCalls: Record<string, unknown>[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            return Promise.resolve({
              aborted: Boolean(params?.taskId),
              ...(!params?.taskId ? { reason: 'task_id_required' } : {}),
            }) as Promise<T>
          }
          sendParams.push({ ...(params || {}) })
          if (sendParams.length === 1) {
            return new Promise<T>((_resolve, reject) => {
              rejectFirstSend = reject
            })
          }
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: 'task-hidden-replayed-receipt',
            task_status: 'queued',
            user_message_id: 'message-hidden-replayed-receipt',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const acceptanceStopPending = ref(false)
      const taskOwnership = useChatTaskOwnership()
      const harness = makeOptions({ rpc, acceptanceStopPending, taskOwnership })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const first = harness.api.dispatchHiddenSend(
        'synthetic hidden control',
        'visible confirmation',
        'hidden-unknown-stop',
      )
      await Promise.resolve()
      expect(sendParams).toHaveLength(1)
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
      await expect(first).resolves.toMatchObject({
        status: 'unknown',
        reason: 'response_unknown',
      })
      expect(acceptanceStopPending.value).toBe(true)

      await vi.runAllTimersAsync()
      await Promise.resolve()

      expect(sendParams).toHaveLength(2)
      expect(sendParams[1]).toEqual(sendParams[0])
      expect(sendParams[1]?.clientRequestId).toBe('hidden-unknown-stop')
      expect(abortCalls).toContainEqual({
        sessionKey: 'agent:main:webchat:test',
        taskId: 'task-hidden-replayed-receipt',
        source: 'webui_stop',
        scope: 'task',
      })
      expect(acceptanceStopPending.value).toBe(false)

      // The exact abort response is only an acknowledgement; the normal task
      // terminal/hydrate boundary releases the accepted queued owner.
      taskOwnership.noteTerminal('task-hidden-replayed-receipt')
      harness.stream.isStreaming.value = false
      harness.options.activeStreamTaskId.value = ''

      await expect(harness.api.dispatchHiddenSend(
        'next synthetic hidden control',
        'next visible confirmation',
        'hidden-after-recovery',
      )).resolves.toMatchObject({ status: 'accepted' })
      expect(sendParams[2]?.clientRequestId).toBe('hidden-after-recovery')
    } finally {
      vi.useRealTimers()
    }
  })

  it('releases the pre-ACK Stop latch when chat.send is durably rejected', async () => {
    let rejectSend!: (reason: unknown) => void
    const acceptanceStopPending = ref(false)
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.abort') {
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return new Promise<T>((_resolve, reject) => {
          rejectSend = reject
        })
      }) as UseChatSendOptions['rpc']['call'],
    }
    const harness = makeOptions({ rpc, acceptanceStopPending })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.any(Object),
    ))
    harness.api.onStop()
    expect(acceptanceStopPending.value).toBe(true)

    rejectSend(Object.assign(new Error('not accepted'), {
      accepted: false,
      retryable: false,
    }))
    await send

    expect(acceptanceStopPending.value).toBe(false)
  })

  it('releases the pre-ACK Stop latch for accepted terminal-without-task failure', async () => {
    let rejectSend!: (reason: unknown) => void
    const acceptanceStopPending = ref(false)
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.abort') {
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return new Promise<T>((_resolve, reject) => {
          rejectSend = reject
        })
      }) as UseChatSendOptions['rpc']['call'],
    }
    const harness = makeOptions({ rpc, acceptanceStopPending })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.any(Object),
    ))
    harness.api.onStop()
    expect(acceptanceStopPending.value).toBe(true)

    rejectSend(Object.assign(new Error('accepted without task'), {
      code: 'QUEUE_FULL_DIRTY',
      accepted: true,
      retryable: false,
      details: {
        session_key: 'agent:main:webchat:test',
        orphan_message_id: 'orphan-message',
      },
    }))
    await send

    expect(acceptanceStopPending.value).toBe(false)
  })

  it('keeps and reconciles a pre-ACK Stop when chat.send acceptance is unknown', async () => {
    let rejectSend!: (reason: unknown) => void
    const acceptanceStopPending = ref(false)
    const reconcileTaskOwnership = vi.fn()
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.abort') {
          return Promise.resolve({ aborted: false, reason: 'task_id_required' }) as Promise<T>
        }
        return new Promise<T>((_resolve, reject) => {
          rejectSend = reject
        })
      }) as UseChatSendOptions['rpc']['call'],
    }
    const harness = makeOptions({
      rpc,
      acceptanceStopPending,
      reconcileTaskOwnership,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.any(Object),
    ))
    harness.api.onStop()
    rejectSend(Object.assign(new Error('response lost'), { retryable: true }))
    await send

    expect(acceptanceStopPending.value).toBe(true)
    expect(reconcileTaskOwnership).toHaveBeenCalled()
    expect(harness.options.messages.value.every(message => !message.turnOutcome)).toBe(true)
  })

  it('carries a pre-ACK Stop through idempotent acceptance replay to the exact task', async () => {
    let rejectFirstSend!: (reason: unknown) => void
    let sendCalls = 0
    const abortCalls: Record<string, unknown>[] = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.abort') {
          abortCalls.push(params || {})
          return Promise.resolve({ aborted: Boolean(params?.taskId) }) as Promise<T>
        }
        sendCalls += 1
        if (sendCalls === 1) {
          return new Promise<T>((_resolve, reject) => {
            rejectFirstSend = reject
          })
        }
        return Promise.resolve({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-from-replayed-receipt',
          task_status: 'queued',
          user_message_id: 'message-from-replayed-receipt',
        }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const acceptanceStopPending = ref(false)
    const taskOwnership = useChatTaskOwnership()
    const harness = makeOptions({ rpc, acceptanceStopPending, taskOwnership })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const firstSend = harness.api.onSend()
    await vi.waitFor(() => expect(sendCalls).toBe(1))
    harness.api.onStop()
    rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
    await firstSend
    expect(acceptanceStopPending.value).toBe(true)

    await harness.api.onSend()

    expect(abortCalls).toContainEqual({
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-from-replayed-receipt',
      source: 'webui_stop',
      scope: 'task',
    })
    expect(taskOwnership.stopRequestedTaskId.value).toBe('task-from-replayed-receipt')
  })

  it('carries an unknown-acceptance Stop across session navigation and receipt replay', async () => {
    const sessionA = 'agent:main:webchat:unknown-A'
    const sessionB = 'agent:main:webchat:other-B'
    const sessionKey = ref(sessionA)
    const acceptanceStopPending = ref(false)
    let rejectFirstSend!: (reason: unknown) => void
    let sendCalls = 0
    const abortCalls: Record<string, unknown>[] = []
    const requestIds: string[] = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.abort') {
          abortCalls.push(params || {})
          return Promise.resolve({ aborted: Boolean(params?.taskId) }) as Promise<T>
        }
        sendCalls += 1
        requestIds.push(String(params?.clientRequestId || ''))
        if (sendCalls === 1) {
          return new Promise<T>((_resolve, reject) => {
            rejectFirstSend = reject
          })
        }
        return Promise.resolve({
          sessionKey: sessionA,
          task_id: 'task-receipt-after-navigation',
          task_status: 'running',
          user_message_id: 'message-receipt-after-navigation',
        }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const taskOwnership = useChatTaskOwnership()
    const harness = makeOptions({
      rpc,
      sessionKey,
      acceptanceStopPending,
      taskOwnership,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const firstSend = harness.api.onSend()
    await vi.waitFor(() => expect(sendCalls).toBe(1))
    harness.api.onStop()
    rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
    await firstSend
    expect(acceptanceStopPending.value).toBe(true)

    // The session runtime resets its visible per-session latch while showing B.
    // The durable/recovered A attempt must still remember that its user asked to
    // Stop, rather than relying only on this currently displayed ref.
    sessionKey.value = sessionB
    acceptanceStopPending.value = false
    taskOwnership.reset(false)
    sessionKey.value = sessionA
    taskOwnership.reset(true)

    await harness.api.onSend()

    expect(requestIds).toHaveLength(2)
    expect(requestIds[1]).toBe(requestIds[0])
    expect(abortCalls).toContainEqual({
      sessionKey: sessionA,
      taskId: 'task-receipt-after-navigation',
      source: 'webui_stop',
      scope: 'task',
    })
  })

  it('automatically replays an unknown stopped acceptance and exactly aborts its receipt task', async () => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      let sendCalls = 0
      const requestIds: string[] = []
      const abortCalls: Record<string, unknown>[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            return Promise.resolve({ aborted: Boolean(params?.taskId) }) as Promise<T>
          }
          sendCalls += 1
          requestIds.push(String(params?.clientRequestId || ''))
          if (sendCalls === 1) {
            return new Promise<T>((_resolve, reject) => {
              rejectFirstSend = reject
            })
          }
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: 'task-auto-replayed-receipt',
            task_status: 'running',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const acceptanceStopPending = ref(false)
      const taskOwnership = useChatTaskOwnership()
      const harness = makeOptions({
        rpc,
        acceptanceStopPending,
        taskOwnership,
        reconcileTaskOwnership: vi.fn(async () => {}),
      })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const firstSend = harness.api.onSend()
      await Promise.resolve()
      expect(sendCalls).toBe(1)
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
      await firstSend

      await vi.runAllTimersAsync()
      await Promise.resolve()

      expect(sendCalls).toBe(2)
      expect(requestIds[1]).toBe(requestIds[0])
      expect(abortCalls).toContainEqual({
        sessionKey: 'agent:main:webchat:test',
        taskId: 'task-auto-replayed-receipt',
        source: 'webui_stop',
        scope: 'task',
      })
      expect(acceptanceStopPending.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('retries the exact recovered task Stop when its first abort is not acknowledged', async () => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      let sendCalls = 0
      let exactAbortCalls = 0
      const requestIds: string[] = []
      const abortCalls: Record<string, unknown>[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            if (!params?.taskId) return Promise.resolve({ aborted: false }) as Promise<T>
            exactAbortCalls += 1
            return Promise.resolve({ aborted: exactAbortCalls > 1 }) as Promise<T>
          }
          sendCalls += 1
          requestIds.push(String(params?.clientRequestId || ''))
          if (sendCalls === 1) {
            return new Promise<T>((_resolve, reject) => {
              rejectFirstSend = reject
            })
          }
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: 'task-recovered-abort-retry',
            task_status: 'running',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const acceptanceStopPending = ref(false)
      const taskOwnership = useChatTaskOwnership()
      const harness = makeOptions({
        rpc,
        acceptanceStopPending,
        taskOwnership,
        reconcileTaskOwnership: vi.fn(async () => {}),
      })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const firstSend = harness.api.onSend()
      await Promise.resolve()
      expect(sendCalls).toBe(1)
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
      await firstSend

      await vi.advanceTimersByTimeAsync(250)
      expect(sendCalls).toBe(2)
      expect(requestIds[1]).toBe(requestIds[0])
      expect(exactAbortCalls).toBe(1)
      expect(acceptanceStopPending.value).toBe(true)

      await vi.advanceTimersByTimeAsync(1_000)
      expect(sendCalls).toBe(2)
      expect(exactAbortCalls).toBe(2)
      expect(abortCalls.filter(call => call.taskId)).toEqual([
        {
          sessionKey: 'agent:main:webchat:test',
          taskId: 'task-recovered-abort-retry',
          source: 'webui_stop',
          scope: 'task',
        },
        {
          sessionKey: 'agent:main:webchat:test',
          taskId: 'task-recovered-abort-retry',
          source: 'webui_stop',
          scope: 'task',
        },
      ])
      expect(acceptanceStopPending.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it.each(['task_not_active', 'task_mismatch'])(
    'settles recovered Stop when exact abort reports %s',
    async (abortReason) => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      let sendCalls = 0
      let exactAbortCalls = 0
      const requestIds: string[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            if (!params?.taskId) return Promise.resolve({ aborted: false }) as Promise<T>
            exactAbortCalls += 1
            return Promise.resolve({
              aborted: false,
              reason: abortReason,
            }) as Promise<T>
          }
          sendCalls += 1
          requestIds.push(String(params?.clientRequestId || ''))
          if (sendCalls === 1) {
            return new Promise<T>((_resolve, reject) => {
              rejectFirstSend = reject
            })
          }
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: sendCalls === 2 ? 'task-already-terminal' : 'task-new',
            task_status: 'running',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const acceptanceStopPending = ref(false)
      const taskOwnership = useChatTaskOwnership()
      const reconcileTaskOwnership = vi.fn(async () => {
        taskOwnership.noteTerminal('task-already-terminal')
      })
      const harness = makeOptions({
        rpc,
        acceptanceStopPending,
        taskOwnership,
        reconcileTaskOwnership,
      })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const firstSend = harness.api.onSend()
      await Promise.resolve()
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
      await firstSend

      await vi.advanceTimersByTimeAsync(30_250)

      expect(sendCalls).toBe(2)
      expect(exactAbortCalls).toBe(1)
      expect(reconcileTaskOwnership).toHaveBeenCalled()
      expect(acceptanceStopPending.value).toBe(false)
      expect(harness.options.messages.value.every(message => !message.turnOutcome)).toBe(true)

      harness.options.inputText.value = 'new question after the settled Stop'
      await harness.api.onSend()

      expect(sendCalls).toBe(3)
      expect(requestIds[1]).toBe(requestIds[0])
      expect(requestIds[2]).not.toBe(requestIds[0])
    } finally {
      vi.useRealTimers()
    }
    },
  )

  it('keeps automatically replaying the stopped request beyond a 30 second disconnect', async () => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      let sendCalls = 0
      const requestIds: string[] = []
      const abortCalls: Record<string, unknown>[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            return Promise.resolve({ aborted: Boolean(params?.taskId) }) as Promise<T>
          }
          sendCalls += 1
          requestIds.push(String(params?.clientRequestId || ''))
          if (sendCalls === 1) {
            return new Promise<T>((_resolve, reject) => {
              rejectFirstSend = reject
            })
          }
          if (sendCalls < 6) {
            return Promise.reject(Object.assign(new Error('still disconnected'), {
              retryable: true,
            })) as Promise<T>
          }
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: 'task-recovered-after-long-disconnect',
            task_status: 'running',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const acceptanceStopPending = ref(false)
      const harness = makeOptions({
        rpc,
        acceptanceStopPending,
        taskOwnership: useChatTaskOwnership(),
        reconcileTaskOwnership: vi.fn(async () => {}),
      })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const firstSend = harness.api.onSend()
      await Promise.resolve()
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
      await firstSend

      // 250 + 1,000 + 4,000 + 15,000 + 15,000 ms. Recovery must not
      // silently stop after exhausting the first pass through the backoff.
      await vi.advanceTimersByTimeAsync(35_250)

      expect(sendCalls).toBe(6)
      expect(new Set(requestIds)).toEqual(new Set([requestIds[0]]))
      expect(abortCalls).toContainEqual({
        sessionKey: 'agent:main:webchat:test',
        taskId: 'task-recovered-after-long-disconnect',
        source: 'webui_stop',
        scope: 'task',
      })
      expect(acceptanceStopPending.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('automatically replays unknown B without aborting it when Stop targeted hydrated A', async () => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      let sendCalls = 0
      const requestIds: string[] = []
      const abortCalls: Record<string, unknown>[] = []
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            abortCalls.push(params || {})
            return Promise.resolve({ aborted: true }) as Promise<T>
          }
          sendCalls += 1
          requestIds.push(String(params?.clientRequestId || ''))
          if (sendCalls === 1) {
            return new Promise<T>((_resolve, reject) => {
              rejectFirstSend = reject
            })
          }
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: 'task-B-replayed',
            task_status: 'queued',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const taskOwnership = useChatTaskOwnership()
      const activeStreamTaskId = ref('')
      const harness = makeOptions({
        rpc,
        taskOwnership,
        activeStreamTaskId,
        reconcileTaskOwnership: vi.fn(async () => {}),
      })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const firstSendB = harness.api.onSend()
      await Promise.resolve()
      expect(sendCalls).toBe(1)
      taskOwnership.applySnapshot({
        run_status: 'running',
        active_task: { task_id: 'task-A', status: 'running' },
      }, true)
      activeStreamTaskId.value = 'task-A'
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('B response lost'), { retryable: true }))
      await firstSendB

      await vi.runAllTimersAsync()
      await Promise.resolve()

      expect(sendCalls).toBe(2)
      expect(requestIds[1]).toBe(requestIds[0])
      expect(abortCalls).toEqual([{
        sessionKey: 'agent:main:webchat:test',
        taskId: 'task-A',
        source: 'webui_stop',
        scope: 'task',
      }])
      expect(taskOwnership.runningTaskId.value).toBe('task-A')
      expect(taskOwnership.queuedTaskIds.value.has('task-B-replayed')).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('scopes unknown Stop recovery admission to A while allowing B to send', async () => {
    vi.useFakeTimers()
    try {
      const sessionA = 'agent:main:webchat:recovering-A'
      const sessionB = 'agent:main:webchat:independent-B'
      const sessionKey = ref(sessionA)
      const inputText = ref('question A')
      const acceptanceStopPending = ref(false)
      const acceptanceRecoveryPending = ref(false)
      let rejectFirstA!: (reason: unknown) => void
      let aSendCalls = 0
      let bSendCalls = 0
      let settleARecovery = false
      const rpc = {
        call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
          if (method === 'chat.abort') {
            return Promise.resolve({ aborted: false }) as Promise<T>
          }
          const key = String(params?.sessionKey || '')
          if (key === sessionA) {
            aSendCalls += 1
            if (aSendCalls === 1) {
              return new Promise<T>((_resolve, reject) => {
                rejectFirstA = reject
              })
            }
            if (!settleARecovery) {
              return Promise.reject(Object.assign(new Error('A still disconnected'), {
                retryable: true,
              })) as Promise<T>
            }
            return Promise.resolve({
              sessionKey: sessionA,
              task_id: 'task-A',
              task_status: 'cancelled',
            }) as Promise<T>
          }
          bSendCalls += 1
          return Promise.resolve({
            sessionKey: sessionB,
            task_id: 'task-B',
            task_status: 'succeeded',
          }) as Promise<T>
        }) as UseChatSendOptions['rpc']['call'],
      }
      const enqueuePendingInput = vi.fn(() => true)
      const harness = makeOptions({
        rpc,
        sessionKey,
        inputText,
        acceptanceStopPending,
        acceptanceRecoveryPending,
        enqueuePendingInput,
        reconcileTaskOwnership: vi.fn(async () => {}),
      })
      harness.stream.startStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = true
      })
      harness.stream.endStreaming = vi.fn(() => {
        harness.stream.isStreaming.value = false
      })

      const firstA = harness.api.onSend()
      await Promise.resolve()
      harness.api.onStop()
      rejectFirstA(Object.assign(new Error('A response lost'), { retryable: true }))
      await firstA
      await nextTick()
      expect(acceptanceRecoveryPending.value).toBe(true)

      sessionKey.value = sessionB
      // The real session runtime clears the visible, session-local pre-ACK
      // latch on route switch; the request-owned recovery registry survives.
      acceptanceStopPending.value = false
      inputText.value = 'question B'
      await nextTick()
      expect(acceptanceRecoveryPending.value).toBe(false)
      await harness.api.onSend()
      expect(bSendCalls).toBe(1)

      sessionKey.value = sessionA
      inputText.value = 'follow-up C for A'
      await nextTick()
      expect(acceptanceRecoveryPending.value).toBe(true)
      await harness.api.onSend()
      expect(aSendCalls).toBe(1)
      expect(enqueuePendingInput).toHaveBeenCalledWith('follow-up C for A', undefined)

      settleARecovery = true
      await vi.advanceTimersByTimeAsync(250)
      await nextTick()
      expect(aSendCalls).toBe(2)
      expect(acceptanceRecoveryPending.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reconciles an aborted:false response without synthesizing a cancelled terminal', async () => {
    pushToast.mockClear()
    const taskOwnership = useChatTaskOwnership()
    taskOwnership.noteRunning('task-A')
    taskOwnership.noteQueued('task-B')
    const reconcileTaskOwnership = vi.fn()
    const rpc = {
      call: vi.fn().mockResolvedValue({ aborted: false }),
    }
    const harness = makeOptions({
      rpc,
      taskOwnership,
      reconcileTaskOwnership,
      activeStreamTaskId: ref('task-A'),
      activeStreamSessionKey: ref('agent:main:webchat:test'),
      messages: ref<ChatMessage[]>([{
        role: 'user',
        text: 'A',
        ts: 1,
        turnId: 'task-A',
      }]),
    })
    harness.stream.isStreaming.value = true

    harness.api.onStop()
    await vi.waitFor(() => expect(reconcileTaskOwnership).toHaveBeenCalledOnce())

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-A',
      source: 'webui_stop',
      scope: 'task',
    })
    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect([...taskOwnership.queuedTaskIds.value]).toEqual(['task-B'])
    expect(taskOwnership.stopRequestedTaskId.value).toBe('')
    expect(harness.stream.endStreaming).not.toHaveBeenCalled()
    expect(harness.options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    expect(harness.options.messages.value[0]?.turnOutcome).toBeUndefined()
    expect(pushToast).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toContainEqual(expect.objectContaining({
      role: 'system',
      text: 'Stop could not reach the server — the run may still be finishing.',
    }))
  })

  it('does not guess pending steer dispositions or restore them before Stop is authoritative', () => {
    const restoreSteerIntoComposer = vi.fn()
    const messages = ref<ChatMessage[]>([
      {
        role: 'user',
        text: 'first adjustment',
        ts: 1,
        turnId: 'turn-current',
        inputDisposition: 'steering',
      },
      {
        role: 'user',
        text: 'second adjustment',
        ts: 2,
        turnId: 'turn-current',
        inputDisposition: 'steering',
      },
    ])
    const { api, stream } = makeOptions({
      ...sameTurnSteerOptions(),
      messages,
      restoreSteerIntoComposer,
    })
    stream.isStreaming.value = true

    api.onStop()

    expect(messages.value.map(message => message.inputDisposition)).toEqual([
      'steering',
      'steering',
    ])
    expect(messages.value.every(message => message.steerStopRequested)).toBe(true)
    expect(restoreSteerIntoComposer).not.toHaveBeenCalled()
  })

  it('stops only the authoritative task that owns the stream', () => {
    const activeStreamTaskId = ref('task-old')
    const activeStreamSessionKey = ref('agent:main:webchat:old')
    const { api, rpc, stream } = makeOptions({
      sessionKey: ref('agent:main:webchat:new'),
      activeStreamTaskId,
      activeStreamSessionKey,
    })
    stream.isStreaming.value = true

    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:old',
      taskId: 'task-old',
      source: 'webui_stop',
      scope: 'task',
    })
    // Render ownership remains with the task until its matching terminal.
    expect(activeStreamTaskId.value).toBe('task-old')
  })

  it('prefers the server steer turn when the rendered stream id is stale', () => {
    const { api, rpc, stream } = makeOptions({
      activeStreamTaskId: ref('task-rendered-stale'),
      activeSteerCapability: ref({
        mode: 'same_turn',
        expected_turn_id: 'task-authoritative',
        input_kinds: ['text'],
      }),
    })
    stream.isStreaming.value = true

    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-authoritative',
      source: 'webui_stop',
      scope: 'task',
    })
  })

  it('stops an active subagent group after the parent stream has ended', () => {
    const { api, rpc, stream } = makeOptions({ canStop: () => true })
    stream.isStreaming.value = false

    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      source: 'webui_stop',
    })
  })

  it('targets the selected session for a group-only Stop when stream ownership is stale', () => {
    const selectedSessionKey = 'agent:main:webchat:selected'
    const previousSessionKey = 'agent:main:webchat:previous'
    const { api, rpc, stream } = makeOptions({
      sessionKey: ref(selectedSessionKey),
      activeStreamTaskId: ref(''),
      activeStreamSessionKey: ref(previousSessionKey),
      canStop: () => true,
    })
    stream.isStreaming.value = false

    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: selectedSessionKey,
      source: 'webui_stop',
    })
  })

  it('does not mistake a completed send acceptance for a later group-only Stop', async () => {
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.send') {
          return Promise.resolve({
            sessionKey: 'agent:main:webchat:test',
            task_id: 'task-parent-settled',
            task_status: 'running',
          }) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const taskOwnership = useChatTaskOwnership()
    const activeStreamTaskId = ref('')
    const activeStreamSessionKey = ref('')
    const harness = makeOptions({
      rpc,
      taskOwnership,
      activeStreamTaskId,
      activeStreamSessionKey,
      canStop: () => true,
    })

    await harness.api.onSend()
    taskOwnership.noteTerminal('task-parent-settled')
    activeStreamTaskId.value = ''
    activeStreamSessionKey.value = ''
    harness.stream.isStreaming.value = false

    harness.api.onStop()

    expect(rpc.call).toHaveBeenLastCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      source: 'webui_stop',
    })
  })

  it('ignores a stale acceptance after navigation and Stops only the new running task', async () => {
    const oldSessionKey = 'agent:main:webchat:old'
    const newSessionKey = 'agent:main:webchat:new'
    let resolveOldSend!: (value: unknown) => void
    const abortCalls: Record<string, unknown>[] = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveOldSend = resolve as (value: unknown) => void
          })
        }
        if (method === 'chat.abort') {
          abortCalls.push(params || {})
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        return Promise.resolve({}) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const sessionKey = ref(oldSessionKey)
    const taskOwnership = useChatTaskOwnership()
    const activeStreamTaskId = ref('')
    const activeStreamSessionKey = ref('')
    const harness = makeOptions({
      rpc,
      sessionKey,
      taskOwnership,
      activeStreamTaskId,
      activeStreamSessionKey,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    const oldSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: oldSessionKey }),
    ))

    sessionKey.value = newSessionKey
    taskOwnership.reset(true)
    taskOwnership.noteRunning('task-new')
    activeStreamTaskId.value = 'task-new'
    activeStreamSessionKey.value = newSessionKey

    resolveOldSend({
      sessionKey: oldSessionKey,
      task_id: 'task-old-late',
      task_status: 'running',
    })
    await oldSend

    expect(taskOwnership.runningTaskId.value).toBe('task-new')
    expect(taskOwnership.queuedTaskIds.value.has('task-old-late')).toBe(false)

    harness.api.onStop()
    await vi.waitFor(() => expect(abortCalls).toContainEqual({
      sessionKey: newSessionKey,
      taskId: 'task-new',
      source: 'webui_stop',
      scope: 'task',
    }))
    expect(abortCalls).not.toContainEqual(expect.objectContaining({
      taskId: 'task-old-late',
      source: 'webui_stop',
    }))
  })

  it('queues the next turn while a pre-ACK Stop is awaiting its terminal', async () => {
    const pendingResponses: Array<(value: unknown) => void> = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.abort') return Promise.resolve({ aborted: true }) as Promise<T>
        return new Promise<T>((resolve) => {
          pendingResponses.push(resolve as (value: unknown) => void)
        })
      }) as UseChatSendOptions['rpc']['call'],
    }
    const inputText = ref('first')
    const messages = ref<ChatMessage[]>([])
    const activeStreamTaskId = ref('')
    const enqueuePendingInput = vi.fn(() => true)
    const { api, stream } = makeOptions({
      rpc,
      inputText,
      messages,
      activeStreamTaskId,
      enqueuePendingInput,
    })
    stream.startStreaming = vi.fn(() => { stream.isStreaming.value = true })
    stream.endStreaming = vi.fn(() => { stream.isStreaming.value = false })

    const firstSend = api.onSend()
    const firstClientMessageId = messages.value[0]?.clientId
    api.onStop()

    inputText.value = 'second'
    await api.onSend()

    expect(pendingResponses).toHaveLength(1)
    expect(enqueuePendingInput).toHaveBeenCalledWith('second', undefined)

    pendingResponses[0]({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-A',
      user_message_id: 'message-A',
    })
    await firstSend

    expect(activeStreamTaskId.value).toBe('task-A')
    expect(messages.value.find(message => message.clientId === firstClientMessageId)?.messageId)
      .toBe('message-A')
    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-A',
      source: 'webui_stop',
      scope: 'task',
    })
  })
})

describe('useChatSend Ensemble image guard', () => {
  function readyAttachment(
    mime: string,
    overrides: Partial<Attachment> = {},
  ): Attachment {
    return {
      kind: 'staged',
      local_id: 91,
      name: 'input.bin',
      mime,
      file_uuid: 'file-ready',
      ...overrides,
    }
  }

  it('blocks a direct Ensemble image send before any visible or RPC mutation', async () => {
    const image = readyAttachment('image/png', { name: 'photo.png' })
    const pendingAttachments = ref<Attachment[]>([image])
    const inputText = ref('describe this')
    const prepareAttachmentsForSend = vi.fn(async () => true)
    const { api, options, rpc, stream } = makeOptions({
      inputText,
      pendingAttachments,
      modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
      prepareAttachmentsForSend,
    })

    await api.onSend()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(prepareAttachmentsForSend).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(inputText.value).toBe('describe this')
    expect(pendingAttachments.value).toEqual([image])
    expect(options.pendingSessionIntent.value).toBeNull()
    expect(options.closeSlashMenu).not.toHaveBeenCalled()
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('blocks image sends while routing settings are being written', async () => {
    const image = readyAttachment('image/webp')
    const pendingAttachments = ref<Attachment[]>([image])
    const { api, options, rpc } = makeOptions({
      pendingAttachments,
      modelRoutingMode: ref<'off'>('off'),
      modelRoutingSettingsBusy: ref(true),
    })

    await api.onSend()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(options.inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([image])
  })

  it.each(['queue', 'steer'] as const)(
    'queues an Ensemble image draft in %s mode without pretending to steer',
    async (busySendMode) => {
      const image = readyAttachment('image/jpeg')
      const pendingAttachments = ref<Attachment[]>([image])
      const enqueuePendingInput = vi.fn(() => true)
      const { api, options, rpc, stream } = makeOptions({
        pendingAttachments,
        busySendMode: ref<BusySendMode>(busySendMode),
        modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
        enqueuePendingInput,
      })
      stream.isStreaming.value = true

      await api.onSend()

      expect(rpc.call).not.toHaveBeenCalled()
      expect(enqueuePendingInput).toHaveBeenCalledWith('hello', undefined)
      expect(options.messages.value).toEqual([])
      expect(options.inputText.value).toBe('hello')
      expect(pendingAttachments.value).toEqual([image])
    },
  )

  it('rechecks routing after attachment preparation without consuming the draft', async () => {
    const image = readyAttachment('image/gif')
    const pendingAttachments = ref<Attachment[]>([image])
    const modelRoutingMode = ref<'off' | 'llm_ensemble'>('off')
    const prepareAttachmentsForSend = vi.fn(async () => {
      modelRoutingMode.value = 'llm_ensemble'
      return true
    })
    const { api, options, rpc } = makeOptions({
      pendingAttachments,
      modelRoutingMode,
      prepareAttachmentsForSend,
    })

    await api.onSend()

    expect(prepareAttachmentsForSend).toHaveBeenCalledOnce()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(options.inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([image])
  })

  it('blocks a recovered image retry after the user switches to Ensemble', async () => {
    const image = readyAttachment('image/jpg', { name: 'photo.jpg' })
    const pendingAttachments = ref<Attachment[]>([image])
    const modelRoutingMode = ref<'off' | 'llm_ensemble'>('off')
    const rpc = {
      call: vi.fn().mockRejectedValue(new Error('connection lost')),
    }
    const { api, options } = makeOptions({ rpc, pendingAttachments, modelRoutingMode })

    await api.onSend()
    expect(rpc.call).toHaveBeenCalledOnce()
    modelRoutingMode.value = 'llm_ensemble'

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledOnce()
    expect(options.inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([image])
  })

  it('preserves an auto-drained queued image after routing switches to Ensemble', async () => {
    vi.useFakeTimers()
    try {
      const image = readyAttachment('image/png')
      const inputText = ref('queued image')
      const pendingAttachments = ref<Attachment[]>([image])
      const pendingSessionIntent = ref<string | null>(null)
      const sessionKey = ref('agent:main:webchat:test')
      const modelRoutingMode = ref<'off' | 'llm_ensemble'>('off')
      const { stream } = makeOptions()
      stream.isStreaming.value = true
      let sendCurrentInput: () => void = () => {}
      const pendingRecords = new Map<string, import('@/utils/chat/pendingInputWal').PendingInputWalRecord>()
      const pending = useChatPendingQueue({
        sessionKey,
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        isStreaming: stream.isStreaming,
        isBlocked: () => false,
        autoResizeTextarea: vi.fn(),
        sendCurrentInput: () => sendCurrentInput(),
        resetInputHistory: vi.fn(),
        hasComposer: () => true,
        pendingInputWal: {
          put: async record => { pendingRecords.set(record.pendingInputId, record) },
          list: async key => [...pendingRecords.values()].filter(record => (
            record.sessionKey === key
          )),
          delete: async pendingInputId => { pendingRecords.delete(pendingInputId) },
          close: () => {},
        },
        supportsMethod: () => false,
      })
      const { api, options, rpc } = makeOptions({
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        sessionKey,
        modelRoutingMode,
        busySendMode: pending.busySendMode,
        stream,
        enqueuePendingInput: pending.enqueuePendingInput,
        popAllPendingIntoComposer: pending.popAllPendingIntoComposer,
      })
      sendCurrentInput = () => { void api.onSend() }

      await api.onSend()
      expect(pending.pendingQueue.value).toHaveLength(1)
      expect(inputText.value).toBe('')
      expect(pendingAttachments.value).toEqual([])

      modelRoutingMode.value = 'llm_ensemble'
      pending.schedulePendingDrainAfterTerminal()
      stream.isStreaming.value = false
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(pending.pendingQueue.value).toEqual([])
      expect(rpc.call).not.toHaveBeenCalled()
      expect(options.messages.value).toEqual([])
      expect(inputText.value).toBe('queued image')
      expect(pendingAttachments.value).toEqual([image])
      pending.cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it('lets a handled local slash command run without consuming attached images', async () => {
    const image = readyAttachment('image/png')
    const pendingAttachments = ref<Attachment[]>([image])
    const inputText = ref('/status')
    const executeSlashCommand = vi.fn(async () => true)
    const { api, options, rpc } = makeOptions({
      inputText,
      pendingAttachments,
      modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
      executeSlashCommand,
    })

    await api.onSend()

    expect(executeSlashCommand).toHaveBeenCalledWith('/status', 'registered')
    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/status')
    expect(pendingAttachments.value).toEqual([image])
    expect(options.messages.value).toEqual([])
  })

  it.each(['application/pdf', 'image/svg+xml', 'image/tiff'])(
    'does not block the non-model-image MIME %s in Ensemble mode',
    async (mime) => {
      const pendingAttachments = ref<Attachment[]>([readyAttachment(mime)])
      const { api, rpc } = makeOptions({
        pendingAttachments,
        modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
      })

      await api.onSend()

      expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
        attachments: [expect.objectContaining({ mime })],
      }))
    },
  )

  it('localizes a defensive server rejection while preserving its error code', async () => {
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('server fallback text'), {
        code: 'ensemble_multimodal_unsupported',
        retryable: false,
      })),
    }
    const { api, options } = makeOptions({ rpc })

    await api.onSend()

    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      errorCode: 'ensemble_multimodal_unsupported',
      text: "Ensemble doesn't support image input yet. Under Model routing, choose AI-powered single-model router with an image-capable tier configured, or turn routing Off and select an image-capable model.",
    })
  })

  it('localizes a terminal response code but leaves an unknown server message unchanged', async () => {
    const knownRpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_status: 'failed',
        terminal_reason: 'ensemble_multimodal_unsupported',
        terminal_message: 'server fallback text',
      }),
    }
    const known = makeOptions({ rpc: knownRpc })
    await known.api.onSend()
    expect(known.options.messages.value[known.options.messages.value.length - 1]).toMatchObject({
      errorCode: 'ensemble_multimodal_unsupported',
      text: "Ensemble doesn't support image input yet. Under Model routing, choose AI-powered single-model router with an image-capable tier configured, or turn routing Off and select an image-capable model.",
    })

    const unknownRpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_status: 'failed',
        terminal_reason: 'provider_custom_failure',
        terminal_message: 'Provider supplied this exact explanation.',
      }),
    }
    const unknown = makeOptions({ rpc: unknownRpc })
    await unknown.api.onSend()
    expect(unknown.options.messages.value[unknown.options.messages.value.length - 1]).toMatchObject({
      errorCode: 'provider_custom_failure',
      text: 'Provider supplied this exact explanation.',
    })
  })

  it('sends the draft Plan mode atomically with intent=new_chat', async () => {
    const { api, rpc } = makeOptions({
      pendingSessionIntent: ref('new_chat'),
      initialCollaborationMode: ref<CollaborationMode>('plan'),
    })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      intent: 'new_chat',
      collaborationMode: 'plan',
    }))
  })

  it('keeps the default draft compatible with gateways that predate initial modes', async () => {
    const { api, rpc } = makeOptions({
      pendingSessionIntent: ref('new_chat'),
      initialCollaborationMode: ref<CollaborationMode>('default'),
    })

    await api.onSend()

    const params = rpc.call.mock.calls[0]?.[1]
    expect(params).toEqual(expect.objectContaining({ intent: 'new_chat' }))
    expect(params).not.toHaveProperty('collaborationMode')
  })

  it('materializes a draft intent only after durable acceptance', async () => {
    let resolveSend!: (value: { sessionKey: string; task_id: string }) => void
    const pendingSessionIntent = ref<string | null>('new_chat')
    const rpc = {
      call: vi.fn(() => new Promise<{ sessionKey: string; task_id: string }>(resolve => {
        resolveSend = resolve
      })),
    }
    const { api } = makeOptions({
      rpc: rpc as UseChatSendOptions['rpc'],
      pendingSessionIntent,
    })

    const send = api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    expect(pendingSessionIntent.value).toBe('new_chat')

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-first',
    })
    await send

    expect(pendingSessionIntent.value).toBeNull()
  })

  it('does not let a stale accepted response materialize a newer draft', async () => {
    let resolveSend!: (value: { sessionKey: string; task_id: string }) => void
    const firstKey = 'agent:main:webchat:first-draft'
    const secondKey = 'agent:main:webchat:second-draft'
    const sessionKey = ref(firstKey)
    const pendingSessionIntent = ref<string | null>('new_chat')
    const rpc = {
      call: vi.fn(() => new Promise<{ sessionKey: string; task_id: string }>(resolve => {
        resolveSend = resolve
      })),
    }
    const { api } = makeOptions({
      rpc: rpc as UseChatSendOptions['rpc'],
      sessionKey,
      pendingSessionIntent,
    })

    const send = api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    sessionKey.value = secondKey

    resolveSend({ sessionKey: firstKey, task_id: 'task-first' })
    await send

    expect(sessionKey.value).toBe(secondKey)
    expect(pendingSessionIntent.value).toBe('new_chat')
  })

  it('does not attach an initial collaboration mode to an existing-session send', async () => {
    const { api, rpc } = makeOptions({
      initialCollaborationMode: ref<CollaborationMode>('plan'),
    })

    await api.onSend()

    const params = rpc.call.mock.calls[0]?.[1]
    expect(params).not.toHaveProperty('collaborationMode')
  })
})

describe('useChatSend slash-prefixed input fall-through', () => {
  it('queues unknown slash-prefixed text as a follow-up while a turn is busy', async () => {
    const inputText = ref('/gamemode creative')
    const enqueuePendingInput = vi.fn(() => true)
    const classifySlashCommand = vi.fn(async () => 'unknown' as const)
    const { api, rpc, stream } = makeOptions({
      inputText,
      enqueuePendingInput,
      classifySlashCommand,
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(classifySlashCommand).toHaveBeenCalledWith('/gamemode creative')
    expect(enqueuePendingInput).toHaveBeenCalledWith(
      '/gamemode creative',
      undefined,
      { confirmedPlainText: true },
    )
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('durably queues and drains unknown slash text through the real pending queue', async () => {
    vi.useFakeTimers()
    try {
      const inputText = ref('/gamemode creative')
      const pendingAttachments = ref<Attachment[]>([])
      const pendingSessionIntent = ref<string | null>(null)
      const sessionKey = ref('agent:main:webchat:test')
      const { stream } = makeOptions()
      stream.isStreaming.value = true
      const pendingRecords = new Map<
        string,
        import('@/utils/chat/pendingInputWal').PendingInputWalRecord
      >()
      const rpcCall = vi.fn(async (
        method: string,
        params: Record<string, unknown> = {},
      ): Promise<Record<string, unknown>> => {
        if (method === 'sessions.pending_inputs.list') return { items: [] }
        if (method === 'sessions.pending_inputs.enqueue') {
          return { requestFingerprint: 'sha256:unknown-slash', revision: 1 }
        }
        if (method === 'sessions.pending_inputs.dispatch') {
          return { sessionKey: sessionKey.value }
        }
        throw new Error(`unexpected method: ${method} ${JSON.stringify(params)}`)
      })
      const rpc: UseChatSendOptions['rpc'] = {
        call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
          rpcCall(method, params) as Promise<T>
        ),
      }
      let sendApi!: ReturnType<typeof useChatSend>
      const pending = useChatPendingQueue({
        sessionKey,
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        isStreaming: stream.isStreaming,
        isBlocked: () => false,
        autoResizeTextarea: vi.fn(),
        sendCurrentInput: vi.fn(),
        resetInputHistory: vi.fn(),
        hasComposer: () => true,
        pendingInputWal: {
          put: async record => { pendingRecords.set(record.pendingInputId, record) },
          list: async key => [...pendingRecords.values()].filter(record => (
            record.sessionKey === key
          )),
          delete: async pendingInputId => { pendingRecords.delete(pendingInputId) },
          close: () => {},
        },
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
        dispatchPendingItem: (item, ownerSessionKey) => (
          sendApi.sendQueuedFollowup(item, ownerSessionKey)
        ),
      })
      const configured = makeOptions({
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        sessionKey,
        stream,
        rpc,
        busySendMode: pending.busySendMode,
        enqueuePendingInput: pending.enqueuePendingInput,
        enqueuePendingPayload: pending.enqueuePendingPayload,
        popAllPendingIntoComposer: pending.popAllPendingIntoComposer,
        classifySlashCommand: vi.fn(async () => 'unknown' as const),
      })
      sendApi = configured.api

      await sendApi.onSend()

      expect(pending.pendingQueue.value).toHaveLength(1)
      expect(pendingRecords.size).toBe(1)
      await vi.waitFor(() => {
        expect(pending.pendingQueue.value[0]?.pendingPersistenceState).toBe('staged')
      })
      expect(pendingRecords.values().next().value).toMatchObject({
        text: '/gamemode creative',
        confirmedPlainText: true,
      })
      expect(rpcCall).toHaveBeenCalledWith(
        'sessions.pending_inputs.enqueue',
        expect.objectContaining({
          message: '/gamemode creative',
          confirmedPlainText: true,
        }),
      )
      expect(inputText.value).toBe('')
      stream.isStreaming.value = false
      pending.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(rpcCall).toHaveBeenCalledWith(
        'sessions.pending_inputs.dispatch',
        expect.objectContaining({
          requestFingerprint: 'sha256:unknown-slash',
        }),
      )
      expect(rpcCall).not.toHaveBeenCalledWith('chat.send', expect.anything())
      expect(pending.pendingQueue.value).toHaveLength(0)
      expect(pendingRecords.size).toBe(0)
      pending.cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it('durably queues and drains an escaped registered slash as literal text', async () => {
    vi.useFakeTimers()
    try {
      const attachment: Attachment = {
        kind: 'staged',
        local_id: 95,
        name: 'literal-context.txt',
        mime: 'text/plain',
        file_uuid: 'file-literal-slash-attachment',
      }
      const inputText = ref('//coding')
      const pendingAttachments = ref<Attachment[]>([attachment])
      const pendingSessionIntent = ref<string | null>(null)
      const sessionKey = ref('agent:main:webchat:test')
      const { stream } = makeOptions()
      stream.isStreaming.value = true
      const pendingRecords = new Map<
        string,
        import('@/utils/chat/pendingInputWal').PendingInputWalRecord
      >()
      const rpcCall = vi.fn(async (
        method: string,
        params: Record<string, unknown> = {},
      ): Promise<Record<string, unknown>> => {
        if (method === 'sessions.pending_inputs.list') return { items: [] }
        if (method === 'sessions.pending_inputs.enqueue') {
          return { requestFingerprint: 'sha256:literal-slash', revision: 1 }
        }
        if (method === 'sessions.pending_inputs.dispatch') {
          return { sessionKey: sessionKey.value }
        }
        throw new Error(`unexpected method: ${method} ${JSON.stringify(params)}`)
      })
      const rpc: UseChatSendOptions['rpc'] = {
        call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
          rpcCall(method, params) as Promise<T>
        ),
      }
      let sendApi!: ReturnType<typeof useChatSend>
      const pending = useChatPendingQueue({
        sessionKey,
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        isStreaming: stream.isStreaming,
        isBlocked: () => false,
        autoResizeTextarea: vi.fn(),
        sendCurrentInput: vi.fn(),
        resetInputHistory: vi.fn(),
        hasComposer: () => true,
        pendingInputWal: {
          put: async record => { pendingRecords.set(record.pendingInputId, record) },
          list: async key => [...pendingRecords.values()].filter(record => (
            record.sessionKey === key
          )),
          delete: async pendingInputId => { pendingRecords.delete(pendingInputId) },
          close: () => {},
        },
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
        dispatchPendingItem: (item, ownerSessionKey) => (
          sendApi.sendQueuedFollowup(item, ownerSessionKey)
        ),
      })
      const classifySlashCommand = vi.fn(async () => 'registered' as const)
      const executeSlashCommand = vi.fn(async () => true)
      const configured = makeOptions({
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        sessionKey,
        stream,
        rpc,
        busySendMode: pending.busySendMode,
        enqueuePendingInput: pending.enqueuePendingInput,
        enqueuePendingPayload: pending.enqueuePendingPayload,
        popAllPendingIntoComposer: pending.popAllPendingIntoComposer,
        classifySlashCommand,
        executeSlashCommand,
      })
      sendApi = configured.api

      await sendApi.onSend()

      expect(pending.pendingQueue.value).toHaveLength(1)
      expect(pending.pendingQueue.value[0]).toMatchObject({
        text: '//coding',
        attachments: [expect.objectContaining({
          name: attachment.name,
          mime: attachment.mime,
        })],
      })
      expect(pendingRecords.size).toBe(1)
      await vi.waitFor(() => {
        expect(pending.pendingQueue.value[0]?.pendingPersistenceState).toBe('staged')
      })
      expect(rpcCall).toHaveBeenCalledWith(
        'sessions.pending_inputs.enqueue',
        expect.objectContaining({
          message: '/coding',
          displayText: '//coding',
          attachments: [expect.objectContaining({
            file_uuid: 'file-literal-slash-attachment',
          })],
        }),
      )
      stream.isStreaming.value = false
      pending.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(classifySlashCommand).not.toHaveBeenCalled()
      expect(executeSlashCommand).not.toHaveBeenCalled()
      expect(rpcCall).toHaveBeenCalledWith(
        'sessions.pending_inputs.dispatch',
        expect.objectContaining({
          requestFingerprint: 'sha256:literal-slash',
        }),
      )
      expect(rpcCall).not.toHaveBeenCalledWith('chat.send', expect.anything())
      expect(pending.pendingQueue.value).toHaveLength(0)
      expect(pendingRecords.size).toBe(0)
      pending.cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it.each(['registered', 'unavailable'] as const)(
    'keeps %s slash input editable while a turn is busy',
    async classification => {
      const inputText = ref('/coding')
      const enqueuePendingInput = vi.fn(() => true)
      const { api, rpc, stream } = makeOptions({
        inputText,
        enqueuePendingInput,
        classifySlashCommand: vi.fn(async () => classification),
      })
      stream.isStreaming.value = true

      await api.onSend()

      expect(enqueuePendingInput).not.toHaveBeenCalled()
      expect(rpc.call).not.toHaveBeenCalled()
      expect(inputText.value).toBe('/coding')
    },
  )

  it('sends an unknown slash-prefixed input as a normal message exactly once', async () => {
    const inputText = ref('/gamemode creative')
    const executeSlashCommand = vi.fn(async () => false)
    const { api, rpc } = makeOptions({
      inputText,
      classifySlashCommand: vi.fn(async () => 'unknown' as const),
      executeSlashCommand,
    })

    await api.onSend()

    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: '/gamemode creative',
    }))
  })

  it('keeps attachments on an unknown slash-prefixed normal message', async () => {
    const pendingAttachments = ref<Attachment[]>([{
      kind: 'staged',
      local_id: 92,
      name: 'commands.txt',
      mime: 'text/plain',
      file_uuid: 'file-slash-text',
    }])
    const { api, rpc } = makeOptions({
      inputText: ref('/usr/bin/env'),
      pendingAttachments,
      classifySlashCommand: vi.fn(async () => 'unknown' as const),
      executeSlashCommand: vi.fn(async () => false),
    })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: '/usr/bin/env',
      attachments: [expect.objectContaining({
        file_uuid: 'file-slash-text',
        mime: 'text/plain',
      })],
    }))
  })

  it('does not send when a registered slash command handles the input', async () => {
    const inputText = ref('/coding')
    const executeSlashCommand = vi.fn(async () => true)
    const { api, rpc } = makeOptions({
      inputText,
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      executeSlashCommand,
    })

    await api.onSend()

    // A registered command is handled by the command path: no chat.send.
    expect(executeSlashCommand).toHaveBeenCalledWith('/coding', 'registered')
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('keeps an idle slash draft owned by A when classification finishes in B', async () => {
    let resolveClassification!: (classification: 'unknown') => void
    const classification = new Promise<'unknown'>(resolve => {
      resolveClassification = resolve
    })
    const sessionKey = ref('agent:main:webchat:A')
    const inputText = ref('/gamemode creative')
    const pendingAttachments = ref<Attachment[]>([{
      kind: 'staged',
      local_id: 93,
      name: 'commands.txt',
      mime: 'text/plain',
      file_uuid: 'file-idle-slash-text',
    }])
    const classifySlashCommand = vi.fn(() => classification)
    const executeSlashCommand = vi.fn(async () => false)
    const { api, rpc } = makeOptions({
      sessionKey,
      inputText,
      pendingAttachments,
      classifySlashCommand,
      executeSlashCommand,
    })

    const sending = api.onSend()
    await vi.waitFor(() => expect(classifySlashCommand).toHaveBeenCalledOnce())
    sessionKey.value = 'agent:main:webchat:B'
    resolveClassification('unknown')
    await sending

    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/gamemode creative')
    expect(pendingAttachments.value).toHaveLength(1)
  })

  it('keeps an idle slash draft when sending becomes blocked during classification', async () => {
    let resolveClassification!: (classification: 'unknown') => void
    const classification = new Promise<'unknown'>(resolve => {
      resolveClassification = resolve
    })
    const inputText = ref('/gamemode creative')
    const sendBlockedReason = ref('')
    const classifySlashCommand = vi.fn(() => classification)
    const { api, rpc } = makeOptions({
      inputText,
      sendBlockedReason,
      classifySlashCommand,
    })

    const sending = api.onSend()
    await vi.waitFor(() => expect(classifySlashCommand).toHaveBeenCalledOnce())
    sendBlockedReason.value = 'Live updates are unavailable'
    resolveClassification('unknown')
    await sending

    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/gamemode creative')
  })

  it('keeps an idle slash draft with its original handoff owner during classification', async () => {
    let resolveClassification!: (classification: 'unknown') => void
    const classification = new Promise<'unknown'>(resolve => {
      resolveClassification = resolve
    })
    const sessionKey = ref('agent:main:webchat:test')
    const inputText = ref('/gamemode creative')
    const pendingQueueOwnerContext = ref({
      sessionKey: sessionKey.value,
      ownerRequestId: 'owner-request-A',
    })
    const enqueuePendingInput = vi.fn(() => true)
    const classifySlashCommand = vi.fn(() => classification)
    const { api, rpc, stream } = makeOptions({
      sessionKey,
      inputText,
      pendingQueueOwnerContext,
      enqueuePendingInput,
      classifySlashCommand,
    })
    stream.isStreaming.value = true

    const sending = api.onSend()
    await vi.waitFor(() => expect(classifySlashCommand).toHaveBeenCalledOnce())
    pendingQueueOwnerContext.value = {
      sessionKey: sessionKey.value,
      ownerRequestId: 'owner-request-B',
    }
    resolveClassification('unknown')
    await sending

    expect(enqueuePendingInput).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/gamemode creative')
  })

  it('keeps an idle slash draft when its workspace becomes unavailable during classification', async () => {
    let resolveClassification!: (classification: 'unknown') => void
    const classification = new Promise<'unknown'>(resolve => {
      resolveClassification = resolve
    })
    let projectBlocker: string | null = null
    const validateActiveProjectBeforeSend = vi.fn(async () => projectBlocker)
    const inputText = ref('/gamemode creative')
    const classifySlashCommand = vi.fn(() => classification)
    const { api, rpc } = makeOptions({
      inputText,
      validateActiveProjectBeforeSend,
      classifySlashCommand,
    })

    const sending = api.onSend()
    await vi.waitFor(() => expect(classifySlashCommand).toHaveBeenCalledOnce())
    projectBlocker = 'removed'
    resolveClassification('unknown')
    await sending

    expect(validateActiveProjectBeforeSend).toHaveBeenCalledTimes(2)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/gamemode creative')
  })

  it('sends // escaped input as literal text with one slash stripped', async () => {
    const inputText = ref('//usr/bin/env')
    const executeSlashCommand = vi.fn(async () => true)
    const { api, rpc } = makeOptions({ inputText, executeSlashCommand })

    await api.onSend()

    // "//" is a literal-slash escape: the command path is skipped entirely and
    // the message is sent with exactly one leading slash removed.
    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: '/usr/bin/env',
    }))
  })

  it('sends an unknown slash-prefixed queued follow-up as a normal message exactly once', async () => {
    const executeSlashCommand = vi.fn(async () => false)
    const { api, rpc } = makeOptions({
      classifySlashCommand: vi.fn(async () => 'unknown' as const),
      executeSlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-unknown-slash-followup',
      text: '/gamemode creative',
      attachments: [],
      intent: null,
    }

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('accepted')

    // The command path reports "unhandled" for unknown slash inputs, so the
    // queued follow-up must fall through to the normal chat.send path — and
    // only once, mirroring the primary onSend contract.
    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: '/gamemode creative',
    }))
  })

  it('keeps a queued follow-up editable when it becomes a registered command', async () => {
    const executeSlashCommand = vi.fn(async () => true)
    const { api, rpc } = makeOptions({
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      executeSlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-registered-slash-followup',
      text: '/coding',
      attachments: [],
      intent: null,
    }

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('not_sent')

    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('cancels a server-staged row without auto-executing a newly registered command', async () => {
    const events: string[] = []
    const cancelDurablePendingItem = vi.fn(async () => {
      events.push('cancel')
      return true
    })
    const executeSlashCommand = vi.fn(async () => {
      events.push('execute')
      return true
    })
    const { api, rpc } = makeOptions({
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      cancelDurablePendingItem,
      executeSlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-staged-registered-slash',
      text: '/coding',
      attachments: [],
      intent: null,
      confirmedPlainText: true,
      pendingInputId: 'pending-staged-registered-slash',
      pendingClientRequestId: 'request-staged-registered-slash',
      pendingClientMessageId: 'message-staged-registered-slash',
      pendingRequestFingerprint: 'sha256:staged-registered-slash',
      pendingPersistenceState: 'staged',
    }

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('not_sent')

    expect(events).toEqual(['cancel'])
    expect(cancelDurablePendingItem).toHaveBeenCalledWith(queued, { retainAfterCancel: true })
    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('keeps a server-staged registered command when its tombstone is unproven', async () => {
    const cancelDurablePendingItem = vi.fn(async () => false)
    const executeSlashCommand = vi.fn(async () => true)
    const { api } = makeOptions({
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      cancelDurablePendingItem,
      executeSlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-staged-registered-retry',
      text: '/coding',
      attachments: [],
      intent: null,
      confirmedPlainText: true,
      pendingInputId: 'pending-staged-registered-retry',
      pendingClientRequestId: 'request-staged-registered-retry',
      pendingClientMessageId: 'message-staged-registered-retry',
      pendingRequestFingerprint: 'sha256:staged-registered-retry',
      pendingPersistenceState: 'staged',
    }

    await expect(api.sendQueuedFollowup(queued)).resolves.toBe('retryable_failure')

    expect(cancelDurablePendingItem).toHaveBeenCalledWith(queued, { retainAfterCancel: true })
    expect(executeSlashCommand).not.toHaveBeenCalled()
  })

  it('does not execute a staged command after its cancellation switches sessions', async () => {
    let resolveCancel!: (cancelled: boolean) => void
    const cancelDurablePendingItem = vi.fn(() => new Promise<boolean>((resolve) => {
      resolveCancel = resolve
    }))
    const executeSlashCommand = vi.fn(async () => true)
    const sessionKey = ref('session-a')
    const { api } = makeOptions({
      sessionKey,
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      cancelDurablePendingItem,
      executeSlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-staged-session-fence',
      text: '/reset',
      attachments: [],
      intent: null,
      confirmedPlainText: true,
      ownerSessionKey: 'session-a',
      pendingInputId: 'pending-staged-session-fence',
      pendingClientRequestId: 'request-staged-session-fence',
      pendingClientMessageId: 'message-staged-session-fence',
      pendingRequestFingerprint: 'sha256:staged-session-fence',
      pendingPersistenceState: 'staged',
    }

    const send = api.sendQueuedFollowup(queued)
    await vi.waitFor(() => expect(cancelDurablePendingItem).toHaveBeenCalledWith(
      queued,
      { retainAfterCancel: true },
    ))
    sessionKey.value = 'session-b'
    resolveCancel(true)

    await expect(send).resolves.toBe('not_sent')
    expect(executeSlashCommand).not.toHaveBeenCalled()
  })

  it('never grants concurrent cancel winners authority to execute a staged command', async () => {
    const firstExecute = vi.fn(async () => true)
    const secondExecute = vi.fn(async () => true)
    const firstCancel = vi.fn(async () => true)
    const secondCancel = vi.fn(async () => true)
    const first = makeOptions({
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      cancelDurablePendingItem: firstCancel,
      executeSlashCommand: firstExecute,
    })
    const second = makeOptions({
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      cancelDurablePendingItem: secondCancel,
      executeSlashCommand: secondExecute,
    })
    const stagedItem = (): ChatPendingItem => ({
      pendingUiId: 'pending-ui-concurrent-registered',
      text: '/reset',
      attachments: [],
      intent: null,
      confirmedPlainText: true,
      pendingInputId: 'pending-concurrent-registered',
      pendingClientRequestId: 'request-concurrent-registered',
      pendingClientMessageId: 'message-concurrent-registered',
      pendingRequestFingerprint: 'sha256:concurrent-registered',
      pendingPersistenceState: 'staged',
    })

    await expect(Promise.all([
      first.api.sendQueuedFollowup(stagedItem()),
      second.api.sendQueuedFollowup(stagedItem()),
    ])).resolves.toEqual(['not_sent', 'not_sent'])

    expect(firstCancel).toHaveBeenCalledOnce()
    expect(secondCancel).toHaveBeenCalledOnce()
    expect(firstExecute).not.toHaveBeenCalled()
    expect(secondExecute).not.toHaveBeenCalled()
  })

  it.each(['unavailable', 'registered'] as const)(
    'keeps a durable queued slash item with attachments when catalog is %s',
    async classification => {
      vi.useFakeTimers()
      try {
        const inputText = ref('')
        const pendingAttachments = ref<Attachment[]>([])
        const pendingSessionIntent = ref<string | null>(null)
        const sessionKey = ref('agent:main:webchat:test')
        const isStreaming = ref(false)
        const pendingRecords = new Map<
          string,
          import('@/utils/chat/pendingInputWal').PendingInputWalRecord
        >()
        let sendApi!: ReturnType<typeof useChatSend>
        const pending = useChatPendingQueue({
          sessionKey,
          inputText,
          pendingAttachments,
          pendingSessionIntent,
          isStreaming,
          isBlocked: () => false,
          autoResizeTextarea: vi.fn(),
          sendCurrentInput: vi.fn(),
          resetInputHistory: vi.fn(),
          hasComposer: () => true,
          pendingInputWal: {
            put: async record => { pendingRecords.set(record.pendingInputId, record) },
            list: async key => [...pendingRecords.values()].filter(record => (
              record.sessionKey === key
            )),
            delete: async pendingInputId => { pendingRecords.delete(pendingInputId) },
            close: () => {},
          },
          supportsMethod: () => false,
          dispatchPendingItem: (item, ownerSessionKey) => (
            sendApi.sendQueuedFollowup(item, ownerSessionKey)
          ),
        })
        const executeSlashCommand = vi.fn(async () => true)
        const classifySlashCommand = vi.fn(async () => classification)
        const configured = makeOptions({
          inputText,
          pendingAttachments,
          pendingSessionIntent,
          sessionKey,
          busySendMode: pending.busySendMode,
          enqueuePendingInput: pending.enqueuePendingInput,
          enqueuePendingPayload: pending.enqueuePendingPayload,
          popAllPendingIntoComposer: pending.popAllPendingIntoComposer,
          classifySlashCommand,
          executeSlashCommand,
        })
        sendApi = configured.api
        const attachment: Attachment = {
          kind: 'staged',
          local_id: 94,
          name: 'slash-context.txt',
          mime: 'text/plain',
          file_uuid: 'file-durable-slash-attachment',
        }
        await pending.enqueuePendingPayload({
          text: '/gamemode creative',
          attachments: [attachment],
        })

        pending.schedulePendingDrainAfterTerminal()
        await vi.advanceTimersByTimeAsync(50)
        await nextTick()

        expect(classifySlashCommand).toHaveBeenCalledOnce()
        expect(executeSlashCommand).not.toHaveBeenCalled()
        expect(configured.rpc.call).not.toHaveBeenCalled()
        expect(pending.pendingQueue.value).toHaveLength(1)
        expect(pending.pendingQueue.value[0]?.deliveryState).toBe('retryable')
        expect(pending.pendingQueue.value[0]?.attachments).toEqual([attachment])
        expect(pendingRecords.size).toBe(1)
        pending.cleanup()
      } finally {
        vi.useRealTimers()
      }
    },
  )

  it('keeps a queued slash item when its workspace becomes unavailable during classification', async () => {
    let resolveClassification!: (classification: 'unknown') => void
    const classification = new Promise<'unknown'>(resolve => {
      resolveClassification = resolve
    })
    let projectBlocker: string | null = null
    const validateActiveProjectBeforeSend = vi.fn(async () => projectBlocker)
    const classifySlashCommand = vi.fn(() => classification)
    const { api, rpc } = makeOptions({
      validateActiveProjectBeforeSend,
      classifySlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-workspace-blocked-slash',
      ownerSessionKey: 'agent:main:webchat:test',
      text: '/gamemode creative',
      attachments: [],
      intent: null,
    }

    const sending = api.sendQueuedFollowup(queued, 'agent:main:webchat:test')
    await vi.waitFor(() => expect(classifySlashCommand).toHaveBeenCalledOnce())
    projectBlocker = 'removed'
    resolveClassification('unknown')

    await expect(sending).resolves.toBe('deferred')
    expect(validateActiveProjectBeforeSend).toHaveBeenCalledTimes(2)
    expect(rpc.call).not.toHaveBeenCalled()
  })

  it('keeps a queued slash item owned by A when classification finishes after switching to B', async () => {
    let resolveClassification!: (classification: 'unknown') => void
    const classification = new Promise<'unknown'>(resolve => {
      resolveClassification = resolve
    })
    const sessionKey = ref('agent:main:webchat:A')
    const classifySlashCommand = vi.fn(() => classification)
    const executeSlashCommand = vi.fn(async () => false)
    const { api, rpc } = makeOptions({
      sessionKey,
      classifySlashCommand,
      executeSlashCommand,
    })
    const queued: ChatPendingItem = {
      pendingUiId: 'pending-ui-session-switch-slash',
      ownerSessionKey: 'agent:main:webchat:A',
      text: '/gamemode creative',
      attachments: [],
      intent: null,
    }

    const sending = api.sendQueuedFollowup(queued, 'agent:main:webchat:A')
    sessionKey.value = 'agent:main:webchat:B'
    resolveClassification('unknown')

    await expect(sending).resolves.toBe('not_sent')
    expect(executeSlashCommand).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
  })
})
