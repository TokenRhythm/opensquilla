import { describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import type { ChatMessage, ChatPendingItem } from '@/types/chat'
import { TurnCommandError, type TurnSendRequest, type TurnSendResponse, type TurnReceiptResult } from '@/modules/turnCommands'
import { createPendingQueuePolicy } from '@/utils/chat/pendingQueuePolicy'
import { decideSendResponseSession, type UseChatSendOptions } from './useChatSend'
import { createChatSendHarness, memoryDeliveryWal, flushDelivery } from './chatSendTestHarness'
import { useChatTaskOwnership } from './useChatTaskOwnership'
import { useChatSteerDelivery } from './useChatSteerDelivery'
import type { DeliveryWalRecord } from '@/utils/chat/pendingInputWal'
import { acceptsVisualizeContext, useVisualizeDraft } from '@/visualize/useVisualizeDraft'
import type { VisualizeReceipt } from '@/visualize/types'
import type { SlashCommandClassification } from './useChatSlashCommands'
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'
import { useChatStream } from './useChatStream'

const { pushToast } = vi.hoisted(() => ({ pushToast: vi.fn() }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))

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
    supportsResolvableSend: () => false,
    getReceiptEpoch: () => 'epoch-1',
    isAdmissionClosed: vi.fn(() => false),
    resolveReceipt: vi.fn(async (): Promise<TurnReceiptResult> => ({ status: 'retired' })),
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
    persistRestoredDraft: () => true,
    ...overrides,
  }
  return { ...createChatSendHarness(options), commands, policy, scope, pausePendingAutoSend }
}

const closureAnnotation: PromptAnnotationSnapshot = {
  annotationId: 'original-note', documentId: 'page-1', documentName: 'page.html',
  body: 'Make this heading larger', targetRef: 'page:1', resourceId: 'document:page-1',
  tagName: 'h1', quote: 'Heading', sentOrder: 0,
}

