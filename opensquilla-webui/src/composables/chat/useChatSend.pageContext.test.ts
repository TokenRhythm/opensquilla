import { TurnCommandError } from '@/modules/turnCommands'
import { useToasts } from '@/composables/useToasts'
import { createV4TurnCommandsFromRpcClient } from '@/adapters/gateway/turnCommandsV4'
import { computed, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useArtifactPromptAnnotationsStore } from '@/stores/artifactPromptAnnotations'
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

describe('sending with empty page annotation drafts', () => {
  beforeEach(() => {
    const values = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    })
    setActivePinia(createPinia())
  })

  afterEach(() => vi.unstubAllGlobals())

  async function createDraftHarness(overrides: Partial<UseChatSendOptions> = {}) {
    const store = useArtifactPromptAnnotationsStore()
    const sessionKey = 'agent:main:webchat:test'
    const draft = {
      annotationId: 'empty-draft', sessionKey, documentId: 'document-1',
      documentName: 'page.html', resourceId: 'document:document-1', body: ' \n\t ',
      selection: {
        selectionId: 'selection-1', targetRef: 'target-1', tagName: 'h1',
        elementPath: 'h1', selectionText: 'Welcome', locatorHint: 'h1',
      },
    }
    await store.create(draft)
    const harness = createHarness({
      draftIds: computed(() => store.sendableDraftsForSession(sessionKey)
        .map(item => item.annotationId)),
      sendBlockedReason: computed(() => store.sendBlockedReason(sessionKey)),
      preparePromptAnnotationsForSend: ids => store.prepareForSend(ids),
      promptAnnotationSnapshots: ids => store.snapshotsForIds(ids),
      acknowledgePromptAnnotations: snapshots => { store.acknowledgeSent(snapshots) },
      ...overrides,
    })
    return { ...harness, store, draft }
  }

  it('sends populated annotations and preserves the empty draft for later editing', async () => {
    const harness = await createDraftHarness()
    await harness.store.create({
      ...harness.draft, annotationId: 'ready-draft', body: 'Enlarge this heading',
    })
    await harness.api.onSend()
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: 'Enlarge this heading',
      pageContext: expect.objectContaining({
        annotations: [{ text: 'Enlarge this heading', selectionText: 'Welcome', locatorHint: 'h1' }],
      }),
    }))
    expect(Object.keys(harness.store.annotations)).toEqual(['empty-draft'])
  })

  it('sends ordinary text while keeping empty annotations out of the payload', async () => {
    const harness = await createDraftHarness({ inputText: ref('Update the page title') })
    await harness.api.onSend()
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      message: 'Update the page title',
    }))
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params).not.toHaveProperty('pageContext')
    expect(Object.keys(harness.store.annotations)).toEqual(['empty-draft'])
  })

  it('sends restored instructions without preparing an expired automatic screenshot', async () => {
    const prepare = vi.fn<NonNullable<UseChatSendOptions['prepareAttachmentsForSend']>>(async () => true)
    const harness = await createDraftHarness({ prepareAttachmentsForSend: prepare })
    await harness.store.create({
      ...harness.draft, annotationId: 'restored-draft', body: 'Enlarge this heading',
    })
    localStorage.setItem('opensquilla.page-annotation-drafts.v1', JSON.stringify([
      harness.store.annotations['empty-draft'],
      {
        ...harness.store.annotations['restored-draft'],
        screenshotAttachment: {
          kind: 'staged', local_id: -1, name: 'page-selection.png', mime: 'image/png',
          file_uuid: 'expired-capture', expires_at: '2000-01-01T00:00:00Z',
        },
      },
    ]))
    harness.store.reset()

    await harness.api.onSend()
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params.message).toBe('Enlarge this heading')
    expect(params.pageContext).toMatchObject({
      targetRef: 'target-1',
      annotations: [{ text: 'Enlarge this heading', selectionText: 'Welcome', locatorHint: 'h1' }],
    })
    expect(params.attachments ?? []).toEqual([])
    expect(prepare.mock.calls.every(([options]) => !options?.attachments?.length)).toBe(true)
    expect(Object.keys(harness.store.annotations)).toEqual(['empty-draft'])
  })

  it('sends ordinary attachments while empty annotations are present', async () => {
    const reference: Attachment = {
      kind: 'staged', local_id: 1, name: 'reference.png', mime: 'image/png',
      file_uuid: 'reference-file',
    }
    const harness = await createDraftHarness({ pendingAttachments: ref([reference]) })
    await harness.api.onSend()
    expect(harness.rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { file_uuid: 'reference-file', type: 'image/png', mime: 'image/png', name: 'reference.png' },
      ],
    }))
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params).not.toHaveProperty('pageContext')
    expect(Object.keys(harness.store.annotations)).toEqual(['empty-draft'])
  })

  it('does not send an empty message when the only input is an empty annotation', async () => {
    const harness = await createDraftHarness()
    await harness.api.onSend()
    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.messages.value).toEqual([])
    expect(Object.keys(harness.store.annotations)).toEqual(['empty-draft'])
  })
})

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
    expect(params.attachments ?? []).toEqual([])
    expect(harness.options.messages.value[0]?.attachments ?? []).toEqual([])
    expect(harness.options.acknowledgePromptAnnotations).toHaveBeenCalledWith(
      [snapshot('annotation-2', 0), snapshot('annotation-1', 1)],
      'agent:main:webchat:test', undefined,
    )
  })

  it('sends an explicitly attached reference image alongside page annotations', async () => {
    const reference: Attachment = {
      kind: 'staged', local_id: 1, name: 'reference.png', mime: 'image/png',
      file_uuid: 'reference-file',
    }
    const prepare = vi.fn(async () => true)
    const harness = createHarness({
      pendingAttachments: ref([reference]),
      prepareAttachmentsForSend: prepare,
    })
    await harness.api.onSend()
    const params = harness.rpc.call.mock.calls[0]?.[1] as Record<string, unknown>
    expect(params.attachments).toEqual([
      { file_uuid: 'reference-file', type: 'image/png', mime: 'image/png', name: 'reference.png' },
    ])
    expect(JSON.stringify(params.pageContext)).not.toContain('dataBase64')
    expect(prepare).toHaveBeenCalledWith(expect.objectContaining({ attachments: [reference] }))
    expect(harness.options.messages.value[0]?.attachments).toHaveLength(1)
  })

  it('queues page annotations without adding a screenshot attachment', async () => {
    const harness = createHarness()
    harness.options.stream.isStreaming.value = true
    await harness.api.onSend()
    expect(harness.options.enqueuePendingInput).toHaveBeenCalledWith(
      '', undefined, expect.objectContaining({ attachments: [], pageContext: expect.any(Object) }),
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

describe('explicit skill send ownership', () => {
  const skill = { name: 'synthetic-table', instanceId: 'instance-one', digest: 'digest-one' }
  const other = { name: 'synthetic-paper', instanceId: 'instance-two', digest: 'digest-two' }

  function skillHarness() {
    const harness = createHarness({
      inputText: ref('Make a table'), draftIds: ref([]), selectedSkills: ref([{ ...skill }]),
    })
    harness.options.turnCommands = createV4TurnCommandsFromRpcClient(harness.rpc, () => true)
    return harness
  }

  it('retains the chip until acceptance and submits it together with attachments', async () => {
    const harness = skillHarness()
    harness.options.pendingAttachments.value = [{
      kind: 'staged', local_id: 1, name: 'sample.txt', mime: 'text/plain', file_uuid: 'file-one',
    }]
    let accept!: (value: Record<string, unknown>) => void
    harness.rpc.call.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const sending = harness.api.onSend()
    await vi.waitFor(() => expect(harness.rpc.call).toHaveBeenCalledOnce())
    expect(harness.options.selectedSkills!.value).toEqual([skill])
    expect(harness.options.inputText.value).toBe('Make a table')
    expect(harness.rpc.call.mock.calls[0]?.[1]).toMatchObject({
      selectedSkills: [skill], queueMode: 'followup', attachments: [{ file_uuid: 'file-one' }],
    })
    accept({ sessionKey: 'agent:main:webchat:test', task_id: 'task-one' })
    await sending
    expect(harness.options.selectedSkills!.value).toEqual([])
    expect(harness.options.inputText.value).toBe('')
  })

  it('a late acceptance cannot clear a newly selected skill', async () => {
    const harness = skillHarness()
    let accept!: (value: Record<string, unknown>) => void
    harness.rpc.call.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const sending = harness.api.onSend()
    await vi.waitFor(() => expect(harness.rpc.call).toHaveBeenCalledOnce())
    harness.options.selectedSkills!.value = [other]
    harness.options.inputText.value = 'Next request'
    accept({ sessionKey: 'agent:main:webchat:test', task_id: 'task-one' })
    await sending
    expect(harness.options.selectedSkills!.value).toEqual([other])
    expect(harness.options.inputText.value).toBe('Next request')
    expect(harness.rpc.call.mock.calls[0]?.[1]).toMatchObject({ selectedSkills: [skill] })
  })

  it('queues selected skills even when the busy-send preference is steer', async () => {
    const harness = skillHarness()
    harness.options.stream.isStreaming.value = true
    harness.options.busySendMode.value = 'steer'
    await harness.api.onSend()
    expect(harness.options.enqueuePendingInput).toHaveBeenCalledWith(
      'Make a table', undefined, expect.objectContaining({ selectedSkills: [skill] }),
    )
    expect(harness.rpc.call).not.toHaveBeenCalled()
  })

  it('blocks restored selections on an older gateway without losing the draft', async () => {
    const harness = skillHarness()
    harness.options.turnCommands = createV4TurnCommandsFromRpcClient(harness.rpc, () => false)
    await harness.api.onSend()
    expect(harness.rpc.call).not.toHaveBeenCalled()
    expect(harness.options.inputText.value).toBe('Make a table')
    expect(harness.options.selectedSkills!.value).toEqual([skill])
  })

  it('a restored queued message sends its own selection and keeps the live composer', async () => {
    const harness = skillHarness()
    harness.options.selectedSkills!.value = [other]
    await harness.api.sendQueuedFollowup({
      pendingUiId: 'pending-skill', text: 'Queued request', selectedSkills: [skill],
      attachments: [], intent: null, ownerSessionKey: 'agent:main:webchat:test',
    })
    expect(harness.rpc.call.mock.calls[0]?.[1]).toMatchObject({ selectedSkills: [skill] })
    expect(harness.options.selectedSkills!.value).toEqual([other])
    expect(harness.options.inputText.value).toBe('Make a table')
  })
})

describe('explicit skill send boundaries', () => {
  const skill = { name: 'synthetic-table', instanceId: 'instance-one', digest: 'digest-one' }
  const other = { name: 'synthetic-paper', instanceId: 'instance-two', digest: 'digest-two' }
  function harness() {
    const result = createHarness({
      inputText: ref('Make a table'), draftIds: ref([]), selectedSkills: ref([{ ...skill }]),
      deliveryIdentity: ref('synthetic-gateway:owner'),
    })
    result.options.turnCommands = createV4TurnCommandsFromRpcClient(result.rpc, () => true)
    return result
  }

  it.each(['/compact', '/meta report -- summarize', '/plan', '!pwd'])('keeps explicit selection out of %s controls', async command => {
    const result = harness()
    result.options.inputText.value = command
    result.options.classifySlashCommand = vi.fn(async () => 'registered' as const)
    await result.api.onSend()
    expect(result.options.executeSlashCommand).not.toHaveBeenCalled()
    expect(result.rpc.call).not.toHaveBeenCalled()
    expect(result.options.selectedSkills!.value).toEqual([skill])
    expect(result.options.inputText.value).toBe(command)
  })

  it('a session change during project validation cannot send the old selection', async () => {
    const result = harness()
    let validated!: () => void
    result.options.validateActiveProjectBeforeSend = vi.fn(() => new Promise<string | null>(resolve => {
      validated = () => resolve(null)
    }))
    const sending = result.api.onSend()
    await vi.waitFor(() => expect(result.options.validateActiveProjectBeforeSend).toHaveBeenCalledOnce())
    result.options.sessionKey.value = 'agent:main:webchat:other'
    result.options.selectedSkills!.value = [other]
    result.options.inputText.value = 'Other session'
    validated()
    await sending
    expect(result.rpc.call).not.toHaveBeenCalled()
    expect(result.options.selectedSkills!.value).toEqual([other])
    expect(result.options.inputText.value).toBe('Other session')
  })

  it('a late ACK keeps an intentional re-selection of the same skill', async () => {
    const result = harness()
    let accepted!: (value: Record<string, unknown>) => void
    result.rpc.call.mockImplementationOnce(() => new Promise(resolve => { accepted = resolve }))
    const sending = result.api.onSend()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledOnce())
    result.options.selectedSkills!.value = [{ ...skill }]
    accepted({ sessionKey: 'agent:main:webchat:test', task_id: 'task-one' })
    await sending
    expect(result.options.selectedSkills!.value).toEqual([skill])
  })

  it('consumes only the source saved snapshot when acceptance arrives in another session', async () => {
    const result = harness()
    result.options.consumeAcceptedDraft = vi.fn(async () => true)
    let accepted!: (value: Record<string, unknown>) => void
    result.rpc.call.mockImplementationOnce(() => new Promise(resolve => { accepted = resolve }))
    const sending = result.api.onSend()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledOnce())
    result.options.sessionKey.value = 'agent:main:webchat:other'
    result.options.selectedSkills!.value = [other]
    result.options.inputText.value = 'Other session'
    accepted({ sessionKey: 'agent:main:webchat:test', task_id: 'task-one' })
    await sending
    expect(result.options.consumeAcceptedDraft).toHaveBeenCalledExactlyOnceWith(
      'agent:main:webchat:test', { text: 'Make a table', selectedSkills: [skill] },
    )
    expect(result.options.selectedSkills!.value).toEqual([other])
    expect(result.options.inputText.value).toBe('Other session')
  })

  it('keeps the saved skill draft when a source request is rejected offscreen', async () => {
    const result = harness()
    result.options.consumeAcceptedDraft = vi.fn(async () => true)
    let rejected!: (error: unknown) => void
    result.rpc.call.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejected = reject }))
    const sending = result.api.onSend()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledOnce())
    result.options.sessionKey.value = 'agent:main:webchat:other'
    result.options.selectedSkills!.value = [other]
    result.options.inputText.value = 'Other session'
    rejected(new TurnCommandError('rejected', 'Synthetic retry', 'NOT_READY', false, true))
    await sending
    expect(result.options.consumeAcceptedDraft).not.toHaveBeenCalled()
    expect(result.options.selectedSkills!.value).toEqual([other])
    expect(result.options.inputText.value).toBe('Other session')
  })

  it('retries the rejected original without merging it into a differently selected draft', async () => {
    const result = harness()
    let rejected!: (error: unknown) => void
    result.rpc.call.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejected = reject }))
    const sending = result.api.onSend()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledOnce())
    const original = structuredClone(result.rpc.call.mock.calls[0]?.[1])
    result.options.selectedSkills!.value = [other]
    result.options.inputText.value = 'A new request'
    rejected(new TurnCommandError('rejected', 'Synthetic retry', 'NOT_READY', false, true))
    await sending
    expect(result.options.inputText.value).toBe('A new request')
    expect(result.options.selectedSkills!.value).toEqual([other])
    const toast = [...useToasts().toasts.value].reverse().find(item => item.action)
    expect(toast?.action).toBeDefined()
    toast!.action!.onClick()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledTimes(2))
    expect(result.rpc.call.mock.calls[1]?.[1]).toEqual(original)
    expect(result.options.inputText.value).toBe('A new request')
    expect(result.options.selectedSkills!.value).toEqual([other])
  })

  it('preserves a removed skill tag on rejection and retries the original separately', async () => {
    const result = harness()
    let rejected!: (error: unknown) => void
    result.rpc.call.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejected = reject }))
    const sending = result.api.onSend()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledOnce())
    const original = structuredClone(result.rpc.call.mock.calls[0]?.[1])
    result.options.selectedSkills!.value = []
    rejected(new TurnCommandError('rejected', 'Synthetic retry', 'NOT_READY', false, true))
    await sending

    expect(result.options.inputText.value).toBe('Make a table')
    expect(result.options.selectedSkills!.value).toEqual([])
    const toast = [...useToasts().toasts.value].reverse().find(item => item.action)
    expect(toast?.action).toBeDefined()
    toast!.action!.onClick()
    await vi.waitFor(() => expect(result.rpc.call).toHaveBeenCalledTimes(2))

    expect(result.rpc.call.mock.calls[1]?.[1]).toEqual(original)
    expect(result.options.inputText.value).toBe('Make a table')
    expect(result.options.selectedSkills!.value).toEqual([])
  })
})

it('consumes the accepted skill snapshot before acknowledged annotations mutate composer state', async () => {
  const skill = { name: 'synthetic-table', instanceId: 'instance-one', digest: 'digest-one' }
  const draftIds = ref(['annotation-1'])
  const result = createHarness({
    draftIds, selectedSkills: ref([skill]),
    acknowledgePromptAnnotations: vi.fn(() => { draftIds.value = [] }),
  })
  result.options.turnCommands = createV4TurnCommandsFromRpcClient(result.rpc, () => true)
  await result.api.onSend()
  expect(result.rpc.call.mock.calls[0]?.[1]).toMatchObject({
    selectedSkills: [skill], pageContext: { annotations: [{ text: 'Change annotation-1' }] },
  })
  expect(draftIds.value).toEqual([])
  expect(result.options.selectedSkills!.value).toEqual([])
})
