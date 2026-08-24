import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { ChatMessage, ChatPendingItem } from '@/types/chat'
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'
import type {
  PendingInputWal,
  ResponseHandoffWalRecord,
} from '@/utils/chat/pendingInputWal'
import type { UseChatSendOptions } from './useChatSend'
import { useChatSend } from './useChatSend'

function snapshot(annotationId: string, sentOrder: number): PromptAnnotationSnapshot {
  return {
    annotationId,
    documentId: 'document-1',
    documentName: 'page.html',
    revisionId: 'revision-1',
    generation: 1,
    anchorId: `anchor-${annotationId}`,
    body: `Change ${annotationId}`,
    tagName: 'button',
    locator: {},
    quote: '<button>',
    sourceExcerpt: null,
    sentOrder,
  }
}

function createHarness(overrides: Partial<UseChatSendOptions> = {}) {
  const rpc = {
    call: vi.fn().mockResolvedValue({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-1',
      acceptedPromptAnnotationIds: ['annotation-2'],
    }),
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
    appendToolResult: vi.fn(),
    appendArtifact: vi.fn(),
    reconcileFinalText: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(),
    appendFrame: vi.fn(),
    useReducer: ref(false),
  }
  const options: UseChatSendOptions = {
    rpc,
    inputText: ref(''),
    messages: ref<ChatMessage[]>([]),
    sessionKey: ref('agent:main:webchat:test'),
    pendingQueueOwnerContext: ref(null),
    busySendMode: ref('queue'),
    modelRoutingMode: ref('off'),
    modelRoutingSettingsBusy: ref(false),
    elevatedMode: ref(''),
    runMode: ref('safe'),
    pendingAttachments: ref([]),
    pendingSessionIntent: ref(null),
    initialCollaborationMode: ref('default'),
    initialRoutingMode: ref(null),
    pendingForkBeforeMessageId: ref(null),
    promptAnnotationIds: ref(['annotation-2', 'annotation-1']),
    promptAnnotationSnapshots: ids => ids.map((id, index) => snapshot(id, index)),
    acknowledgePromptAnnotations: vi.fn(),
    aborted: ref(false),
    activeStreamTaskId: ref(''),
    activeStreamSessionKey: ref(''),
    autoScroll: ref(false),
    stream,
    normalizeElevatedMode: mode => mode,
    adoptResponseSession: vi.fn(),
    scheduleHistorySync: vi.fn(),
    schedulePendingDrainAfterTerminal: vi.fn(),
    flushDeferredPendingDrain: vi.fn(),
    isCompactInFlightForCurrentSession: () => false,
    hasPendingAttachmentWork: () => false,
    enqueuePendingInput: vi.fn(() => true),
    steerDelivery: {
      attemptForItem: vi.fn(() => null),
      begin: vi.fn(() => null),
      markRetryable: vi.fn(),
      accept: vi.fn(),
      disposition: vi.fn(),
      fallback: vi.fn(),
      reject: vi.fn(),
      acknowledgeAcceptedOffscreen: vi.fn(),
      markStopRequested: vi.fn(),
      reconcileDurableMessages: vi.fn(),
      resetTransientBoundaries: vi.fn(),
    } as UseChatSendOptions['steerDelivery'],
    popAllPendingIntoComposer: vi.fn(() => false),
    classifySlashCommand: vi.fn(async () => 'unknown' as const),
    executeSlashCommand: vi.fn(async () => false),
    closeSlashMenu: vi.fn(),
    autoResizeTextarea: vi.fn(),
    scrollToBottom: vi.fn(),
    ...overrides,
  }
  return { api: useChatSend(options), options, rpc }
}