describe('fenced delivery draft recovery', () => {
  it.each(['retired', 'accepted-unavailable', 'text', 'provider activity', 'bound history', 'later content'] as const)(
    'cleans only its retired local admission placeholder: %s', async (scenario) => {
      vi.useFakeTimers()
      vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
      vi.stubGlobal('cancelAnimationFrame', vi.fn())
      const previous: ChatMessage = { role: 'assistant', clientId: 'previous-empty-row', text: '', ts: 1 }
      const messages = ref<ChatMessage[]>([previous])
      const stream = useChatStream({ messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(false),
        applySessionRunState: vi.fn(), renderMarkdown: text => text,
        stripDirectiveTags: text => text, stripGeneratedArtifactMarkers: text => text, scrollToBottom: vi.fn() })
      const h = queuedSendHarness({ messages, stream })
      try {
        h.commands.supportsResolvableSend = () => true
        h.commands.send.mockImplementationOnce(async () => {
          if (scenario === 'text') stream.appendDelta('Actual answer')
          if (scenario === 'provider activity') stream.setStreamActivity('Waiting for model', 'provider:requesting')
          if (scenario === 'bound history') stream.setAssistantMessageId('durable-message')
          throw new TurnCommandError('transport', 'lost ACK', undefined, null)
        })
        await h.api.onSend()
        const pending = messages.value.filter(message => message.role === 'assistant' && message.clientId !== previous.clientId)
        expect(pending).toHaveLength(1)
        if (scenario === 'later content') pending[0]!.text = 'History caught up before Check'
        await vi.advanceTimersByTimeAsync(6_000)
        const id = h.api.deliveryRecoveryEntries.value[0]!.id
        if (scenario === 'accepted-unavailable') h.commands.resolveReceipt.mockResolvedValueOnce({ status: 'accepted-unavailable' })
        await h.api.checkDelivery(id)
        const remaining = messages.value.filter(message => message.role === 'assistant')
        expect(remaining.map(message => message.clientId)).toEqual(scenario === 'retired'
          ? [previous.clientId] : [previous.clientId, pending[0]!.clientId])
        expect(h.commands.send).toHaveBeenCalledOnce()
        expect(stream.isStreaming.value).toBe(false)
      } finally {
        h.api.dispose(); h.options.durableDelivery.dispose(); stream.cleanup()
        vi.unstubAllGlobals(); vi.useRealTimers()
      }
    },
  )

  it('keeps the old handoff when attachments change while the restored draft write is pending', async () => {
    vi.useFakeTimers()
    let stored!: (result: boolean) => void
    const h = queuedSendHarness({ persistRestoredDraft: () => new Promise(resolve => { stored = resolve }) })
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      const checking = h.api.checkDelivery(id)
      await vi.advanceTimersByTimeAsync(0)
      h.options.pendingAttachments.value = [{ kind: 'staged', local_id: 2, name: 'new.txt', mime: 'text/plain', file_uuid: 'new-upload' }]
      stored(true)
      await checking
      expect((await h.options.durableDelivery.get(id))?.handoff).toBeDefined()
      expect(h.options.pendingAttachments.value[0]?.local_id).toBe(2)
      expect(h.commands.send).toHaveBeenCalledOnce()
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it('retains the fenced handoff if local draft persistence fails and can recover it after reload', async () => {
    vi.useFakeTimers()
    const wal = memoryDeliveryWal()
    const h = queuedSendHarness({ pendingInputWal: wal, persistRestoredDraft: () => false })
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      await h.api.checkDelivery(id)
      expect(h.options.inputText.value).toBe('unrelated composer draft')
      expect((await h.options.durableDelivery.get(id))?.handoff?.composerText).toBe('unrelated composer draft')
      expect(h.api.deliveryRecoveryEntries.value).toEqual([expect.objectContaining({ draftAvailable: true })])
      h.api.dispose(); h.options.durableDelivery.dispose()
      const fresh = queuedSendHarness({ inputText: ref(''), pendingInputWal: wal })
      await fresh.options.durableDelivery.wake()
      await fresh.api.checkDelivery(id)
      expect(fresh.options.inputText.value).toBe('unrelated composer draft')
      expect(fresh.commands.send).not.toHaveBeenCalled()
      expect(fresh.commands.resolveReceipt).not.toHaveBeenCalled()
      expect(fresh.api.deliveryRecoveryEntries.value).toEqual([])
      fresh.options.durableDelivery.dispose()
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it('completes an explicit Check that joins an already-running background receipt round', async () => {
    vi.useFakeTimers()
    const h = queuedSendHarness()
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(0)
      expect(h.commands.lookupReceipt).toHaveBeenCalledOnce()
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      const checking = h.api.checkDelivery(id)
      await vi.advanceTimersByTimeAsync(6_000)
      await checking
      expect(h.commands.resolveReceipt).toHaveBeenCalledOnce()
      expect(h.options.inputText.value).toBe('unrelated composer draft')
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
      expect(h.commands.send).toHaveBeenCalledOnce()
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it('keeps annotation-only input intact, resolves only on Check, and uses a fresh ID for the next send', async () => {
    vi.useFakeTimers()
    const drafts = ref([closureAnnotation.annotationId])
    const acknowledge = vi.fn(() => { drafts.value = [] })
    const h = queuedSendHarness({ inputText: ref(''), draftIds: drafts,
      promptAnnotationSnapshots: () => [closureAnnotation], acknowledgePromptAnnotations: acknowledge,
      restorePromptAnnotations: vi.fn(() => true) })
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      await h.options.durableDelivery.wake()
      const original = h.commands.send.mock.calls[0]![0]
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      expect(h.commands.resolveReceipt).not.toHaveBeenCalled()
      expect((await h.options.durableDelivery.get(id))?.handoff).toMatchObject({
        composerText: '', recoveryDraftIds: ['original-note'],
        recoveryPromptAnnotations: [expect.objectContaining({ body: closureAnnotation.body })],
      })
      await h.api.checkDelivery(id)
      expect(h.commands.resolveReceipt).toHaveBeenCalledOnce()
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(acknowledge).not.toHaveBeenCalled()
      expect(drafts.value).toEqual(['original-note'])
      expect(h.options.inputText.value).toBe('')
      expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
      expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(false)
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
      await h.api.onSend()
      expect(h.commands.send).toHaveBeenCalledTimes(2)
      expect(h.commands.send.mock.calls[1]![0].params.clientRequestId).not.toBe(original.params.clientRequestId)
      expect(acknowledge).toHaveBeenCalledOnce()
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it('retains the original handoff behind a newer draft and later restores locally without another RPC', async () => {
    vi.useFakeTimers()
    const skills = [{ name: 'review', instanceId: 'skill-1', digest: 'digest-1' }]
    const attachment = { kind: 'staged' as const, local_id: 1, name: 'notes.txt', mime: 'text/plain', file_uuid: 'original-upload' }
    const h = queuedSendHarness({ inputText: ref('review\nC:/notes/source.txt'), selectedSkills: ref(skills), localPathReferences: ref(['C:/notes/source.txt']),
      pendingAttachments: ref([attachment]),
      pendingSessionIntent: ref('new_chat'), pendingWorkspaceId: ref('workspace-1') })
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      h.options.inputText.value = 'a different request'
      h.options.selectedSkills!.value = []
      h.options.localPathReferences!.value = []
      h.options.pendingAttachments.value = []
      await h.api.checkDelivery(id)
      expect(h.options.inputText.value).toBe('a different request')
      expect(h.api.deliveryRecoveryEntries.value).toEqual([expect.objectContaining({ draftAvailable: true })])
      h.options.inputText.value = ''
      await h.api.checkDelivery(id)
      expect(h.options.inputText.value).toBe('review\nC:/notes/source.txt')
      expect(h.options.selectedSkills!.value).toEqual(skills)
      expect(h.options.localPathReferences!.value).toEqual(['C:/notes/source.txt'])
      expect(h.options.pendingSessionIntent.value).toBe('new_chat')
      expect(h.options.pendingWorkspaceId!.value).toBe('workspace-1')
      expect(h.options.pendingAttachments.value).toEqual([attachment])
      expect(h.commands.resolveReceipt).toHaveBeenCalledOnce()
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it('recovers annotation-only material after page recreation without an in-memory SendAttempt', async () => {
    vi.useFakeTimers()
    const wal = memoryDeliveryWal()
    const h = queuedSendHarness({ inputText: ref(''), pendingInputWal: wal,
      draftIds: ref(['original-note']), promptAnnotationSnapshots: () => [closureAnnotation] })
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      h.api.dispose(); h.options.durableDelivery.dispose()
      const restored = ref<PromptAnnotationSnapshot[]>([])
      const fresh = queuedSendHarness({ inputText: ref(''), pendingInputWal: wal, draftIds: ref([]),
        restorePromptAnnotations: (snapshots) => { restored.value = [...snapshots]; return true } })
      fresh.commands.supportsResolvableSend = () => true
      await fresh.options.durableDelivery.wake()
      await fresh.api.checkDelivery(id)
      expect(restored.value).toEqual([expect.objectContaining({ annotationId: 'original-note', body: closureAnnotation.body })])
      expect(fresh.options.inputText.value).toBe('')
      expect(fresh.commands.send).not.toHaveBeenCalled()
      expect(fresh.options.adoptResponseSession).not.toHaveBeenCalled()
      expect(fresh.api.deliveryRecoveryEntries.value).toEqual([])
      fresh.options.durableDelivery.dispose()
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it('closes accepted-unavailable without restoring, routing, or consuming a newer annotation edit twice', async () => {
    vi.useFakeTimers()
    let current = { ...closureAnnotation }
    const acknowledge = vi.fn((snapshots: readonly PromptAnnotationSnapshot[]) => {
      if (snapshots[0]?.body === current.body) throw new Error('newer annotation was incorrectly acknowledged')
    })
    const h = queuedSendHarness({ inputText: ref(''), draftIds: ref(['original-note']),
      promptAnnotationSnapshots: () => [current], acknowledgePromptAnnotations: acknowledge,
      restorePromptAnnotations: vi.fn(() => true) })
    try {
      h.commands.supportsResolvableSend = () => true
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost ACK', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      current = { ...closureAnnotation, body: 'A newer edited instruction' }
      h.options.inputText.value = 'new draft'
      h.commands.resolveReceipt.mockResolvedValue({ status: 'accepted-unavailable' })
      const id = h.api.deliveryRecoveryEntries.value[0]!.id
      await h.api.checkDelivery(id)
      await h.api.recoverResponseHandoffs()
      await h.options.durableDelivery.wake()
      expect(acknowledge).toHaveBeenCalledOnce()
      expect(acknowledge.mock.calls[0]![0][0]?.body).toBe(closureAnnotation.body)
      expect(current.body).toBe('A newer edited instruction')
      expect(h.options.inputText.value).toBe('new draft')
      expect(h.options.restorePromptAnnotations).not.toHaveBeenCalled()
      expect(h.options.adoptResponseSession).not.toHaveBeenCalled()
      expect(h.options.activeStreamTaskId.value).toBe('')
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
      expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
      expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(false)
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })
})

describe('ordinary send recovery stays separate from rendering', () => {
  it('keeps a running turn after navigating A, sending in B, and returning before A ACK', async () => {
    const h = queuedSendHarness()
    const sessionA = h.options.sessionKey.value
    let acceptA!: (response: TurnSendResponse) => void
    h.commands.send.mockImplementationOnce(() => new Promise(resolve => { acceptA = resolve }))
    const sendingA = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())

    // Session switching resets render ownership; the ChatView itself is reused.
    h.options.sessionKey.value = 'agent:main:webchat:B'
    h.options.messages.value = []
    h.options.stream.isStreaming.value = false
    h.options.activeStreamTaskId.value = ''
    h.options.activeStreamSessionKey.value = ''
    h.options.inputText.value = 'message in B'
    await h.api.onSend()

    h.options.sessionKey.value = sessionA
    const visibleAnswer: ChatMessage = { role: 'assistant', text: 'A is still answering', ts: '2026-01-01T00:00:00Z' }
    h.options.messages.value = [visibleAnswer]
    h.options.inputText.value = 'new draft in A'
    h.options.stream.isStreaming.value = true
    h.options.activeStreamTaskId.value = 'running-A'
    h.options.activeStreamSessionKey.value = sessionA
    vi.mocked(h.options.scheduleHistorySync).mockClear()
    acceptA({ taskId: 'running-A', sessionKey: sessionA, taskStatus: 'running' })
    await sendingA
    await h.options.durableDelivery.wake()

    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.commands.cancel).not.toHaveBeenCalled()
    expect(h.options.messages.value).toEqual([visibleAnswer])
    expect(h.options.inputText.value).toBe('new draft in A')
    expect(h.options.activeStreamTaskId.value).toBe('running-A')
    expect(h.options.scheduleHistorySync).toHaveBeenCalled()
  })

  it('quietly reconciles a lost ACK without resending or replacing a newer draft and answer', async () => {
    pushToast.mockClear()
    const h = queuedSendHarness()
    let rejectSend!: (error: Error) => void
    let acceptReceipt!: (response: TurnReceiptResult) => void
    h.commands.send.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSend = reject }))
    h.commands.lookupReceipt.mockImplementation(() => new Promise(resolve => { acceptReceipt = resolve }))
    const sending = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    const originalRequest = h.commands.send.mock.calls[0]![0]
    h.options.inputText.value = 'draft typed while waiting'
    h.options.messages.value.push({ role: 'assistant', text: 'already received output', ts: '2026-01-01T00:00:00Z' })
    rejectSend(new TurnCommandError('transport', 'synthetic ACK loss', undefined, null))
    await sending
    await vi.waitFor(() => expect(h.commands.lookupReceipt).toHaveBeenCalledOnce())
    acceptReceipt({ status: 'found', response: { taskId: 'original-task', taskStatus: 'running' } })
    await h.options.durableDelivery.wake()

    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.commands.lookupReceipt).toHaveBeenCalledWith(
      { kind: 'send', request: originalRequest }, expect.objectContaining({ signal: expect.anything() }),
    )
    expect(h.options.durableDelivery.snapshots()[0]?.phase).toBe('accepted')
    expect(h.options.inputText.value).toBe('draft typed while waiting')
    expect(h.options.messages.value).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'assistant', text: 'already received output' }),
    ]))
    expect(pushToast).not.toHaveBeenCalled()
    expect(h.options.messages.value.filter(message => message.role === 'error')).toEqual([])
  })

  it('keeps an unresolved request checkable after the receipt budget without claiming failure or resending', async () => {
    vi.useFakeTimers()
    pushToast.mockClear()
    const h = queuedSendHarness()
    try {
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'synthetic ACK loss', undefined, null))
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(6_000)
      await h.options.durableDelivery.wake()
      expect(h.commands.lookupReceipt).toHaveBeenCalledTimes(4)
      expect(h.api.deliveryRecoveryEntries.value).toEqual([
        expect.objectContaining({ phase: 'unknown', waitReason: 'budget' }),
      ])
      expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(true)
      expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(true)
      expect(pushToast).not.toHaveBeenCalled()
      expect(h.options.messages.value.filter(message => message.role === 'error')).toEqual([])

      h.commands.lookupReceipt.mockResolvedValue({ status: 'found', response: { taskId: 'original-task' } })
      await h.api.checkDelivery(h.api.deliveryRecoveryEntries.value[0]!.id)
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
    } finally {
      h.options.durableDelivery.dispose()
      vi.useRealTimers()
    }
  })

  it('keeps an unsupported receipt checkable without claiming failure or resending', async () => {
    pushToast.mockClear()
    const h = queuedSendHarness()
    h.commands.supportsReceiptLookup = () => false
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'synthetic ACK loss', undefined, null))
    await h.api.onSend()
    await h.options.durableDelivery.wake()

    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.api.deliveryRecoveryEntries.value).toEqual([
      expect.objectContaining({ phase: 'unknown', waitReason: 'receipt-unsupported' }),
    ])
    expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(true)
    expect(h.options.messages.value.some(message => message.role === 'user')).toBe(true)
    expect(pushToast).not.toHaveBeenCalled()

    await h.api.checkDelivery(h.api.deliveryRecoveryEntries.value[0]!.id)
    expect(h.commands.send).toHaveBeenCalledOnce()
  })

  it('restores the draft and reports an offline failure proven to precede dispatch', async () => {
    pushToast.mockClear()
    const h = queuedSendHarness()
    const draft = h.options.inputText.value
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('unavailable', 'offline before transport', undefined, false, true))
    await h.api.onSend()

    expect(h.options.inputText.value).toBe(draft)
    expect(h.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ tone: 'danger' }))
    expect(h.options.messages.value.filter(message => message.role === 'error')).toHaveLength(1)
  })

  it('preserves a pre-ACK Stop through another session send and cancels only the original task', async () => {
    const acceptanceStopPending = ref(false)
    const h = queuedSendHarness({ acceptanceStopPending })
    const sessionA = h.options.sessionKey.value
    let acceptA!: (response: TurnSendResponse) => void
    h.commands.send.mockImplementationOnce(() => new Promise(resolve => { acceptA = resolve }))
    const sendingA = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    h.api.onStop()

    h.options.sessionKey.value = 'agent:main:webchat:B'
    // Match the session runtime's visible-state reset; the durable request
    // retains A's Stop independently of this latch.
    acceptanceStopPending.value = false
    h.options.messages.value = []
    h.options.activeStreamTaskId.value = ''
    h.options.activeStreamSessionKey.value = ''
    h.options.stream.isStreaming.value = false
    h.options.inputText.value = 'message in B'
    await h.api.onSend()

    h.options.sessionKey.value = sessionA
    const visibleAnswer: ChatMessage = { role: 'assistant', text: 'A partial output', ts: '2026-01-01T00:00:00Z' }
    h.options.messages.value = [visibleAnswer]
    h.options.inputText.value = 'new draft in A'
    acceptA({ taskId: 'original-A-task', sessionKey: sessionA, taskStatus: 'running' })
    await sendingA
    await h.options.durableDelivery.wake()

    await vi.waitFor(() => expect(h.commands.cancel).toHaveBeenCalledExactlyOnceWith({
      sessionKey: sessionA, taskId: 'original-A-task', source: 'webui_stop', scope: 'task',
    }, expect.anything()))
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.options.inputText.value).toBe('new draft in A')
    expect(h.options.messages.value).toEqual([visibleAnswer])
  })

  it('preserves a newer draft and output when the receipt resolves immediately after ACK loss', async () => {
    pushToast.mockClear()
    const h = queuedSendHarness()
    let rejectSend!: (error: Error) => void
    h.commands.send.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSend = reject }))
    h.commands.lookupReceipt.mockResolvedValue({
      status: 'found', response: { taskId: 'original-task', taskStatus: 'running' },
    })
    const sending = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    h.options.inputText.value = 'draft typed while waiting'
    h.options.messages.value.push({ role: 'assistant', text: 'already received output', ts: '2026-01-01T00:00:00Z' })
    rejectSend(new TurnCommandError('transport', 'synthetic ACK loss', undefined, null))
    await sending
    await h.options.durableDelivery.wake()

    expect(h.options.durableDelivery.snapshots()[0]?.phase).toBe('accepted')
    expect(h.options.inputText.value).toBe('draft typed while waiting')
    expect(h.options.messages.value).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'assistant', text: 'already received output' }),
    ]))
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.commands.cancel).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
    expect(h.api.deliveryRecoveryEntries.value).toEqual([])
  })

  it('still restores a definitely rejected draft and reports the failure', async () => {
    pushToast.mockClear()
    const h = queuedSendHarness()
    const draft = h.options.inputText.value
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('rejected', 'synthetic refusal', 'REFUSED', false, true))
    await h.api.onSend()
    expect(h.options.inputText.value).toBe(draft)
    expect(h.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ tone: 'danger' }))
    expect(h.options.messages.value.filter(message => message.role === 'error')).toHaveLength(1)
  })
})

