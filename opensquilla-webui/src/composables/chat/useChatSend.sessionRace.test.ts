import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { ChatMessage, ChatPendingItem } from '@/types/chat'
import { TurnCommandError, type TurnSendRequest, type TurnSendResponse, type TurnReceiptResult } from '@/modules/turnCommands'
import { createPendingQueuePolicy } from '@/utils/chat/pendingQueuePolicy'
import { decideSendResponseSession, type UseChatSendOptions } from './useChatSend'
import { createChatSendHarness, memoryDeliveryWal } from './chatSendTestHarness'
import { useChatSteerDelivery } from './useChatSteerDelivery'

vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))

function queuedSendHarness(overrides: Partial<UseChatSendOptions> = {}) {
  const messages = ref<ChatMessage[]>([])
  const stream: UseChatSendOptions['stream'] = {
    isStreaming: ref(false), streamBubble: ref(false), streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(), endStreaming: vi.fn(), checkpointForUserMessage: vi.fn(),
    appendDelta: vi.fn(), scheduleRender: vi.fn(), appendToolCall: vi.fn(),
    appendToolDelta: vi.fn(), appendToolEnd: vi.fn(), appendToolResult: vi.fn(),
    appendArtifact: vi.fn(), reconcileFinalText: vi.fn(), resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(), setStreamActivity: vi.fn(), showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(), appendFrame: vi.fn(),
  }
  const commands = {
    send: vi.fn(async (_request: TurnSendRequest): Promise<TurnSendResponse> => ({ taskId: 'synthetic-next-task' })),
    steer: vi.fn(async () => ({ accepted: true })),
    cancel: vi.fn(async () => ({ aborted: true })),
    supports: () => true,
    lookupReceipt: vi.fn(async (): Promise<TurnReceiptResult> => ({ status: 'not-found' })),
    supportsReceiptLookup: () => true,
  }
  const scope = { sessionKey: 'agent:main:webchat:test', deliveryIdentity: 'synthetic-identity' }
  const policy = createPendingQueuePolicy(null)
  const pausePendingAutoSend = vi.fn(() => { policy.pause(scope) })
  const options: Omit<UseChatSendOptions, 'durableDelivery'> = {
    turnCommands: commands, inputText: ref('unrelated composer draft'), messages,
    sessionKey: ref(scope.sessionKey), deliveryIdentity: ref(scope.deliveryIdentity),
    pendingQueueOwnerContext: ref(null), busySendMode: ref('queue'), modelRoutingMode: ref('off'),
    modelRoutingSettingsBusy: ref(false), elevatedMode: ref(''), runMode: ref('safe'),
    pendingAttachments: ref([]), pendingSessionIntent: ref(null), initialCollaborationMode: ref('default'),
    initialRoutingMode: ref(null), pendingForkBeforeMessageId: ref(null), aborted: ref(false),
    activeStreamTaskId: ref(''), activeStreamSessionKey: ref(''), autoScroll: ref(false), stream,
    normalizeElevatedMode: mode => mode, adoptResponseSession: vi.fn(), scheduleHistorySync: vi.fn(),
    schedulePendingDrainAfterTerminal: vi.fn(), flushDeferredPendingDrain: vi.fn(),
    isCompactInFlightForCurrentSession: () => false, hasPendingAttachmentWork: () => false,
    enqueuePendingInput: vi.fn(() => true), popAllPendingIntoComposer: vi.fn(() => false),
    steerDelivery: useChatSteerDelivery({ messages, pendingQueue: ref([]),
      checkpointForUserMessage: stream.checkpointForUserMessage, scheduleHistorySync: vi.fn() }),
    classifySlashCommand: vi.fn(async () => 'unknown' as const), executeSlashCommand: vi.fn(async () => false),
    closeSlashMenu: vi.fn(), autoResizeTextarea: vi.fn(), scrollToBottom: vi.fn(),
    canStop: () => true, pausePendingAutoSend,
    ...overrides,
  }
  return { ...createChatSendHarness(options), commands, policy, scope, pausePendingAutoSend }
}

