import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/types/chat'
import type { ChatDocumentContext } from '@/types/rpc'
import type { UseChatSendOptions } from './useChatSend'
import { useChatSend } from './useChatSend'

const SESSION_KEY = 'agent:main:webchat:document-context'

function createHarness(overrides: Partial<UseChatSendOptions> = {}) {
  const rpc = {
    call: vi.fn().mockResolvedValue({
      sessionKey: SESSION_KEY,
      task_id: 'task-1',
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
    inputText: ref('Update the title and accent color.'),
    messages: ref<ChatMessage[]>([]),
    sessionKey: ref(SESSION_KEY),
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

describe('useChatSend document context', () => {
  it('flushes before optimistic mutation and sends the resulting exact head', async () => {
    let releaseFlush!: () => void
    const flushGate = new Promise<void>(resolve => { releaseFlush = resolve })
    let current: ChatDocumentContext = {
      documentId: 'document-1',
      headRevisionId: 'revision-1',
    }
    const prepareDocumentContextForSend = vi.fn(async () => {
      await flushGate
      current = { documentId: 'document-1', headRevisionId: 'revision-2' }
      return current
    })
    const harness = createHarness({
      currentDocumentContext: () => current,
      prepareDocumentContextForSend,
    })

    const sending = harness.api.onSend()
    await vi.waitFor(() => expect(prepareDocumentContextForSend).toHaveBeenCalledWith(
      SESSION_KEY,
      expect.objectContaining({ isCurrent: expect.any(Function) }),
    ))

    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toEqual([])
    expect(harness.options.inputText.value).toBe('Update the title and accent color.')

    releaseFlush()
    await sending

    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      documentContext: {
        documentId: 'document-1',
        headRevisionId: 'revision-2',
      },
    }))
    expect(harness.options.messages.value[0]).toMatchObject({
      role: 'user',
      text: 'Update the title and accent color.',
    })
    expect(harness.options.inputText.value).toBe('')
  })

  it.each([
    ['false result', vi.fn(async () => false as const)],
    ['rejection', vi.fn(async () => { throw new Error('save failed') })],
  ])('keeps the composer intact when editor preparation ends in %s', async (_, prepare) => {
    const current = { documentId: 'document-1', headRevisionId: 'revision-1' }
    const harness = createHarness({
      currentDocumentContext: () => current,
      prepareDocumentContextForSend: prepare,
    })

    await harness.api.onSend()

    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toEqual([])
    expect(harness.options.inputText.value).toBe('Update the title and accent color.')
    expect(harness.options.pendingAttachments.value).toEqual([])
  })

  it('replays an unknown acceptance with the immutable original context and no second flush', async () => {
    let current: ChatDocumentContext = {
      documentId: 'document-1',
      headRevisionId: 'revision-1',
    }
    const prepareDocumentContextForSend = vi.fn(async () => {
      current = { documentId: 'document-1', headRevisionId: 'revision-2' }
      return current
    })
    const harness = createHarness({
      currentDocumentContext: () => current,
      prepareDocumentContextForSend,
    })
    harness.rpc.call
      .mockRejectedValueOnce(Object.assign(new Error('response lost'), {
        code: 'RPC_RESPONSE_UNKNOWN',
      }))
      .mockResolvedValueOnce({ sessionKey: SESSION_KEY, task_id: 'task-1' })

    await harness.api.onSend()
    const firstParams = structuredClone(harness.rpc.call.mock.calls[0]?.[1])
    expect(firstParams).toMatchObject({
      documentContext: {
        documentId: 'document-1',
        headRevisionId: 'revision-2',
      },
    })

    // Changing the currently open document cannot change an already
    // fingerprinted request whose acceptance is unknown.
    current = { documentId: 'document-2', headRevisionId: 'revision-9' }
    await harness.api.onSend()

    expect(harness.rpc.call).toHaveBeenCalledTimes(2)
    expect(harness.rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(prepareDocumentContextForSend).toHaveBeenCalledTimes(1)
  })

  it('does not reuse a rejected fingerprint after the active document changes', async () => {
    let current: ChatDocumentContext = {
      documentId: 'document-1',
      headRevisionId: 'revision-1',
    }
    const prepareDocumentContextForSend = vi.fn(async () => current)
    const harness = createHarness({
      currentDocumentContext: () => current,
      prepareDocumentContextForSend,
    })
    harness.rpc.call
      .mockRejectedValueOnce(Object.assign(new Error('rejected'), { accepted: false }))
      .mockResolvedValueOnce({ sessionKey: SESSION_KEY, task_id: 'task-2' })

    await harness.api.onSend()
    const firstParams = structuredClone(harness.rpc.call.mock.calls[0]?.[1])
    current = { documentId: 'document-2', headRevisionId: 'revision-1' }

    await harness.api.onSend()
    const secondParams = harness.rpc.call.mock.calls[1]?.[1]

    expect(prepareDocumentContextForSend).toHaveBeenCalledTimes(2)
    expect(secondParams).toMatchObject({
      documentContext: current,
    })
    expect(secondParams?.clientRequestId).not.toBe(firstParams?.clientRequestId)
  })

  it('drops a prepared head when navigation changes the request session', async () => {
    let releaseFlush!: () => void
    const flushGate = new Promise<void>(resolve => { releaseFlush = resolve })
    const prepareDocumentContextForSend = vi.fn(async () => {
      await flushGate
      return { documentId: 'document-1', headRevisionId: 'revision-2' }
    })
    const harness = createHarness({
      currentDocumentContext: () => ({
        documentId: 'document-1',
        headRevisionId: 'revision-1',
      }),
      prepareDocumentContextForSend,
    })

    const sending = harness.api.onSend()
    await vi.waitFor(() => expect(prepareDocumentContextForSend).toHaveBeenCalledOnce())
    harness.options.sessionKey.value = 'agent:main:webchat:another-session'
    releaseFlush()
    await sending

    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toEqual([])
    expect(harness.options.inputText.value).toBe('Update the title and accent color.')
  })

  it('never combines document context with prompt annotations', async () => {
    const prepareDocumentContextForSend = vi.fn(async () => ({
      documentId: 'document-1',
      headRevisionId: 'revision-2',
    }))
    const harness = createHarness({
      promptAnnotationIds: ref(['annotation-1']),
      currentDocumentContext: () => ({
        documentId: 'document-1',
        headRevisionId: 'revision-1',
      }),
      prepareDocumentContextForSend,
    })

    await harness.api.onSend()

    expect(prepareDocumentContextForSend).not.toHaveBeenCalled()
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      promptAnnotationIds: ['annotation-1'],
    }))
    expect(harness.rpc.call.mock.calls[0]?.[1]).not.toHaveProperty('documentContext')
  })

  it('does not carry an open document into a new-session send', async () => {
    const prepareDocumentContextForSend = vi.fn(async () => ({
      documentId: 'document-1',
      headRevisionId: 'revision-2',
    }))
    const harness = createHarness({
      pendingSessionIntent: ref('new_chat'),
      currentDocumentContext: () => ({
        documentId: 'document-1',
        headRevisionId: 'revision-1',
      }),
      prepareDocumentContextForSend,
    })

    await harness.api.onSend()

    expect(prepareDocumentContextForSend).not.toHaveBeenCalled()
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      intent: 'new_chat',
    }))
    expect(harness.rpc.call.mock.calls[0]?.[1]).not.toHaveProperty('documentContext')
  })
})