describe('explicit composer context text', () => {
  const contextualText = 'Explain this choice\n\nCanvas conditions · choices.html\nTwo selected items\n\n{"selected":["a","b"]}'
  it.each([
    { text: '//literal question', classification: 'registered', attached: true },
    { text: '/unknown question', classification: 'unknown', attached: true },
    { text: '/compact', classification: 'registered', attached: false },
    { text: '/unknown question', classification: 'unavailable', attached: false },
  ] as const)('preserves sender semantics for $text ($classification) with a canvas attachment', async ({ text, classification, attached }) => {
    const classify = vi.fn(async (): Promise<SlashCommandClassification> => classification)
    const execute = vi.fn(async () => true)
    const h = queuedSendHarness({ inputText: ref(text), classifySlashCommand: classify, executeSlashCommand: execute })
    const scope = effectScope()
    const draft = scope.run(() => useVisualizeDraft({ sessionKey: h.options.sessionKey,
      deliveryIdentity: h.options.deliveryIdentity!, generation: ref(0), available: () => true }))!
    try {
      draft.attach(h.options.sessionKey.value,
        { documentId: 'doc', revisionId: 'rev', name: 'choices.html' } as VisualizeReceipt,
        { summary: 'Selected A', data: { selected: 'A' } })
      const context = draft.capture()!
      const withContext = await acceptsVisualizeContext(text, classify)
      expect(withContext).toBe(attached)
      await h.api.onSend({
        ...(withContext ? { composerText: text, textOverride: context.message(text), consumeComposerOnQueue: true } : {}),
        onAccepted: () => { if (withContext) context.accepted() },
      })
      if (attached) {
        expect(h.commands.send).toHaveBeenCalledOnce()
        expect(h.commands.send.mock.calls[0]![0]).toMatchObject({
          params: { message: context.message(text.startsWith('//') ? text.slice(1) : text) },
        })
        expect(execute).not.toHaveBeenCalled()
        expect(draft.snapshot.value).toBeNull()
      } else {
        expect(h.commands.send).not.toHaveBeenCalled()
        expect(execute).toHaveBeenCalledWith(text, classification)
        expect(draft.snapshot.value?.summary).toBe('Selected A')
      }
      if (text.startsWith('//')) expect(classify).not.toHaveBeenCalled()
    } finally { scope.stop() }
  })

  it('sends the attached snapshot through the ordinary command path and acknowledges only after acceptance', async () => {
    const h = queuedSendHarness(), accepted = vi.fn()
    const composerText = h.options.inputText.value
    let accept!: (response: TurnSendResponse) => void
    h.commands.send.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const pending = h.api.onSend({ textOverride: contextualText, composerText, consumeComposerOnQueue: true, onAccepted: accepted })
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    expect(h.commands.send.mock.calls[0]![0]).toMatchObject({ kind: 'new-turn', params: { message: contextualText } })
    expect(accepted).not.toHaveBeenCalled()
    accept({ taskId: 'context-task' }); await pending
    expect(accepted).toHaveBeenCalledOnce()
    expect(h.options.inputText.value).toBe('')
  })

  it('restores the editable question after rejection and keeps the context unacknowledged', async () => {
    const h = queuedSendHarness(), accepted = vi.fn(), composerText = h.options.inputText.value
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('rejected', 'refusal', 'REFUSED', false, true))
    await h.api.onSend({ textOverride: contextualText, composerText, consumeComposerOnQueue: true, onAccepted: accepted })
    expect(h.options.inputText.value).toBe(composerText)
    expect(accepted).not.toHaveBeenCalled()
  })

  it('uses the existing composer-consuming queue path only for explicit context submissions', async () => {
    const payload = vi.fn(() => true)
    const h = queuedSendHarness({ enqueuePendingPayload: payload })
    h.options.stream.isStreaming.value = true
    h.options.activeStreamTaskId.value = 'active-task'
    const accepted = vi.fn()
    await h.api.onSend({ textOverride: contextualText, composerText: h.options.inputText.value, consumeComposerOnQueue: true, onAccepted: accepted })
    expect(h.options.enqueuePendingInput).toHaveBeenCalledWith(contextualText, undefined)
    expect(payload).not.toHaveBeenCalled()
    expect(accepted).toHaveBeenCalledOnce()
    expect(h.commands.send).not.toHaveBeenCalled()
  })

  it('still preserves the composer for existing programmatic override calls', async () => {
    const payload = vi.fn(() => true)
    const h = queuedSendHarness({ enqueuePendingPayload: payload })
    h.options.stream.isStreaming.value = true
    h.options.activeStreamTaskId.value = 'active-task'
    await h.api.onSend({ textOverride: contextualText })
    expect(payload).toHaveBeenCalledOnce()
    expect(h.options.enqueuePendingInput).not.toHaveBeenCalled()
  })

  it('does not submit a removed context after asynchronous preparation', async () => {
    let current = true, release!: () => void
    const prepare = new Promise<void>(resolve => { release = resolve })
    const h = queuedSendHarness({ validateActiveProjectBeforeSend: async () => { await prepare; return null } })
    const pending = h.api.onSend({ textOverride: contextualText, preflightGuard: () => current, consumeComposerOnQueue: true })
    current = false; release(); await pending
    expect(h.commands.send).not.toHaveBeenCalled()
    expect(h.options.inputText.value).toBe('unrelated composer draft')
  })
})

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

