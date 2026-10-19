import { describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import type { ChatMessage, ChatPendingItem } from '@/types/chat'
import { TurnCommandError, type TurnSendRequest, type TurnSendResponse, type TurnReceiptResult, type TurnCommandRequestOptions } from '@/modules/turnCommands'
import { createPendingQueuePolicy } from '@/utils/chat/pendingQueuePolicy'
import { decideSendResponseSession, type UseChatSendOptions } from './useChatSend'
import { createChatSendHarness, memoryDeliveryWal } from './chatSendTestHarness'
import { useChatSteerDelivery } from './useChatSteerDelivery'
import type { DeliveryWalRecord } from '@/utils/chat/pendingInputWal'
import { acceptsVisualizeContext, useVisualizeDraft } from '@/visualize/useVisualizeDraft'
import type { VisualizeReceipt } from '@/visualize/types'
import type { SlashCommandClassification } from './useChatSlashCommands'

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
    send: vi.fn(async (_request: TurnSendRequest, _options?: TurnCommandRequestOptions): Promise<TurnSendResponse> => ({ taskId: 'synthetic-next-task' })),
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

describe('ordinary send recovery stays separate from rendering', () => {
  it.each(['project-init', 'plan-continuation'] as const)(
    'keeps %s intent while ending admission feedback before handoff cleanup', async kind => {
      const command = { name: 'project.init' as const, args: 'preserve guidance' }
      const composerText = kind === 'project-init' ? '/init preserve guidance' : '/plan /init is literal prompt text'
      const h = queuedSendHarness({
        inputText: ref(composerText), commandText: ref(composerText),
        classifySlashCommand: vi.fn(async () => 'registered' as const),
        resolveTurnCommand: vi.fn(() => kind === 'project-init' ? command : null),
        supportsTurnCommand: vi.fn(async () => true),
        executeSlashCommand: vi.fn(async () => ({
          kind: 'composer-prompt' as const, prompt: '/init is literal prompt text', composerText,
        })),
      })
      let accept!: (response: TurnSendResponse) => void
      let finishCleanup!: () => void
      h.commands.send.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
      const remove = h.options.pendingInputWal!.deleteHandoff!
      const cleanup = vi.fn(async (id: string) => {
        await new Promise<void>(resolve => { finishCleanup = resolve })
        await remove(id)
      })
      h.options.pendingInputWal!.deleteHandoff = cleanup
      const sending = h.api.onSend()
      await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
      expect(h.api.sendAcceptancePending.value).toBe(true)
      await h.api.onSend()
      expect(h.commands.send).toHaveBeenCalledOnce()
      const request = h.commands.send.mock.calls[0]![0]
      if (request.kind !== 'new-turn') throw new Error('Expected an ordinary turn admission')
      expect(request.params.message).toBe(kind === 'project-init' ? composerText : '/init is literal prompt text')
      if (kind === 'project-init') {
        expect(request.params.turnCommand).toEqual(command)
        expect(h.options.executeSlashCommand).not.toHaveBeenCalled()
      } else {
        expect(request.params).not.toHaveProperty('turnCommand')
        expect(h.options.executeSlashCommand).toHaveBeenCalledOnce()
      }
      h.options.inputText.value = 'new draft typed before acceptance'
      accept({ taskId: 'accepted-command-task', taskStatus: 'running', sessionKey: h.scope.sessionKey })
      await vi.waitFor(() => expect(cleanup).toHaveBeenCalledOnce())
      expect(h.api.sendAcceptancePending.value).toBe(false)
      expect(h.api.sendPending.value).toBe(true)
      expect(h.options.inputText.value).toBe('new draft typed before acceptance')
      expect(h.options.activeStreamTaskId.value).toBe('accepted-command-task')
      finishCleanup()
      await sending
      expect(h.api.sendPending.value).toBe(false)
      expect(h.commands.send).toHaveBeenCalledOnce()
    },
  )

  it('bounds a lost init ACK and checks its immutable receipt after the command catalog disappears', async () => {
    vi.useFakeTimers()
    pushToast.mockClear()
    const command = { name: 'project.init' as const, args: 'preserve guidance' }
    let supported = true
    const h = queuedSendHarness({
      inputText: ref('/init preserve guidance'),
      classifySlashCommand: vi.fn(async () => 'registered' as const),
      resolveTurnCommand: vi.fn(() => supported ? command : null),
      supportsTurnCommand: vi.fn(async () => supported),
    })
    h.commands.send.mockImplementationOnce((_request, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener('abort', () => reject(new TurnCommandError(
        'aborted', 'local RPC wait ended', 'RPC_ABORTED',
      )), { once: true })
    }))
    try {
      const sending = h.api.onSend()
      await vi.advanceTimersByTimeAsync(0)
      expect(h.commands.send).toHaveBeenCalledOnce()
      const original = structuredClone(h.commands.send.mock.calls[0]![0])
      if (original.kind !== 'new-turn') throw new Error('Expected an ordinary turn admission')
      supported = false
      h.options.inputText.value = 'newer draft'
      await vi.advanceTimersByTimeAsync(30_000)
      await sending
      expect(h.api.sendAcceptancePending.value).toBe(false)
      await vi.advanceTimersByTimeAsync(6_000)
      await h.options.durableDelivery.wake()
      expect(h.commands.lookupReceipt).toHaveBeenCalledTimes(4)
      expect(h.api.deliveryRecoveryEntries.value).toEqual([
        expect.objectContaining({ phase: 'unknown', waitReason: 'budget' }),
      ])
      h.commands.lookupReceipt.mockResolvedValue({ status: 'found', response: {
        taskId: 'accepted-init-task', taskStatus: 'running', sessionKey: h.scope.sessionKey,
      } })
      await h.api.checkDelivery(h.api.deliveryRecoveryEntries.value[0]!.id)
      expect(h.commands.lookupReceipt).toHaveBeenLastCalledWith(
        { kind: 'send', request: original }, expect.objectContaining({ signal: expect.anything() }),
      )
      expect(original.params.turnCommand).toEqual(command)
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.commands.cancel).not.toHaveBeenCalled()
      expect(h.options.supportsTurnCommand).not.toHaveBeenCalled()
      expect(h.options.inputText.value).toBe('newer draft')
      expect(h.api.deliveryRecoveryEntries.value).toEqual([])
      expect(pushToast).not.toHaveBeenCalled()
    } finally {
      h.options.durableDelivery.dispose()
      vi.useRealTimers()
    }
  })

  it.each([false, true])('ends admission feedback on ACK while retaining the transaction lock through handoff cleanup (retry=%s)', async retry => {
    const h = queuedSendHarness()
    if (retry) {
      h.commands.send.mockRejectedValueOnce(new TurnCommandError('rejected', 'synthetic refusal', 'REFUSED', false, true))
      await h.api.onSend()
      h.commands.send.mockClear()
    }
    let accept!: (response: TurnSendResponse) => void
    let finishCleanup!: () => void
    h.commands.send.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const remove = h.options.pendingInputWal!.deleteHandoff!
    const cleanup = vi.fn(async (id: string) => {
      await new Promise<void>(resolve => { finishCleanup = resolve })
      await remove(id)
    })
    h.options.pendingInputWal!.deleteHandoff = cleanup
    const sending = h.api.onSend()
    expect(h.api.sendPending.value).toBe(true)
    expect(h.api.sendAcceptancePending.value).toBe(true)
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledOnce()
    accept({ taskId: 'accepted-task', taskStatus: 'running', sessionKey: h.scope.sessionKey })
    await vi.waitFor(() => expect(cleanup).toHaveBeenCalledOnce())
    expect(h.api.sendAcceptancePending.value).toBe(false)
    expect(h.api.sendPending.value).toBe(true)
    expect(h.options.activeStreamTaskId.value).toBe('accepted-task')
    finishCleanup()
    await sending
    expect(h.api.sendPending.value).toBe(false)
    expect(h.commands.send).toHaveBeenCalledOnce()
  })

  it('ends the button wait after a lost admission deadline and preserves receipt-only recovery without notices', async () => {
    vi.useFakeTimers()
    pushToast.mockClear()
    const h = queuedSendHarness()
    h.commands.send.mockImplementationOnce((_request, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener('abort', () => reject(new TurnCommandError(
        'aborted', 'local RPC wait ended', 'RPC_ABORTED',
      )), { once: true })
    }))
    try {
      const sending = h.api.onSend()
      await vi.advanceTimersByTimeAsync(0)
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.api.sendAcceptancePending.value).toBe(true)
      await h.api.onSend()
      await vi.advanceTimersByTimeAsync(30_000)
      await sending
      expect(h.api.sendAcceptancePending.value).toBe(false)
      expect(h.api.sendPending.value).toBe(false)
      expect(h.options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(6_000)
      await h.options.durableDelivery.wake()
      expect(h.api.deliveryRecoveryEntries.value).toEqual([
        expect.objectContaining({ phase: 'unknown', waitReason: 'budget' }),
      ])
      expect(h.commands.lookupReceipt).toHaveBeenCalledTimes(4)
      expect(h.commands.send).toHaveBeenCalledOnce()
      expect(h.commands.cancel).not.toHaveBeenCalled()
      expect(pushToast).not.toHaveBeenCalled()
      expect(h.options.messages.value.filter(message => message.role === 'error')).toEqual([])
    } finally {
      h.options.durableDelivery.dispose()
      vi.useRealTimers()
    }
  })

  it('scopes pending admission feedback to the current session and delivery identity', async () => {
    const deliveryIdentity = ref('synthetic-identity')
    const h = queuedSendHarness({ deliveryIdentity })
    let accept!: (response: TurnSendResponse) => void
    h.commands.send.mockImplementationOnce(() => new Promise(resolve => { accept = resolve }))
    const sending = h.api.onSend()
    await vi.waitFor(() => expect(h.commands.send).toHaveBeenCalledOnce())
    h.options.sessionKey.value = 'another-session'
    expect(h.api.sendAcceptancePending.value).toBe(false)
    h.options.sessionKey.value = h.scope.sessionKey
    expect(h.api.sendAcceptancePending.value).toBe(true)
    deliveryIdentity.value = 'another-owner'
    expect(h.api.sendAcceptancePending.value).toBe(false)
    deliveryIdentity.value = h.scope.deliveryIdentity
    expect(h.api.sendAcceptancePending.value).toBe(true)
    accept({ taskId: 'original-task', sessionKey: h.scope.sessionKey })
    await sending
    expect(h.api.sendAcceptancePending.value).toBe(false)
    expect(h.commands.send).toHaveBeenCalledOnce()
  })

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
      expect(h.api.sendAcceptancePending.value).toBe(false)
      expect(h.api.sendPending.value).toBe(false)
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

