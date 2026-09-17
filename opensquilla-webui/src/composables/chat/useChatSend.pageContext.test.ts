import { createV4TurnCommandsFromRpcClient } from '@/adapters/gateway/turnCommandsV4'
import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import type { Attachment, ChatMessage } from '@/types/chat'
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'
import type { UseChatSendOptions } from './useChatSend'
import { useChatSend } from './useChatSend'

function snapshot(annotationId: string, sentOrder: number): PromptAnnotationSnapshot {
  return {
    annotationId,
    documentId: 'document-1',
    documentName: 'page.html',
    targetRef: 'target-1',
    resourceId: 'document:document-1',
    locatorHint: '#button',
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
    appendToolEnd: vi.fn(),
  }
  const options: UseChatSendOptions = {
    turnCommands: createV4TurnCommandsFromRpcClient(rpc),
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
    draftIds: ref(['annotation-2', 'annotation-1']),
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

describe('ordinary page annotation input', () => {
  it('sends text and locators through pageContext and acknowledges the immutable batch', async () => {
    const harness = createHarness()
    await harness.api.onSend()
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: 'Change annotation-2\nChange annotation-1',
      displayText: '',
      pageContext: {
        targetRef: 'target-1', resourceId: 'document:document-1',
        annotations: [
          { text: 'Change annotation-2', selectionText: '<button>', locatorHint: '#button' },
          { text: 'Change annotation-1', selectionText: '<button>', locatorHint: '#button' },
        ],
      },
    }))
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params).not.toHaveProperty('promptAnnotationIds')
    expect(params).not.toHaveProperty('documentContext')
    expect(harness.options.acknowledgePromptAnnotations).toHaveBeenCalledWith(
      [snapshot('annotation-2', 0), snapshot('annotation-1', 1)],
      'agent:main:webchat:test', undefined,
    )
  })

  it('sends a captured page and an explicit reference image as ordinary staged attachments', async () => {
    const capture: Attachment = {
      kind: 'staged', local_id: -1, name: 'page-selection.png', mime: 'image/png',
      file_uuid: 'capture-file',
    }
    const reference: Attachment = {
      kind: 'staged', local_id: 1, name: 'reference.png', mime: 'image/png',
      file_uuid: 'reference-file',
    }
    const prepare = vi.fn(async () => true)
    const harness = createHarness({
      annotationAttachments: () => [capture],
      pendingAttachments: ref([reference]),
      prepareAttachmentsForSend: prepare,
    })
    await harness.api.onSend()
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params.attachments).toEqual([
      { file_uuid: 'reference-file', type: 'image/png', mime: 'image/png', name: 'reference.png' },
      { file_uuid: 'capture-file', type: 'image/png', mime: 'image/png', name: 'page-selection.png' },
    ])
    expect(JSON.stringify(params.pageContext)).not.toContain('dataBase64')
    expect(prepare).toHaveBeenCalledWith(expect.objectContaining({ attachments: [reference, capture] }))
    expect(harness.options.messages.value[0]?.attachments).toHaveLength(2)
  })

  it('freezes screenshot uploads into the ordinary busy follow-up queue', async () => {
    const capture: Attachment = {
      kind: 'staged', local_id: -1, name: 'page-selection.png', mime: 'image/png',
      file_uuid: 'capture-file',
    }
    const harness = createHarness({ annotationAttachments: () => [capture] })
    harness.options.stream.isStreaming.value = true
    await harness.api.onSend()
    expect(harness.options.enqueuePendingInput).toHaveBeenCalledWith(
      '', undefined, expect.objectContaining({ attachments: [capture], pageContext: expect.any(Object) }),
    )
    expect(harness.rpc.call).not.toHaveBeenCalled()
  })

  it('retains editable drafts until ordinary chat acceptance', async () => {
    const harness = createHarness()
    let accept!: (value: Record<string, unknown>) => void
    harness.rpc.call.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const sending = harness.api.onSend()
    await vi.waitFor(() => expect(harness.rpc.call).toHaveBeenCalledOnce())
    expect(harness.options.acknowledgePromptAnnotations).not.toHaveBeenCalled()
    expect(harness.options.messages.value[0]?.promptAnnotations).toHaveLength(2)
    accept({ sessionKey: 'agent:main:webchat:test', task_id: 'task-1' })
    await sending
    expect(harness.options.acknowledgePromptAnnotations).toHaveBeenCalledOnce()
    expect(harness.options.messages.value[0]?.promptAnnotations).toHaveLength(2)
  })

  it('sends ordinary text without automatically attaching an open document', async () => {
    const harness = createHarness({ inputText: ref('Explain this idea'), draftIds: ref([]) })
    await harness.api.onSend()
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params.message).toBe('Explain this idea')
    expect(params).not.toHaveProperty('pageContext')
    expect(params).not.toHaveProperty('documentContext')
  })

  it('preserves a restored queued pageContext without any live draft identifiers', async () => {
    const harness = createHarness({ draftIds: ref([]) })
    const pageContext = {
      targetRef: 'target-queued', resourceId: 'document:queued',
      annotations: [{ text: 'Make this heading larger', locatorHint: 'h1' }],
    }
    await harness.api.sendQueuedFollowup({
      pendingUiId: 'pending-1', text: 'Update the heading', attachments: [], intent: null,
      ownerSessionKey: 'agent:main:webchat:test', pageContext,
    })
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({ pageContext }))
  })
})