describe('explicit send scroll ownership', () => {
  it('notifies an explicit UI callback only after durable admission without changing automatic sends', async () => {
    const h = queuedSendHarness(), accepted = vi.fn()
    let accept!: (response: TurnSendResponse) => void
    h.commands.send.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const sending = h.api.onSend({ onAccepted: accepted })
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    expect(accepted).not.toHaveBeenCalled()
    accept({ taskId: 'explicit-task' })
    await sending
    expect(accepted).toHaveBeenCalledOnce()
    h.options.inputText.value = 'automatic follow-up'
    await h.api.onSend()
    expect(accepted).toHaveBeenCalledOnce()
  })

  it('does not notify an explicit UI callback for a rejected send', async () => {
    const h = queuedSendHarness(), accepted = vi.fn()
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('rejected', 'synthetic refusal', 'REFUSED', false, true))
    await h.api.onSend({ onAccepted: accepted })
    expect(accepted).not.toHaveBeenCalled()
  })

  it('retains the original delivery observer when a manual retry replaces its UI callback', async () => {
    const h = queuedSendHarness()
    const delivered = vi.fn(), originalUi = vi.fn(), retryUi = vi.fn()
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('rejected', 'synthetic refusal', 'REFUSED', false, true))
    await h.api.onSend({ onDeliveryAccepted: delivered, onAccepted: originalUi })
    const originalRequestId = h.commands.send.mock.calls[0]![0].params.clientRequestId
    expect(delivered).not.toHaveBeenCalled()
    expect(h.options.inputText.value).toBe('unrelated composer draft')
    await h.api.onSend({ onAccepted: retryUi })
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.commands.send.mock.calls[1]![0].params.clientRequestId).toBe(originalRequestId)
    expect(delivered).toHaveBeenCalledOnce()
    expect(originalUi).not.toHaveBeenCalled()
    expect(retryUi).toHaveBeenCalledOnce()
    h.options.inputText.value = 'a separate later message'
    await h.api.onSend()
    expect(delivered).toHaveBeenCalledOnce()
  })

  it('follows the new user turn even when the stream reducer does not change scroll intent', async () => {
    const h = queuedSendHarness()
    expect(h.options.autoScroll.value).toBe(false)
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.options.messages.value).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'user', text: 'unrelated composer draft' }),
    ]))
    expect(h.options.autoScroll.value).toBe(true)
    expect(h.options.scrollToBottom).toHaveBeenCalled()
  })
})