describe('useChatSend prompt annotations', () => {
  it('shows a pending optimistic annotation card before chat.send is acknowledged', async () => {
    let acceptSend!: (value: Record<string, unknown>) => void
    const harness = createHarness()
    harness.rpc.call.mockImplementationOnce(() => new Promise(resolve => {
      acceptSend = resolve
    }))

    const sending = harness.api.onSend()
    await vi.waitFor(() => expect(harness.rpc.call).toHaveBeenCalledOnce())

    expect(harness.options.messages.value[0]?.promptAnnotations?.map(item => item.annotationId))
      .toEqual(['annotation-2', 'annotation-1'])
    expect(harness.options.acknowledgePromptAnnotations).not.toHaveBeenCalled()

    acceptSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-1',
      acceptedPromptAnnotationIds: ['annotation-2'],
    })
    await sending

    expect(harness.options.messages.value[0]?.promptAnnotations?.map(item => item.annotationId))
      .toEqual(['annotation-2'])
  })

  it('sends an ordered batch and clears only IDs proven accepted by the Gateway', async () => {
    const harness = createHarness()

    await harness.api.onSend()

    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: i18n.global.t('chat.promptAnnotations.applyPrompt'),
      displayText: '',
      promptAnnotationIds: ['annotation-2', 'annotation-1'],
    }))
    expect(harness.options.messages.value[0]).toMatchObject({
      role: 'user',
      text: '',
      promptAnnotations: [
        { annotationId: 'annotation-2', sentOrder: 0 },
      ],
    })
    expect(harness.options.acknowledgePromptAnnotations).toHaveBeenCalledWith(
      ['annotation-2', 'annotation-1'],
      ['annotation-2'],
      'agent:main:webchat:test',
    )
  })

  it('publishes both request and canonical session keys after draft materialization', async () => {
    const harness = createHarness()
    harness.rpc.call.mockResolvedValueOnce({
      sessionKey: 'agent:main:webchat:canonical',
      task_id: 'task-1',
      acceptedPromptAnnotationIds: ['annotation-2'],
    })

    await harness.api.onSend()

    expect(harness.options.acknowledgePromptAnnotations).toHaveBeenCalledWith(
      ['annotation-2', 'annotation-1'],
      ['annotation-2'],
      'agent:main:webchat:canonical',
      'agent:main:webchat:test',
    )
  })

  it('does not infer acceptance when an older response omits accepted IDs', async () => {
    const harness = createHarness()
    harness.rpc.call.mockResolvedValue({ sessionKey: 'agent:main:webchat:test', task_id: 'task-1' })

    await harness.api.onSend()

    expect(harness.options.acknowledgePromptAnnotations).not.toHaveBeenCalled()
    expect(harness.options.messages.value[0]?.promptAnnotations?.map(item => item.annotationId))
      .toEqual(['annotation-2', 'annotation-1'])
  })

  it('keeps a stale batch completely local while send is blocked', async () => {
    const harness = createHarness({ sendBlockedReason: ref('Reselect stale annotations.') })

    await harness.api.onSend()

    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toEqual([])
  })

  it('waits for annotation autosaves before capturing the optimistic snapshot', async () => {
    let releaseAutosave!: () => void
    const autosaveGate = new Promise<void>(resolve => {
      releaseAutosave = resolve
    })
    let savedBody = 'Unsaved body'
    const preparePromptAnnotationsForSend = vi.fn(async () => {
      await autosaveGate
      savedBody = 'Body accepted by the annotation service'
      return true
    })
    const harness = createHarness({
      preparePromptAnnotationsForSend,
      promptAnnotationSnapshots: ids => ids.map((id, index) => ({
        ...snapshot(id, index),
        body: savedBody,
      })),
    })

    const pendingSend = harness.api.onSend()
    await vi.waitFor(() => {
      expect(preparePromptAnnotationsForSend).toHaveBeenCalledWith(
        ['annotation-2', 'annotation-1'],
        expect.objectContaining({ isCurrent: expect.any(Function) }),
      )
    })
    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toEqual([])

    releaseAutosave()
    await pendingSend

    expect(harness.options.messages.value[0]?.promptAnnotations).toEqual([
      expect.objectContaining({
        annotationId: 'annotation-2',
        body: 'Body accepted by the annotation service',
      }),
    ])
  })

  it.each([
    Object.assign(new Error('definitive rejection'), { accepted: false }),
    Object.assign(new Error('response lost'), { code: 'RPC_RESPONSE_UNKNOWN' }),
  ])('keeps annotation drafts out of history and unacknowledged on failure', async (error) => {
    const harness = createHarness()
    harness.rpc.call.mockRejectedValue(error)

    await harness.api.onSend()

    expect(harness.options.acknowledgePromptAnnotations).not.toHaveBeenCalled()
    expect(harness.options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
    expect(harness.options.messages.value.find(message => message.role === 'user')?.promptAnnotations)
      .toBeUndefined()
  })

  it.each([
    'PERMISSION_DENIED',
    'INVALID_REQUEST',
    'INTERNAL_ERROR',
    'UNEXPECTED_ARTIFACT_FAILURE',
  ])('does not expose raw %s failures for an annotation send', async (code) => {
    const privateMessage = `private revision/receipt diagnostic for ${code}`
    const harness = createHarness()
    harness.rpc.call.mockRejectedValue(Object.assign(new Error(privateMessage), {
      code,
      accepted: false,
      retryable: false,
    }))

    await harness.api.onSend()

    const renderedMessages = JSON.stringify(harness.options.messages.value)
    expect(renderedMessages).not.toContain(privateMessage)
    expect(renderedMessages).not.toContain('revision/receipt')
    expect(harness.options.messages.value.some(message => message.role === 'error')).toBe(true)
  })

  it('preserves the existing raw fallback for an unrelated ordinary chat error', async () => {
    const harness = createHarness({
      inputText: ref('hello'),
      promptAnnotationIds: ref([]),
      promptAnnotationSnapshots: () => [],
    })
    harness.rpc.call.mockRejectedValue(Object.assign(new Error('ordinary provider detail'), {
      code: 'PROVIDER_REJECTED',
      accepted: false,
      retryable: false,
    }))

    await harness.api.onSend()

    expect(JSON.stringify(harness.options.messages.value)).toContain('ordinary provider detail')
  })

  it('keeps raw errors out of annotation handoff recovery toasts', async () => {
    const params = {
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'annotation-handoff-request',
      clientMessageId: 'annotation-handoff-message',
      message: 'Apply annotations',
      promptAnnotationIds: ['annotation-2'],
    }
    const record: ResponseHandoffWalRecord = {
      schemaVersion: 1,
      ownerRequestId: params.clientRequestId,
      requestSessionKey: params.sessionKey,
      clientRequestId: params.clientRequestId,
      clientMessageId: params.clientMessageId,
      composerText: '',
      recoveryAttachments: [],
      params,
      state: 'submitting',
      createdAt: 1,
      updatedAt: 1,
    }
    const pendingInputWal: PendingInputWal = {
      put: async () => {},
      list: async () => [],
      delete: async () => {},
      listHandoffs: async () => [record],
      putHandoff: async () => {},
      close: () => {},
    }
    const harness = createHarness({ pendingInputWal })
    const privateMessage = 'private receipt and revision from replay'
    harness.rpc.call.mockRejectedValue(Object.assign(new Error(privateMessage), {
      code: 'INTERNAL_ERROR',
      accepted: false,
      retryable: false,
    }))
    const { toasts, dismissToast } = useToasts()
    for (const toast of [...toasts.value]) dismissToast(toast.id)

    await harness.api.recoverResponseHandoffs()

    expect(toasts.value).toHaveLength(1)
    expect(toasts.value[0]?.message).not.toContain(privateMessage)
    expect(toasts.value[0]?.message).not.toContain('receipt')
    dismissToast(toasts.value[0]!.id)
  })

  it('replays unknown acceptance before a newly-stale local annotation gate', async () => {
    const localSendGate = ref<string | null>(null)
    const liveReplayGate = ref<string | null>(null)
    const promptAnnotationIds = ref<readonly string[]>(['annotation-2', 'annotation-1'])
    const preparePromptAnnotationsForSend = vi.fn(async () => true)
    const harness = createHarness({
      promptAnnotationIds,
      sendBlockedReason: localSendGate,
      idempotentReplayBlockedReason: liveReplayGate,
      preparePromptAnnotationsForSend,
    })
    const lostResponse = Object.assign(new Error('response lost'), {
      code: 'RPC_RESPONSE_UNKNOWN',
    })
    harness.rpc.call
      .mockRejectedValueOnce(lostResponse)
      .mockResolvedValueOnce({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-1',
        acceptedPromptAnnotationIds: ['annotation-2', 'annotation-1'],
      })

    await harness.api.onSend()
    const firstParams = harness.rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toBeDefined()
    expect(preparePromptAnnotationsForSend).toHaveBeenCalledTimes(1)

    // Simulate the server having consumed the drafts and advanced the head
    // while the first response was lost. These local facts must not suppress
    // replay of the already-fingerprinted request.
    promptAnnotationIds.value = []
    localSendGate.value = 'Reselect stale annotations.'
    liveReplayGate.value = 'Gateway disconnected.'

    await harness.api.onSend()
    expect(harness.rpc.call).toHaveBeenCalledTimes(1)

    liveReplayGate.value = null
    await harness.api.onSend()

    expect(harness.rpc.call).toHaveBeenCalledTimes(2)
    expect(harness.rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(preparePromptAnnotationsForSend).toHaveBeenCalledTimes(1)
    expect(harness.options.acknowledgePromptAnnotations).toHaveBeenCalledWith(
      ['annotation-2', 'annotation-1'],
      ['annotation-2', 'annotation-1'],
      'agent:main:webchat:test',
    )
    const userMessages = harness.options.messages.value.filter(message => message.role === 'user')
    expect(userMessages).toHaveLength(1)
    expect(userMessages[0]?.promptAnnotations?.map(item => item.annotationId))
      .toEqual(['annotation-2', 'annotation-1'])
  })

  it('acknowledges accepted annotations exactly once after receipt recovery', async () => {
    vi.useFakeTimers()
    try {
      let rejectFirstSend!: (reason: unknown) => void
      let sendCalls = 0
      const acknowledgePromptAnnotations = vi.fn()
      const harness = createHarness({ acknowledgePromptAnnotations })
      harness.options.stream.startStreaming = vi.fn(() => {
        harness.options.stream.isStreaming.value = true
      })
      harness.options.stream.endStreaming = vi.fn(() => {
        harness.options.stream.isStreaming.value = false
      })
      harness.rpc.call.mockImplementation(<T = unknown>(
        method: string,
      ): Promise<T> => {
        if (method === 'chat.abort') {
          return Promise.resolve({ aborted: true }) as Promise<T>
        }
        sendCalls += 1
        if (sendCalls === 1) {
          return new Promise<T>((_resolve, reject) => {
            rejectFirstSend = reject
          })
        }
        return Promise.resolve({
          sessionKey: 'agent:main:webchat:test',
          task_id: 'task-recovered-annotation-send',
          task_status: 'running',
          acceptedPromptAnnotationIds: ['annotation-2'],
        }) as Promise<T>
      })

      const firstSend = harness.api.onSend()
      await Promise.resolve()
      expect(sendCalls).toBe(1)
      harness.api.onStop()
      rejectFirstSend(Object.assign(new Error('response lost'), { retryable: true }))
      await firstSend

      expect(acknowledgePromptAnnotations).not.toHaveBeenCalled()
      await vi.runAllTimersAsync()
      await Promise.resolve()

      expect(sendCalls).toBe(2)
      expect(acknowledgePromptAnnotations).toHaveBeenCalledTimes(1)
      expect(acknowledgePromptAnnotations).toHaveBeenCalledWith(
        ['annotation-2', 'annotation-1'],
        ['annotation-2'],
        'agent:main:webchat:test',
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('retains annotation ids when a queued follow-up drains after the first turn', async () => {
    const harness = createHarness({ promptAnnotationIds: ref([]) })
    const item: ChatPendingItem = {
      pendingUiId: 'pending-annotation-follow-up',
      ownerSessionKey: 'agent:main:webchat:test',
      text: 'Apply the second selected edit.',
      promptAnnotationIds: ['annotation-2', 'annotation-1'],
      attachments: [],
      intent: null,
    }

    await expect(harness.api.sendQueuedFollowup(item)).resolves.toBe('accepted')
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      promptAnnotationIds: ['annotation-2', 'annotation-1'],
    }))
  })
})