describe('decideSendResponseSession', () => {
  it('ignores a late response after the user navigated to another session', () => {
    expect(decideSendResponseSession({
      requestSessionKey: 'agent:main:webchat:A',
      currentSessionKey: 'agent:main:webchat:B',
      responseSessionKey: 'agent:main:webchat:A',
    })).toEqual({
      action: 'ignore',
      reason: 'current_session_changed',
    })
  })

  it('persists server canonicalization while the request session is still current', () => {
    expect(decideSendResponseSession({
      requestSessionKey: 'legacy-A',
      currentSessionKey: 'legacy-A',
      responseSessionKey: 'agent:main:webchat:legacy-A',
    })).toEqual({
      action: 'persist',
      responseSessionKey: 'agent:main:webchat:legacy-A',
    })
  })

  it('ignores same-session responses', () => {
    expect(decideSendResponseSession({
      requestSessionKey: 'agent:main:webchat:A',
      currentSessionKey: 'agent:main:webchat:A',
      responseSessionKey: 'agent:main:webchat:A',
    })).toEqual({
      action: 'ignore',
      reason: 'same_session',
    })
  })

  it('ignores responses with no session key', () => {
    expect(decideSendResponseSession({
      requestSessionKey: 'agent:main:webchat:A',
      currentSessionKey: 'agent:main:webchat:A',
      responseSessionKey: undefined,
    })).toEqual({
      action: 'ignore',
      reason: 'missing_response_session',
    })
  })
})