function savedDelivery(id: string, overrides: Partial<DeliveryWalRecord> = {}): DeliveryWalRecord {
  return {
    schemaVersion: 2, ownerRequestId: id, deliveryIdentity: 'identity-A',
    requestSessionKey: 'agent:main:webchat:test', phase: 'unknown', revision: 1, createdAt: 1, updatedAt: 1,
    request: { kind: 'send', request: { kind: 'new-turn', params: {
      sessionKey: 'agent:main:webchat:test', message: `original ${id}`,
      clientRequestId: id, clientMessageId: `message-${id}`,
    } } },
    ...overrides,
  }
}

describe('explicit continuation past a closed unknown admission', () => {
  function harness(overrides: Partial<UseChatSendOptions> = {}) {
    const taskOwnership = useChatTaskOwnership(false)
    const h = queuedSendHarness({ taskOwnership, canContinueAfterClosedAdmission: () => true, ...overrides })
    h.commands.supportsResolvableSend = () => true
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    h.commands.resolveReceipt.mockResolvedValue({ status: 'not-found' })
    return { ...h, taskOwnership }
  }

  async function restartIdle(h: ReturnType<typeof harness>) {
    h.commands.isAdmissionClosed.mockReturnValue(true)
    const waking = h.options.durableDelivery.wake()
    if (vi.isFakeTimers()) await vi.advanceTimersByTimeAsync(31_000)
    await waking
    h.taskOwnership.applySnapshot({ run_status: 'idle', active_task: null, last_task: null }, true)
    h.options.stream.isStreaming.value = false
    await nextTick()
  }

  it('retains an expired-token attempt and waits for explicit new input after a later policy tick', async () => {
    const attachment = { kind: 'staged' as const, local_id: 1, name: 'notes.txt', mime: 'text/plain', file_uuid: 'original-upload' }
    const h = harness({ pendingAttachments: ref([attachment]),
      draftIds: ref(['original-note']), promptAnnotationSnapshots: () => [closureAnnotation] })
    const oldEpoch = `v2.${'a'.repeat(32)}.100.125`
    let currentEpoch = oldEpoch
    h.commands.getReceiptEpoch = () => currentEpoch
    h.commands.isAdmissionClosed.mockImplementation(() => currentEpoch !== oldEpoch)
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('unavailable',
      'Request admission is closed; its earlier outcome is unknown', 'UNAVAILABLE', null, false))
    await h.api.onSend()
    const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    expect(await h.options.durableDelivery.get(oldId)).toMatchObject({
      phase: 'unknown', request: { request: { acceptanceEpoch: oldEpoch } },
      handoff: { composerText: 'unrelated composer draft', recoveryAttachments: [attachment],
        recoveryPromptAnnotations: [expect.objectContaining({ body: closureAnnotation.body })] },
    })
    currentEpoch = `v2.${'a'.repeat(32)}.125.150`
    await h.options.durableDelivery.wake()
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    h.options.stream.isStreaming.value = false
    await nextTick()
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledOnce()
    h.options.inputText.value = 'An intentional new instruction after the server window advanced.'
    await h.api.onSend({ explicitNewInput: true })
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.commands.send.mock.calls[1]![0]).toMatchObject({ acceptanceEpoch: currentEpoch })
    expect(h.commands.send.mock.calls[1]![0].params.clientRequestId).not.toBe(oldId)
    expect(await h.options.durableDelivery.get(oldId)).toMatchObject({
      phase: 'unknown', request: { request: { acceptanceEpoch: oldEpoch } },
    })
  })

  it.each([false, true])('allows a fresh explicit ID in the original session; pending Stop=%s', async stopped => {
    vi.useFakeTimers()
    const h = harness()
    try {
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    let fail!: (error: Error) => void
    h.commands.send.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
    const first = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    if (stopped) h.api.onStop()
    fail(new TurnCommandError('transport', 'lost response', undefined, null))
    await first
    const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    await restartIdle(h)
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(true)
    expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(false)
    expect(h.api.durableStopPendingForCurrentSession.value).toBe(false)
    h.options.inputText.value = 'explicit new message'
    h.commands.send.mockResolvedValueOnce({ taskId: 'new-task', taskStatus: 'running' })
    await Promise.all([h.api.onSend({ explicitNewInput: true }), h.api.onSend({ explicitNewInput: true })])
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.commands.send.mock.calls[1]![0].params.clientRequestId).not.toBe(oldId)
    expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
    expect(h.commands.cancel).not.toHaveBeenCalled()
    expect(h.options.enqueuePendingInput).not.toHaveBeenCalled()
    const record = await h.options.pendingInputWal!.getDelivery!(oldId)
    expect(record?.phase).toBe('unknown')
    expect(record?.retired).not.toBe(true)
    if (stopped) expect(record?.stop).toMatchObject({ requested: true })
    h.taskOwnership.noteTerminal('new-task')
    h.options.stream.isStreaming.value = false
    h.options.inputText.value = 'automatic draft'
    await h.api.onSend()
    await h.api.sendQueuedFollowup({ pendingUiId: 'queued', text: 'queued content', attachments: [], intent: null }, h.scope.sessionKey)
    const waking = h.options.durableDelivery.wake()
    await vi.advanceTimersByTimeAsync(31_000)
    await waking
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })

  it.each(['no-proof', 'hydrating', 'running', 'queued', 'additional-work', 'blocked'] as const)(
    'keeps the normal admission gate for %s', async gate => {
      const h = harness()
      const wal = h.options.pendingInputWal!
      await wal.prepareDelivery!(savedDelivery('old', { deliveryIdentity: h.scope.deliveryIdentity,
        schemaVersion: 3 }))
      await restartIdle(h)
      if (gate === 'no-proof') { h.commands.isAdmissionClosed.mockReturnValue(false); await h.options.durableDelivery.wake() }
      if (gate === 'hydrating') h.taskOwnership.beginHydration()
      if (gate === 'running') h.taskOwnership.noteRunning('still-running')
      if (gate === 'queued') h.taskOwnership.noteQueued('still-queued')
      if (gate === 'additional-work') h.options.canContinueAfterClosedAdmission = () => false
      if (gate === 'blocked') h.options.sendBlockedReason = ref('blocked')
      await h.api.onSend({ explicitNewInput: true })
      expect(h.commands.send).not.toHaveBeenCalled()
    },
  )

  it.each([false, true])('does not mistake its own stream projection for authoritative work; fork=%s', async fork => {
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const messages = ref<ChatMessage[]>([])
    const projectedStatus = ref('idle')
    const stream = useChatStream({ messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(false),
      applySessionRunState: source => { projectedStatus.value = String(source?.run_status || 'idle') },
      renderMarkdown: text => text, stripDirectiveTags: text => text,
      stripGeneratedArtifactMarkers: text => text, scrollToBottom: vi.fn() })
    const ownerContext = ref<UseChatSendOptions['pendingQueueOwnerContext']['value']>(null)
    const h = harness({ messages, stream, pendingQueueOwnerContext: ownerContext,
      pendingForkBeforeMessageId: ref(fork ? 'new-fork-anchor' : null),
      canContinueAfterClosedAdmission: afterStartingStream => Boolean(afterStartingStream
        || (projectedStatus.value === 'idle' && !ownerContext.value)),
    })
    try {
      await h.options.pendingInputWal!.prepareDelivery!(savedDelivery('old', {
        deliveryIdentity: h.scope.deliveryIdentity, schemaVersion: 3,
      }))
      await restartIdle(h)
      h.commands.send.mockResolvedValueOnce({ taskId: 'new-task', taskStatus: 'running' })
      await h.api.onSend({ explicitNewInput: true })
      expect(projectedStatus.value).toBe('running')
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
      expect(h.api.sendPending.value).toBe(false)
    } finally { h.api.dispose(); h.options.durableDelivery.dispose(); stream.cleanup(); vi.unstubAllGlobals() }
  })

  it.each([[false, false], [true, false], [false, true], [true, true]])(
    'does not assign a new ID to an unchanged unknown draft; reload=%s, annotations=%s', async (reload, annotations) => {
    const draft: Partial<UseChatSendOptions> = annotations ? {
      inputText: ref(''), draftIds: ref([closureAnnotation.annotationId]),
      promptAnnotationSnapshots: () => [closureAnnotation],
    } : {}
    const original = harness(draft)
    original.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    original.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    await original.api.onSend()
    const oldId = original.commands.send.mock.calls[0]![0].params.clientRequestId!
    let h = original
    if (reload) {
      original.api.dispose(); original.options.durableDelivery.dispose()
      h = harness({ ...draft, pendingInputWal: original.options.pendingInputWal })
    }
    await restartIdle(h)
    h.options.inputText.value = annotations ? '' : 'unrelated composer draft'
    await h.api.onSend({ explicitNewInput: true })
    expect(h.commands.send).toHaveBeenCalledTimes(reload ? 0 : 1)
    expect((await h.options.pendingInputWal!.getDelivery!(oldId))?.phase).toBe('unknown')
    expect(h.options.inputText.value).toBe(annotations ? '' : 'unrelated composer draft')
  })

  it('rechecks continuation permission after asynchronous WAL preparation', async () => {
    const h = harness()
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    await h.api.onSend()
    await restartIdle(h)
    const wal = h.options.pendingInputWal!
    const prepare = wal.prepareDelivery!
    let release!: () => void
    const blocked = new Promise<void>(resolve => { release = resolve })
    const preparing = vi.fn(async (...args: Parameters<typeof prepare>) => { await blocked; return prepare(...args) })
    wal.prepareDelivery = preparing
    h.options.inputText.value = 'new explicit input'
    const sending = h.api.onSend({ explicitNewInput: true })
    await vi.waitFor(() => expect(preparing).toHaveBeenCalledOnce())
    h.options.canContinueAfterClosedAdmission = () => false
    release()
    await sending
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.options.inputText.value).toBe('new explicit input')
  })

  it.each(['default', 'plan'] as const)('does not resend an unchanged first-task draft in %s mode', async mode => {
    const h = harness({ pendingSessionIntent: ref('new_chat'), initialCollaborationMode: ref(mode) })
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    await h.api.onSend()
    await restartIdle(h)
    h.options.inputText.value = 'unrelated composer draft'
    await h.api.onSend({ explicitNewInput: true })
    expect(h.commands.send).toHaveBeenCalledOnce()
  })

  it.each(['none', 'before-new', 'after-new'])('keeps a late fork receipt in the background; reload=%s', async reload => {
    const original = harness({ pendingForkBeforeMessageId: ref('fork-anchor') })
    original.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    original.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    await original.api.onSend()
    const oldId = original.commands.send.mock.calls[0]![0].params.clientRequestId!
    let h = original
    if (reload === 'before-new') {
      original.api.dispose(); original.options.durableDelivery.dispose()
      h = harness({ pendingInputWal: original.options.pendingInputWal })
    }
    await restartIdle(h)
    h.options.pendingForkBeforeMessageId.value = null
    h.options.inputText.value = 'new input in original session'
    h.commands.send.mockResolvedValueOnce({ taskId: 'new-task', taskStatus: 'running' })
    await h.api.onSend({ explicitNewInput: true })
    expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
    expect((await h.options.pendingInputWal!.getDelivery!(oldId))?.handoff?.backgroundOnly).toBe(true)
    if (reload === 'after-new') {
      const wal = h.options.pendingInputWal
      h.api.dispose(); h.options.durableDelivery.dispose()
      h = harness({ pendingInputWal: wal })
      h.taskOwnership.noteRunning('new-task')
      h.commands.isAdmissionClosed.mockReturnValue(true)
      await h.options.durableDelivery.wake()
      await h.api.recoverResponseHandoffs()
    }
    h.options.inputText.value = 'newer draft'
    h.commands.resolveReceipt.mockResolvedValueOnce({ status: 'found',
      response: { taskId: 'old-task', taskStatus: 'running', sessionKey: 'child-session' } })
    await h.api.checkDelivery(oldId)
    await flushDelivery()
    expect(h.options.adoptResponseSession).not.toHaveBeenCalled()
    expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
    expect(h.options.inputText.value).toBe('newer draft')
    expect(h.options.sessionKey.value).toBe(h.scope.sessionKey)
  })

  it('keeps new input editable if releasing old composer ownership cannot be persisted', async () => {
    const h = harness()
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    await h.api.onSend()
    await restartIdle(h)
    h.options.inputText.value = 'new input'
    const detach = vi.spyOn(h.options.durableDelivery, 'detachComposer').mockResolvedValueOnce(false)
    await h.api.onSend({ explicitNewInput: true })
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.options.inputText.value).toBe('new input')
    detach.mockRestore()
    await h.api.onSend({ explicitNewInput: true })
    expect(h.commands.send).toHaveBeenCalledTimes(2)
  })

  it.each(['accepted', 'unknown', 'not-sent'] as const)(
    'honors a peer detach while the original %s result is awaiting WAL persistence', async outcome => {
      const h = harness()
      h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
      const wal = h.options.pendingInputWal!
      const cas = wal.compareAndSwapDelivery!
      let release!: () => void
      const blocked = new Promise<void>(resolve => { release = resolve })
      let waiting = false
      wal.compareAndSwapDelivery = async (id, revision, record) => {
        if (!waiting && record?.phase === outcome) { waiting = true; await blocked }
        return cas(id, revision, record)
      }
      if (outcome === 'accepted') h.commands.send.mockResolvedValueOnce({ taskId: 'old-task', taskStatus: 'running' })
      else h.commands.send.mockRejectedValueOnce(new TurnCommandError(
        'transport', 'delayed original result', undefined, outcome === 'not-sent' ? false : null))
      const accepted = vi.fn()
      const sending = h.api.onSend({ onAccepted: accepted })
      await vi.waitFor(() => expect(waiting).toBe(true))
      const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
      const peer = harness({ pendingInputWal: wal })
      peer.commands.isAdmissionClosed.mockReturnValue(true)
      expect(await peer.options.durableDelivery.detachComposer(oldId)).toBe(true)
      h.taskOwnership.noteRunning('new-task')
      h.options.activeStreamTaskId.value = 'new-task'
      h.options.inputText.value = 'newer draft'
      release()
      await sending
      expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
      expect(h.options.activeStreamTaskId.value).toBe('new-task')
      expect(h.options.inputText.value).toBe('newer draft')
      expect(accepted).not.toHaveBeenCalled()
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.commands.cancel).not.toHaveBeenCalled()
    },
  )

  it('does not let a delayed old running receipt steal the new task or consume its draft', async () => {
    const h = harness()
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    let receipt!: (response: TurnReceiptResult) => void
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    await h.api.onSend()
    const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    await restartIdle(h)
    h.commands.resolveReceipt.mockImplementationOnce(() => new Promise(resolve => { receipt = resolve }))
    const checking = h.api.checkDelivery(oldId)
    await vi.waitFor(() => expect(h.commands.resolveReceipt).toHaveBeenCalledOnce())
    h.options.inputText.value = 'new explicit message'
    h.commands.send.mockResolvedValueOnce({ taskId: 'new-task', taskStatus: 'running' })
    await h.api.onSend({ explicitNewInput: true })
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    h.options.inputText.value = 'next draft untouched'
    receipt({ status: 'found', response: { taskId: 'old-task', taskStatus: 'running' } })
    await checking
    await flushDelivery()
    expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
    expect(h.options.inputText.value).toBe('next draft untouched')
    expect((await h.options.pendingInputWal!.getDelivery!(oldId))?.phase).toBe('accepted')
    expect(h.commands.cancel).not.toHaveBeenCalled()
  })

  it.each(['found', 'accepted-unavailable'] as const)('keeps a peer-detached %s receipt history-only', async status => {
    const h = harness()
    h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'lost response', undefined, null))
    const accepted = vi.fn()
    await h.api.onSend({ onAccepted: accepted })
    const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    await restartIdle(h)
    const peer = harness({ pendingInputWal: h.options.pendingInputWal })
    peer.commands.isAdmissionClosed.mockReturnValue(true)
    expect(await peer.options.durableDelivery.detachComposer(oldId)).toBe(true)
    h.taskOwnership.noteRunning('new-task')
    h.options.inputText.value = 'newer draft'
    h.commands.resolveReceipt.mockResolvedValueOnce(status === 'found'
      ? { status, response: { taskId: 'old-task', taskStatus: 'running' } } : { status })
    await h.api.checkDelivery(oldId)
    await flushDelivery()
    expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
    expect(h.options.inputText.value).toBe('newer draft')
    expect(accepted).not.toHaveBeenCalled()
  })

  it('clears the old local Stop latch when its exact durable cancellation finishes', async () => {
    vi.useFakeTimers()
    const h = harness()
    try {
      h.taskOwnership.applySnapshot({ run_status: 'idle' }, true)
      let fail!: (error: Error) => void
      h.commands.send.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
      const first = h.api.onSend()
      await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
      h.api.onStop()
      fail(new TurnCommandError('transport', 'lost response', undefined, null))
      await first
      const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
      await restartIdle(h)
      h.options.inputText.value = 'new input'
      h.commands.send.mockResolvedValueOnce({ taskId: 'new-task', taskStatus: 'running' })
      await h.api.onSend({ explicitNewInput: true })
      h.commands.resolveReceipt.mockResolvedValue({ status: 'found',
        response: { taskId: 'old-task', taskStatus: 'running' } })
      const checking = h.api.checkDelivery(oldId)
      await vi.advanceTimersByTimeAsync(31_000)
      await checking
      expect(h.commands.cancel).toHaveBeenCalledOnce()
      expect((await h.options.pendingInputWal!.getDelivery!(oldId))?.stop?.completed).toBe(true)
      expect(h.taskOwnership.runningTaskId.value).toBe('new-task')
      expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
      h.taskOwnership.noteTerminal('new-task')
      h.options.stream.isStreaming.value = false
      h.options.inputText.value = 'next message'
      await h.api.onSend({ explicitNewInput: true })
      expect(h.commands.send).toHaveBeenCalledTimes(3)
    } finally { h.options.durableDelivery.dispose(); vi.useRealTimers() }
  })
})