describe('current identity delivery recovery', () => {
  it('uses a fresh identity after a workspace rejection carries a durable retirement proof', async () => {
    const h = queuedSendHarness({ inputText: ref('workspace draft') })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError(
      'rejected', 'Workspace unavailable', 'WORKSPACE_UNAVAILABLE', false, false,
      undefined, { reason: 'unavailable', requestRetired: true },
    ))
    await h.api.onSend()
    const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    expect(h.options.inputText.value).toBe('workspace draft')
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
    expect(h.commands.lookupReceipt).not.toHaveBeenCalled()
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.commands.send.mock.calls[1]![0].params.clientRequestId).not.toBe(oldId)
    expect(await h.options.pendingInputWal!.getDelivery!(oldId)).toMatchObject({ phase: 'not-sent', retired: true })
  })

  it.each([false, true])('releases a server-retired unknown without resending or changing a newer draft (%s)', async newerDraft => {
    pushToast.mockClear()
    const attachment = { kind: 'inline' as const, local_id: 71, name: 'draft.txt', mime: 'text/plain', data: 'b3JpZ2luYWw=' }
    const h = queuedSendHarness({ inputText: ref('original draft'), pendingAttachments: ref([attachment]) })
    const resolveReceipt = vi.fn(async (): Promise<TurnReceiptResult> => ({ status: 'retired' }))
    Object.assign(h.commands, { resolveReceipt })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'ACK lost', undefined, null, true))
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    await h.api.onSend()
    await h.options.durableDelivery.wake()
    const original = h.commands.send.mock.calls[0]![0]
    const originalId = original.params.clientRequestId!
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(true)
    if (newerDraft) h.options.inputText.value = 'newer draft'
    await h.api.checkDelivery(originalId)
    expect(resolveReceipt).toHaveBeenCalledOnce()
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.commands.cancel).not.toHaveBeenCalled()
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
    expect(h.api.sendPending.value).toBe(false)
    expect(h.api.sendAcceptancePending.value).toBe(false)
    expect(h.options.inputText.value).toBe(newerDraft ? 'newer draft' : 'original draft')
    expect(h.options.pendingAttachments.value).toEqual(newerDraft ? [] : [attachment])
    expect(await h.options.pendingInputWal!.getDelivery!(originalId)).toMatchObject({ phase: 'not-sent', retired: true })
    expect(pushToast).not.toHaveBeenCalled()
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    const nextRequest = h.commands.send.mock.calls[1]![0]
    if (nextRequest.kind !== 'new-turn') throw new Error('Expected a deliberate fresh send')
    expect(nextRequest.params.clientRequestId).not.toBe(originalId)
    expect(nextRequest.params.message).toBe(newerDraft ? 'newer draft' : 'original draft')
  })

  it.each([false, true])('restores a retired handoff after remount with its payload, preRetired=%s', async preRetired => {
    pushToast.mockClear()
    const wal = memoryDeliveryWal()
    const record = savedDelivery('retired-handoff')
    if (record.request?.kind !== 'send' || record.request.request.kind !== 'new-turn') throw new Error('expected send')
    const params = record.request.request.params
    params.intent = 'new_chat'
    params.workspaceId = 'selected-project'
    params.forkBeforeMessageId = 'fork-anchor'
    params.attachments = [{ type: 'text/plain', mime: 'text/plain', name: 'saved.txt', data: 'c2F2ZWQ=' }]
    record.handoff = { schemaVersion: 1, ownerRequestId: record.ownerRequestId,
      requestSessionKey: record.requestSessionKey, clientRequestId: record.ownerRequestId,
      clientMessageId: 'message-retired-handoff', params, composerText: 'saved composer',
      recoveryAttachments: [], state: 'submitting', createdAt: 1, updatedAt: 1 }
    if (preRetired) { record.phase = 'not-sent'; record.retired = true }
    await wal.prepareDelivery!(record)
    const h = queuedSendHarness({ pendingInputWal: wal, deliveryIdentity: ref('identity-A'), inputText: ref(''),
      pendingWorkspaceId: ref<string | null>(null) })
    const resolveReceipt = vi.fn(async (): Promise<TurnReceiptResult> => ({ status: 'retired' }))
    Object.assign(h.commands, { resolveReceipt })
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    await h.options.durableDelivery.wake()
    if (!preRetired) await h.api.checkDelivery(record.ownerRequestId)
    await h.api.recoverResponseHandoffs()
    expect(h.options.inputText.value).toBe('saved composer')
    expect(h.options.pendingAttachments.value).toEqual([expect.objectContaining({ kind: 'inline', name: 'saved.txt', data: 'c2F2ZWQ=' })])
    expect(h.options.pendingSessionIntent.value).toBe('new_chat')
    expect(h.options.pendingWorkspaceId!.value).toBe('selected-project')
    expect(h.options.pendingForkBeforeMessageId.value).toBe('fork-anchor')
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
    expect(h.commands.send).not.toHaveBeenCalled()
    expect(h.commands.cancel).not.toHaveBeenCalled()
    expect(h.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(h.options.adoptResponseSession).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
    const retired = await wal.getDelivery!(record.ownerRequestId)
    expect(retired?.handoff).toBeUndefined()
    expect(retired?.request).toEqual(record.request)
    expect(retired?.retired).toBe(true)
  })

  it('does not resurrect a consumed retired draft after a fresh successful send and remount', async () => {
    const wal = memoryDeliveryWal()
    const h = queuedSendHarness({ pendingInputWal: wal, inputText: ref('original draft') })
    Object.assign(h.commands, { resolveReceipt: vi.fn(async (): Promise<TurnReceiptResult> => ({ status: 'retired' })) })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'ACK lost', undefined, null, true))
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    await h.api.onSend()
    await h.options.durableDelivery.wake()
    const oldId = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    await h.api.checkDelivery(oldId)
    expect(h.options.inputText.value).toBe('original draft')
    await h.api.onSend()
    expect(h.commands.send).toHaveBeenCalledTimes(2)
    expect(h.options.inputText.value).toBe('')
    h.api.dispose()
    h.options.durableDelivery.dispose()
    const reloaded = queuedSendHarness({ pendingInputWal: wal, inputText: ref('') })
    await reloaded.options.durableDelivery.wake()
    await reloaded.api.recoverResponseHandoffs()
    expect(reloaded.options.inputText.value).toBe('')
    expect(reloaded.commands.send).not.toHaveBeenCalled()
    expect(await wal.getDelivery!(oldId)).toMatchObject({ phase: 'not-sent', retired: true })
    expect((await wal.getDelivery!(oldId))?.handoff).toBeUndefined()
  })

  it('does not restore a retired request into another session during its manual check', async () => {
    const h = queuedSendHarness({ inputText: ref('original') })
    let retire!: (value: TurnReceiptResult) => void
    const resolveReceipt = vi.fn(() => new Promise<TurnReceiptResult>(resolve => { retire = resolve }))
    Object.assign(h.commands, { resolveReceipt })
    h.commands.send.mockRejectedValueOnce(new TurnCommandError('transport', 'ACK lost', undefined, null, true))
    h.commands.lookupReceipt.mockResolvedValue({ status: 'unsupported' })
    await h.api.onSend()
    await h.options.durableDelivery.wake()
    const id = h.commands.send.mock.calls[0]![0].params.clientRequestId!
    const checking = h.api.checkDelivery(id)
    await vi.waitFor(() => expect(resolveReceipt).toHaveBeenCalledOnce())
    h.options.sessionKey.value = 'agent:main:webchat:other'
    h.options.inputText.value = 'other session draft'
    retire({ status: 'retired' })
    await checking
    expect(h.options.inputText.value).toBe('other session draft')
    expect(h.options.pendingAttachments.value).toEqual([])
    expect(h.commands.send).toHaveBeenCalledOnce()
    expect(h.api.acceptanceRecoveryPendingForCurrentSession.value).toBe(false)
  })

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