describe('explicit composer activity restores queue sending after acceptance', () => {
  it.each([false, true])('only explicit resumeQueueOnSuccess=%s can resume after admission', async manual => {
    const resume = vi.fn()
    const capture = vi.fn(() => resume)
    const h = queuedSendHarness({ capturePendingAutoSendResume: capture })
    await h.api.onSend(manual ? { resumeQueueOnSuccess: true } : {})
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(capture).toHaveBeenCalledTimes(manual ? 1 : 0)
    expect(resume).toHaveBeenCalledTimes(manual ? 1 : 0)
  })

  it('captures permission before project validation and cannot replace a newer Stop on success', async () => {
    let generation = 1
    const resumed: number[] = []
    const capture = vi.fn(() => {
      const captured = generation
      return () => { if (generation === captured) resumed.push(captured) }
    })
    let validate!: () => void
    const h = queuedSendHarness({ capturePendingAutoSendResume: capture,
      validateActiveProjectBeforeSend: () => new Promise<null>(resolve => { validate = () => resolve(null) }),
    })
    const sending = h.api.onSend({ resumeQueueOnSuccess: true })
    expect(capture).toHaveBeenCalledOnce()
    generation += 1
    validate()
    await sending
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(capture).toHaveBeenCalledOnce()
    expect(resumed).toEqual([])
  })

  it('does not resume on rejection and captures new permission for an explicit rejected retry', async () => {
    const first = vi.fn()
    const retry = vi.fn()
    const capture = vi.fn().mockReturnValueOnce(first).mockReturnValueOnce(retry)
    const h = queuedSendHarness({ capturePendingAutoSendResume: capture })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('rejected', 'synthetic refusal', 'REFUSED', false, true))
    await h.api.onSend({ resumeQueueOnSuccess: true })
    expect(first).not.toHaveBeenCalled()
    const originalId = h.commands.send.mock.calls[0]?.[0].params.clientRequestId
    await h.api.onSend({ resumeQueueOnSuccess: true })
    expect(first).not.toHaveBeenCalled()
    expect(retry).toHaveBeenCalledOnce()
    expect(h.commands.send.mock.calls[1]?.[0].params.clientRequestId).toBe(originalId)
  })

  it.each([false, true])('unknown admission resumes only after its original receipt, newerStop=%s', async newerStop => {
    let generation = 1
    const resume = vi.fn()
    const capture = vi.fn(() => {
      const captured = generation
      return () => { if (captured === generation) resume() }
    })
    let releaseReceipt!: (value: TurnReceiptResult) => void
    const receipt = new Promise<TurnReceiptResult>(resolve => { releaseReceipt = resolve })
    const h = queuedSendHarness({ capturePendingAutoSendResume: capture })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'synthetic ACK loss', undefined, null))
    h.commands.lookupReceipt.mockImplementation(() => receipt)
    await h.api.onSend({ resumeQueueOnSuccess: true })
    await vi.waitFor(() => expect(h.commands.lookupReceipt).toHaveBeenCalledOnce())
    expect(resume).not.toHaveBeenCalled()
    if (newerStop) {
      generation += 1
      await h.api.onSend({ resumeQueueOnSuccess: true })
    }
    releaseReceipt({ status: 'found', response: { taskId: 'original-task' } })
    await h.options.durableDelivery.wake()
    expect(resume).toHaveBeenCalledTimes(newerStop ? 0 : 1)
    expect(capture).toHaveBeenCalledTimes(newerStop ? 2 : 1)
    expect(h.commands.send).toHaveBeenCalledOnce()
  })

  it.each(['busy', 'offline'] as const)('resumes a successful %s Queue only after persistence returns true', async mode => {
    let save!: (saved: boolean) => void
    const enqueue = vi.fn(() => new Promise<boolean>(resolve => { save = resolve }))
    const resume = vi.fn()
    const capture = vi.fn(() => resume)
    const h = queuedSendHarness({ enqueuePendingInput: enqueue, capturePendingAutoSendResume: capture,
      ...(mode === 'offline' ? { offlineQueueIdentity: ref('synthetic-identity') } : {}),
    })
    h.options.stream.isStreaming.value = mode === 'busy'
    const sending = h.api.onSend({ resumeQueueOnSuccess: true })
    await vi.waitFor(() => expect(enqueue).toHaveBeenCalledOnce())
    expect(capture).toHaveBeenCalledOnce()
    expect(resume).not.toHaveBeenCalled()
    save(true)
    await sending
    expect(resume).toHaveBeenCalledOnce()
    expect(h.commands.send).not.toHaveBeenCalled()
  })

  it.each(['busy', 'offline'] as const)('keeps the queue paused after a failed %s enqueue', async mode => {
    const resume = vi.fn()
    const h = queuedSendHarness({ enqueuePendingInput: vi.fn(async () => false),
      capturePendingAutoSendResume: () => resume,
      ...(mode === 'offline' ? { offlineQueueIdentity: ref('synthetic-identity') } : {}),
    })
    h.options.stream.isStreaming.value = mode === 'busy'
    await h.api.onSend({ resumeQueueOnSuccess: true })
    expect(resume).not.toHaveBeenCalled()
    expect(h.commands.send).not.toHaveBeenCalled()
  })

  it('does not grant queue resume to an accepted single-card follow-up', async () => {
    const capture = vi.fn(() => vi.fn())
    const h = queuedSendHarness({ capturePendingAutoSendResume: capture })
    await expect(h.api.sendQueuedFollowup({ pendingUiId: 'C', text: 'one card', attachments: [], intent: null }, h.scope.sessionKey))
      .resolves.toBe('accepted')
    expect(capture).not.toHaveBeenCalled()
  })

  it('does not resume the queue after an accepted composer Steer', async () => {
    const resume = vi.fn()
    const h = queuedSendHarness({ capturePendingAutoSendResume: () => resume,
      busySendMode: ref('steer'), activeStreamTaskId: ref('running-task'),
      activeSteerCapability: ref({ mode: 'same_turn', expected_turn_id: 'running-task', input_kinds: ['text'] }),
      enqueuePendingSteerAttempt: payload => ({ pendingUiId: 'steer-C', text: payload.request.message,
        attachments: [], intent: null, steerAttempt: { request: payload.request, phase: payload.phase || 'submitting' } }),
    })
    h.options.stream.isStreaming.value = true
    await h.api.onSend({ resumeQueueOnSuccess: true })
    expect(h.commands.steer).toHaveBeenCalledOnce()
    expect(h.commands.send).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled()
  })

  it('resumes before a fork transfers its response-owned queue to the child', async () => {
    const order: string[] = []
    const h = queuedSendHarness({ pendingForkBeforeMessageId: ref('fork-anchor'),
      capturePendingAutoSendResume: () => () => { order.push('resume') },
      adoptResponseSession: vi.fn(async () => { order.push('adopt') }),
    })
    h.commands.send.mockResolvedValueOnce({ taskId: 'fork-task', sessionKey: 'child-session' })
    await h.api.onSend({ resumeQueueOnSuccess: true })
    expect(order).toEqual(['resume', 'adopt'])
  })

  it('resumes for an error that explicitly proves admission', async () => {
    const resume = vi.fn()
    const h = queuedSendHarness({ capturePendingAutoSendResume: () => resume })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('queue-capacity', 'already admitted', 'QUEUE_FULL_DIRTY', true, false))
    await h.api.onSend({ resumeQueueOnSuccess: true })
    expect(resume).toHaveBeenCalledOnce()
  })
})