describe('current identity delivery recovery', () => {
  it.each([
    ['unknown', false], ['unknown', true], ['rejected', false], ['rejected', true],
  ] as const)('keeps a foreign %s draft isolated while allowing offline new content: changed=%s', async (acceptance, changed) => {
    const identity = ref<string | null>('identity-A')
    const offlineIdentity = ref<string | null>(null)
    const h = queuedSendHarness({ deliveryIdentity: identity, offlineQueueIdentity: offlineIdentity })
    const originalText = h.options.inputText.value
    h.commands.send.mockRejectedValueOnce(new TurnCommandError(
      acceptance === 'unknown' ? 'transport' : 'rejected', 'synthetic original failure', undefined,
      acceptance === 'unknown' ? null : false, true,
    ))
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    await h.api.onSend()
    await h.options.durableDelivery.wake()
    const originalId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    const originalRecord = await h.options.pendingInputWal!.getDelivery!(originalId)
    identity.value = 'identity-B'
    offlineIdentity.value = 'identity-B'
    h.options.inputText.value = changed ? 'new offline message for B' : originalText
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.commands.steer).not.toHaveBeenCalled()
    expect(h.commands.cancel).not.toHaveBeenCalled()
    expect(h.options.enqueuePendingInput).toHaveBeenCalledTimes(changed ? 1 : 0)
    if (changed) expect(h.options.enqueuePendingInput).toHaveBeenCalledWith('new offline message for B', undefined, {
      deliveryIdentity: 'identity-B',
    })
    expect(await h.options.pendingInputWal!.getDelivery!(originalId)).toEqual(originalRecord)
  })

  it('keeps an in-memory stopped acceptance scoped to its identity across a same-key switch', async () => {
    const identity = ref<string | null>('identity-A')
    const acceptanceStopPending = ref(false)
    const h = queuedSendHarness({ deliveryIdentity: identity, acceptanceStopPending })
    let reject!: (error: Error) => void
    h.commands.send.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    const sending = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    h.api.onStop()
    reject(new TurnCommandError('transport', 'synthetic ACK loss', undefined, null))
    await sending
    await h.options.durableDelivery.wake()
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(true)
    identity.value = 'identity-B'
    await nextTick()
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
    expect(h.api.durableStopPendingForCurrentSession.value).toBe(false)
    expect(acceptanceStopPending.value).toBe(false)
    h.options.inputText.value = 'new identity message'
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.options.enqueuePendingInput).not.toHaveBeenCalled()
    identity.value = 'identity-A'
    await nextTick()
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(true)
  })

  it.each(['unknown', 'stop'] as const)('does not let a foreign %s block the same session and restores its projection on switch back', async state => {
    const wal = memoryDeliveryWal()
    const record = savedDelivery('original', state === 'stop'
      ? { phase: 'accepted', response: { taskId: 'original-task' }, stop: { requested: true } } : {})
    await wal.prepareDelivery!(record)
    const identity = ref<string | null>('identity-B')
    const h = queuedSendHarness({ pendingInputWal: wal, deliveryIdentity: identity })
    await h.options.durableDelivery.wake()
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
    expect(h.api.durableStopPendingForCurrentSession.value).toBe(false)
    expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(false)
    expect(h.api.deliveryRecoveryEntries.value).toEqual([])
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.options.enqueuePendingInput).not.toHaveBeenCalled()
    expect(h.commands.lookupReceipt).not.toHaveBeenCalled()
    expect(await wal.getDelivery!('original')).toEqual(record)

    identity.value = 'identity-A'
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(true)
    expect(h.api.durableStopPendingForCurrentSession.value).toBe(state === 'stop')
    expect(h.api.acceptanceStopAvailableForCurrentSession.value).toBe(state === 'unknown')
    expect(h.api.deliveryRecoveryEntries.value.map(item => item.id)).toEqual(['original'])
    identity.value = null
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
    expect(h.api.durableStopPendingForCurrentSession.value).toBe(false)
    expect(h.api.deliveryRecoveryEntries.value).toEqual([])
    identity.value = 'identity-B'
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
  })

  it.each(['budget', 'authority'] as const)('checks a saved %s request after remount without resending it', async reason => {
    vi.useFakeTimers()
    try {
      const wal = memoryDeliveryWal()
      await wal.prepareDelivery!(savedDelivery('original', reason === 'authority' ? { paused: 'authority' } : {}))
      const h = queuedSendHarness({ pendingInputWal: wal, deliveryIdentity: ref('identity-A') })
      const recovery = h.options.durableDelivery.wake()
      await vi.advanceTimersByTimeAsync(6_000)
      await recovery
      const initialReads = h.commands.lookupReceipt.mock.calls.length
      expect(initialReads).toBe(reason === 'budget' ? 4 : 0)
      expect(h.api.deliveryRecoveryEntries.value[0]?.waitReason).toBe(reason === 'budget' ? 'budget' : 'permission')
      await h.api.onSend()
      expect(h.options.enqueuePendingInput).toHaveBeenCalledOnce()
      expect(h.commands.send).not.toHaveBeenCalled()
      h.commands.lookupReceipt.mockResolvedValue({ status: 'found', response: {
        taskId: 'original-task', sessionKey: h.scope.sessionKey, taskStatus: 'succeeded',
      } })
      await h.api.checkDelivery('original')
      expect(h.commands.lookupReceipt).toHaveBeenCalledTimes(initialReads + 1)
      expect(h.commands.send).not.toHaveBeenCalled()
      expect(h.commands.steer).not.toHaveBeenCalled()
      expect(h.commands.cancel).not.toHaveBeenCalled()
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
      expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
      expect((await wal.getDelivery!('original'))?.phase).toBe('accepted')
    } finally { vi.useRealTimers() }
  })

  it('coalesces duplicate checks and hides in-flight checks when identity or session changes', async () => {
    const wal = memoryDeliveryWal()
    await wal.prepareDelivery!(savedDelivery('first', { paused: 'authority' }))
    await wal.prepareDelivery!(savedDelivery('second', { paused: 'authority' }))
    const identity = ref<string | null>('identity-A')
    const h = queuedSendHarness({ pendingInputWal: wal, deliveryIdentity: identity })
    await h.options.durableDelivery.wake()
    let finish!: (result: TurnReceiptResult) => void
    h.commands.lookupReceipt.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const checking = h.api.checkDelivery('first')
    await h.api.checkDelivery('first')
    await vi.waitFor(() => expect(h.commands.lookupReceipt).toHaveBeenCalledOnce())
    expect(h.api.checkingDeliveryIds.value).toEqual(['first'])
    expect(h.api.deliveryRecoveryEntries.value.map(item => item.id)).toEqual(['first', 'second'])
    identity.value = 'identity-B'
    await nextTick()
    expect(h.api.deliveryRecoveryEntries.value).toEqual([])
    expect(h.api.checkingDeliveryIds.value).toEqual([])
    await h.api.checkDelivery('second')
    expect(h.commands.lookupReceipt).toHaveBeenCalledOnce()
    finish({ status: 'found', response: { taskId: 'original-task', taskStatus: 'succeeded' } })
    await checking
    expect(h.commands.send).not.toHaveBeenCalled()
    identity.value = 'identity-A'
    h.options.sessionKey.value = 'another-session'
    expect(h.api.deliveryRecoveryEntries.value).toEqual([])
    await h.api.checkDelivery('second')
    expect(h.commands.lookupReceipt).toHaveBeenCalledOnce()
    h.options.sessionKey.value = h.scope.sessionKey
    h.commands.lookupReceipt.mockResolvedValue({ status: 'found', response: { taskId: 'second-task', taskStatus: 'succeeded' } })
    await h.api.checkDelivery('second')
    expect(h.commands.lookupReceipt).toHaveBeenCalledTimes(2)
    expect(h.commands.send).not.toHaveBeenCalled()
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
    const accepted = vi.fn()
    const h = queuedSendHarness({ enqueuePendingInput: enqueue, capturePendingAutoSendResume: capture,
      ...(mode === 'offline' ? { offlineQueueIdentity: ref('synthetic-identity') } : {}),
    })
    h.options.stream.isStreaming.value = mode === 'busy'
    const sending = h.api.onSend({ resumeQueueOnSuccess: true, onAccepted: accepted })
    await vi.waitFor(() => expect(enqueue).toHaveBeenCalledOnce())
    expect(capture).toHaveBeenCalledOnce()
    expect(resume).not.toHaveBeenCalled()
    expect(accepted).not.toHaveBeenCalled()
    save(true)
    await sending
    expect(resume).toHaveBeenCalledOnce()
    expect(accepted).toHaveBeenCalledOnce()
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
    const accepted = vi.fn()
    const h = queuedSendHarness({ capturePendingAutoSendResume: () => resume,
      busySendMode: ref('steer'), activeStreamTaskId: ref('running-task'),
      activeSteerCapability: ref({ mode: 'same_turn', expected_turn_id: 'running-task', input_kinds: ['text'] }),
      enqueuePendingSteerAttempt: payload => ({ pendingUiId: 'steer-C', text: payload.request.message,
        attachments: [], intent: null, steerAttempt: { request: payload.request, phase: payload.phase || 'submitting' } }),
    })
    h.options.stream.isStreaming.value = true
    await h.api.onSend({ resumeQueueOnSuccess: true, onAccepted: accepted })
    expect(h.commands.steer).toHaveBeenCalledOnce()
    expect(h.commands.send).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled()
    expect(accepted).toHaveBeenCalledOnce()
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