describe('Stop while a queued follow-up is preparing', () => {
  it('keeps a staged request identity retryable when saving the revoked dispatch fails', async () => {
    const wal = memoryDeliveryWal()
    const compare = wal.compareAndSwapDelivery!
    const harness = queuedSendHarness({ pendingInputWal: wal })
    const policy = createPendingQueuePolicy(null)
    const permit = policy.capture(harness.scope)
    let paused = false
    let failNotSent = true
    wal.compareAndSwapDelivery = async (id, revision, record) => {
      if (record?.phase === 'submitting' && !paused) { paused = true; policy.pause(harness.scope) }
      if (record?.phase === 'not-sent' && failNotSent) throw new Error('synthetic revoke storage failure')
      return compare(id, revision, record)
    }
    const item: ChatPendingItem = {
      pendingUiId: 'C', text: 'saved staged follow-up', attachments: [], intent: null,
      ownerSessionKey: harness.scope.sessionKey, pendingInputId: 'pending-C',
      pendingPersistenceState: 'staged', pendingClientRequestId: 'request-C',
      pendingClientMessageId: 'message-C', pendingRequestFingerprint: 'fingerprint-C',
    }
    await expect(harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => policy.allows(permit))).resolves.toBe('retryable_failure')
    expect(harness.commands.send).not.toHaveBeenCalled()
    await harness.options.durableDelivery.wake()
    expect((await wal.getDelivery!('request-C'))?.phase).toBe('submitting')
    failNotSent = false
    await harness.options.durableDelivery.wake()
    expect((await wal.getDelivery!('request-C'))?.phase).toBe('not-sent')
    const selected = policy.capture(harness.scope, true)
    await expect(harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => policy.allows(selected))).resolves.toBe('accepted')
    expect(harness.commands.send).toHaveBeenCalledExactlyOnceWith({ kind: 'pending-input', params: {
      key: harness.scope.sessionKey, pendingInputId: 'pending-C',
      clientRequestId: 'request-C', requestFingerprint: 'fingerprint-C',
    } }, expect.anything())
    expect((await wal.listDeliveries!()).map(record => record.ownerRequestId)).toEqual(['request-C'])
    expect(policy.read(harness.scope).paused).toBe(true)
  })

  it.each(['prepare', 'arm'] as const)('retains a same-window stopped staged draft while WAL %s is waiting', async stage => {
    const wal = memoryDeliveryWal()
    const prepare = wal.prepareDelivery!
    const compare = wal.compareAndSwapDelivery!
    let reached = false
    let release!: () => void
    const barrier = new Promise<void>(resolve => { release = resolve })
    wal.prepareDelivery = async (record, owner) => {
      if (stage === 'prepare' && !reached) { reached = true; await barrier }
      return prepare(record, owner)
    }
    wal.compareAndSwapDelivery = async (id, revision, record) => {
      const result = await compare(id, revision, record)
      if (stage === 'arm' && record?.phase === 'submitting' && result.applied && !reached) {
        reached = true
        await barrier
      }
      return result
    }
    const cancelDurablePendingItem = vi.fn(async (item: ChatPendingItem) => {
      item.pendingRetainAfterCancel = true
      return true
    })
    const harness = queuedSendHarness({ pendingInputWal: wal, cancelDurablePendingItem })
    const item: ChatPendingItem = {
      pendingUiId: 'C', text: 'saved staged follow-up', attachments: [], intent: null,
      ownerSessionKey: harness.scope.sessionKey, pendingInputId: 'pending-C',
      pendingPersistenceState: 'staged', pendingClientRequestId: 'request-C',
      pendingClientMessageId: 'message-C', pendingRequestFingerprint: 'fingerprint-C',
    }
    const permit = harness.policy.capture(harness.scope)
    const sending = harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => harness.policy.allows(permit))
    await vi.waitFor(() => expect(reached).toBe(true))
    harness.api.onStop()
    await harness.options.durableDelivery.wake()
    release()
    await expect(sending).resolves.toBe('not_sent')
    expect(harness.commands.send).not.toHaveBeenCalled()
    expect(cancelDurablePendingItem).toHaveBeenCalledExactlyOnceWith(item, { retainAfterCancel: true })
    expect(item.pendingRetainAfterCancel).toBe(true)
    expect((await wal.getDelivery!('request-C'))).toMatchObject({ phase: 'not-sent', stop: { completed: true } })
    expect(harness.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
  })

  it.each(['local', 'staged'] as const)('returns an unsent %s follow-up to the queue after a peer Stop during durable preparation', async kind => {
    const wal = memoryDeliveryWal()
    const prepare = wal.prepareDelivery!
    let reached = false
    let release!: () => void
    const barrier = new Promise<void>(resolve => { release = resolve })
    wal.prepareDelivery = async (record, owner) => {
      if (!reached) { reached = true; await barrier }
      return prepare(record, owner)
    }
    const harness = queuedSendHarness({ pendingInputWal: wal })
    const item: ChatPendingItem = {
      pendingUiId: 'C', text: 'saved follow-up', attachments: [], intent: null,
      ownerSessionKey: harness.scope.sessionKey,
      ...(kind === 'staged' ? {
        pendingInputId: 'pending-C', pendingPersistenceState: 'staged' as const,
        pendingClientRequestId: 'request-C', pendingClientMessageId: 'message-C',
        pendingRequestFingerprint: 'fingerprint-C',
      } : {}),
    }
    const before = structuredClone(item)
    const values = new Map<string, string>()
    const storage = { getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) } }
    const localPolicy = createPendingQueuePolicy(storage)
    const peerPolicy = createPendingQueuePolicy(storage)
    const permit = localPolicy.capture(harness.scope)
    const sending = harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => localPolicy.allows(permit))
    await vi.waitFor(() => expect(reached).toBe(true))
    expect(harness.commands.send).not.toHaveBeenCalled()
    peerPolicy.pause(harness.scope)
    release()
    await expect(sending).resolves.toBe('deferred')
    expect(harness.commands.send).not.toHaveBeenCalled()
    expect(item).toEqual(before)
    expect(harness.options.messages.value).toEqual([])
    expect(harness.options.inputText.value).toBe('unrelated composer draft')
    expect(harness.options.activeStreamTaskId.value).toBe('')
    await harness.options.durableDelivery.wake()
    expect(harness.commands.send).not.toHaveBeenCalled()
    // Sending this one item is allowed while the rest of the queue remains paused.
    const singlePermit = localPolicy.capture(harness.scope, true)
    await expect(harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => localPolicy.allows(singlePermit))).resolves.toBe('accepted')
    expect(harness.commands.send).toHaveBeenCalledOnce()
    expect(harness.options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
    expect(localPolicy.read(harness.scope).paused).toBe(true)
    if (kind === 'staged') expect(harness.commands.send.mock.calls[0]?.[0]).toEqual({
      kind: 'pending-input', params: { key: harness.scope.sessionKey, pendingInputId: 'pending-C',
        clientRequestId: 'request-C', requestFingerprint: 'fingerprint-C' },
    })
  })

  it('revokes an automatic follow-up during asynchronous project validation', async () => {
    let finish!: () => void
    const validateActiveProjectBeforeSend = vi.fn(() => new Promise<null>(resolve => {
      finish = () => resolve(null)
    }))
    const harness = queuedSendHarness({ validateActiveProjectBeforeSend })
    const item: ChatPendingItem = {
      pendingUiId: 'synthetic-queued-item', text: 'saved follow-up', attachments: [], intent: null,
      ownerSessionKey: harness.scope.sessionKey,
    }
    const permit = harness.policy.capture(harness.scope)
    const sending = harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => harness.policy.allows(permit))
    expect(validateActiveProjectBeforeSend).toHaveBeenCalledOnce()
    harness.api.onStop()
    expect(harness.pausePendingAutoSend).toHaveBeenCalledOnce()
    expect(harness.policy.allows(permit)).toBe(false)
    finish()
    await expect(sending).resolves.toBe('deferred')
    expect(harness.commands.send).not.toHaveBeenCalled()
    expect(item.text).toBe('saved follow-up')
    expect(harness.options.inputText.value).toBe('unrelated composer draft')
    await vi.waitFor(() => expect(harness.commands.cancel).toHaveBeenCalledOnce())
  })

  it('revokes an explicit single-item permit while attachment preparation is awaiting', async () => {
    let finish!: () => void
    const prepareAttachmentsForSend = vi.fn(() => new Promise<boolean>(resolve => {
      finish = () => resolve(true)
    }))
    const harness = queuedSendHarness({ prepareAttachmentsForSend })
    harness.policy.pause(harness.scope)
    const item: ChatPendingItem = {
      pendingUiId: 'synthetic-queued-item', text: 'saved attachment', intent: null,
      ownerSessionKey: harness.scope.sessionKey,
      attachments: [{ kind: 'staged', local_id: 1, name: 'fixture.pdf', mime: 'application/pdf',
        file_uuid: 'synthetic-ready-file' }],
    }
    const before = structuredClone(item)
    const permit = harness.policy.capture(harness.scope, true)
    const sending = harness.api.sendQueuedFollowup(item, harness.scope.sessionKey,
      () => harness.policy.allows(permit))
    await vi.waitFor(() => expect(prepareAttachmentsForSend).toHaveBeenCalledOnce())
    expect(harness.policy.allows(permit)).toBe(true)
    harness.api.onStop()
    finish()
    await expect(sending).resolves.toBe('not_sent')
    expect(harness.commands.send).not.toHaveBeenCalled()
    expect(harness.policy.read(harness.scope).paused).toBe(true)
    expect(item).toEqual(before)
    expect(harness.options.inputText.value).toBe('unrelated composer draft')
    expect(harness.options.messages.value).toEqual([])
  })
})
