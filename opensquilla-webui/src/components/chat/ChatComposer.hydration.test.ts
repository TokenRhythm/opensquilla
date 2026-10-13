// @vitest-environment happy-dom

import { afterEach, expect, it, vi } from 'vitest'
import { computed, createApp, effectScope, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'
import { createChatSendHarness } from '@/composables/chat/chatSendTestHarness'
import type { UseChatSendOptions } from '@/composables/chat/useChatSend'
import { useChatSteerDelivery } from '@/composables/chat/useChatSteerDelivery'
import { useChatTaskOwnership } from '@/composables/chat/useChatTaskOwnership'
import { useChatSessionBootstrap } from '@/composables/chat/useChatSessionBootstrap'
import { useChatSessionSubscription } from '@/composables/chat/useChatSessionSubscription'
import { useActiveProjectWorkspace } from '@/composables/useActiveProjectWorkspace'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import { SessionReadFailure, type SessionReadLease, type SessionReadLifecycle,
  type SessionReadMetadata } from '@/modules/sessionReadLifecycle'
import { TurnCommandError, type TurnCommands, type TurnSendResponse } from '@/modules/turnCommands'
import type { ChatMessage } from '@/types/chat'

vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))

const KEY = 'agent:main:webchat:hydration'

function sendHarness(overrides: Partial<UseChatSendOptions> = {}) {
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
  const send = vi.fn<TurnCommands['send']>(async () => ({ taskId: 'next-task' }))
  const options: Omit<UseChatSendOptions, 'durableDelivery'> = {
    turnCommands: { send, steer: vi.fn(), cancel: vi.fn(), supports: () => true },
    inputText: ref('Synthetic follow-up'), messages, sessionKey: ref(KEY),
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
    ...overrides,
  }
  return { ...createChatSendHarness(options), send }
}

const BASE_PROPS = {
  attachments: [], busySendMode: 'queue' as const, hasSendContent: true,
  isStreaming: false, canStop: false, isNewLanding: false, placeholder: 'Send',
  sendButtonTitle: 'Send', runMode: 'safe' as const, allowedRunModes: ['safe' as const],
  runModeLocked: false, runModeLockMessage: '', sessionRoutingMode: 'off' as const,
  sessionRoutingBusy: false, voiceBusy: false, voiceRecording: false, voiceReady: true,
}

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); document.body.innerHTML = '' })

it('keeps Send disabled through a metadata failure and enables one click after automatic recovery', async () => {
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  const scope = effectScope()
  const ownership = useChatTaskOwnership()
  // The previous terminal is already consumed. This replacement read has no
  // terminal tail that could independently resolve ownership hydration.
  ownership.noteTerminal('previous-task')
  const project = useActiveProjectWorkspace()
  const metadata: SessionReadMetadata = {
    sessionKey: KEY, workspaceId: null, projectWorkspace: null, projectWorkspaceDeferred: false,
    activeTaskGroupIds: [], runModeLock: { locked: false, runMode: null, source: null, additional: {} },
    pendingUserInputs: [], collaboration: null, routing: null, currentPlan: null, activePlanRun: null,
    goal: null, goalSnapshotStreamSeq: null, tasks: [], activeTask: null, lastTask: null,
    runStatus: 'idle', queuedTaskIds: [], epoch: 1, hydrationComplete: true, deferredFields: [], additional: {},
  }
  let rejectMetadata!: (cause: Error) => void
  const pendingMetadata = new Promise<SessionReadMetadata>((_, reject) => { rejectMetadata = reject })
  const confirmInstalled = vi.fn(async () => {})
  const retryMetadata = vi.fn(async () => metadata)
  const lease = {
    criticalRequestsQueued: Promise.resolve(),
    live: Promise.resolve({ sessionKey: KEY, activity: 'idle', activeTaskId: null,
      initialMetadata: { ...metadata, hydrationComplete: false }, snapshot: null,
      reloadRequired: null, confirmInstalled }),
    metadata: pendingMetadata, retryMetadata, close: vi.fn(async () => {}),
  } as unknown as SessionReadLease
  const sessionKey = ref(KEY)
  const subscription = scope.run(() => useChatSessionSubscription({
    sessionReadLeaseReader: { current: () => lease }, conversationRuntime: createConversationRuntime(),
    sessionKey, lastStreamSeq: ref(5), runStatus: ref({ status: 'idle', label: '', task: null }),
    isStreaming: ref(false), hasActiveInterrupt: ref(false), activeStreamTaskId: ref(''),
    activeTaskGroups: ref(new Set<string>()), taskOwnership: ownership,
    ownershipHydrationRequired: () => true, startStreaming: vi.fn(), loadHistory: vi.fn(),
    resetStreamIdleTimer: vi.fn(), resetStreamLiveTurnState: vi.fn(),
    sessionRunStatus: () => ({ status: 'idle', label: '', task: null }),
    beginSessionMetadataResolution: key => project.beginSessionResolution(key),
    onSessionMetadata: (key, generation, value) => {
      project.applySessionSnapshot(key, generation, { workspaceId: value.workspaceId ?? undefined })
    },
    onSessionMetadataError: (key, generation) => { project.failSessionResolution(key, generation) },
  }))!
  const bootstrap = scope.run(() => useChatSessionBootstrap({
    sessionKey, sessionReadLifecycle: { open: () => lease, current: () => lease } as SessionReadLifecycle,
    loadHistory: async () => ({ ok: true }), subscribeSession: subscription.subscribeSession,
    connectionState: ref('connected'), metadataRecoveryError: subscription.metadataRecoveryError,
    retryMetadata: subscription.retrySessionMetadata, cancelHistory: vi.fn(),
    cancelSubscription: subscription.cancelActiveSubscription,
  }))!
  const liveBlocked = computed(() => bootstrap.livePhase.value !== 'ready' ? 'Connecting' : null)
  // ChatView leaves project errors to the send preflight; only pending or
  // unavailable project state disables the composer directly.
  const blocked = computed(() => liveBlocked.value
    || (['resolving', 'unavailable', 'removed'].includes(project.status.value) ? 'Project resolving' : ''))
  const subject = sendHarness({ taskOwnership: ownership, sendBlockedReason: liveBlocked })
  let sending = Promise.resolve()
  const onSend = vi.fn(() => { sending = subject.api.onSend() })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp({ render: () => h(ChatComposer, {
    ...BASE_PROPS, modelValue: subject.options.inputText.value,
    'onUpdate:modelValue': (value: string) => { subject.options.inputText.value = value },
    sendPending: subject.api.sendPending.value, sendDisabled: subject.api.sendHydrationBlocked.value,
    sendBlockedMessage: blocked.value, onSend,
  }) })
  app.use(i18n)
  app.mount(el)
  try {
    await expect(bootstrap.startSessionBootstrap({ includeHistory: false }).live)
      .resolves.toMatchObject({ authoritative: true })
    expect(confirmInstalled).toHaveBeenCalledOnce()
    expect(bootstrap.livePhase.value).toBe('ready')
    expect(ownership.hydrationResolved.value).toBe(false)
    rejectMetadata(new SessionReadFailure('busy', 'Temporary metadata contention', true))
    await vi.advanceTimersByTimeAsync(0)
    expect(project.status.value).toBe('error')
    expect(retryMetadata).not.toHaveBeenCalled()
    const button = el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')!
    expect(button.disabled).toBe(true)
    expect(button.getAttribute('aria-busy')).toBe('false')
    expect(button.querySelector('.loading-spinner')).toBeNull()
    expect(textarea.disabled).toBe(false)
    textarea.value = 'Edited follow-up'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    button.click()
    expect(onSend).not.toHaveBeenCalled()
    expect(subject.send).not.toHaveBeenCalled()

    // Exercise the existing connected/live-ready/error watcher and its 500ms
    // retry, rather than invoking retrySessionMetadata directly.
    await vi.advanceTimersByTimeAsync(500)
    expect(retryMetadata).toHaveBeenCalledOnce()
    expect(ownership.hydrationResolved.value).toBe(true)
    expect(project.status.value).toBe('none')
    expect(button.disabled).toBe(false)
    expect(subject.options.inputText.value).toBe('Edited follow-up')
    expect(subject.send).not.toHaveBeenCalled()
    button.click()
    await sending
    expect(onSend).toHaveBeenCalledOnce()
    expect(subject.send).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      kind: 'new-turn', params: expect.objectContaining({ message: 'Edited follow-up' }),
    }), expect.any(Object))
  } finally { app.unmount(); bootstrap.cancelSessionBootstrap(); scope.stop() }
})

it('preserves Stop while hydration blocks ordinary Send', async () => {
  const onStop = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposer, { ...BASE_PROPS, modelValue: '', canStop: true,
    sendDisabled: true, onStop })
  app.use(i18n)
  app.mount(el)
  try {
    await nextTick()
    const button = el.querySelector<HTMLButtonElement>('.chat-stop-btn')!
    expect(button.disabled).toBe(false)
    button.click()
    expect(onStop).toHaveBeenCalledOnce()
    expect(el.querySelector<HTMLTextAreaElement>('.chat-textarea')!.disabled).toBe(false)
  } finally { app.unmount() }
})

it('keeps the existing offline queue action available while task hydration is unresolved', async () => {
  const enqueuePendingInput = vi.fn(async () => true)
  const subject = sendHarness({ taskOwnership: useChatTaskOwnership(false),
    offlineQueueIdentity: ref('synthetic-identity'), enqueuePendingInput })
  expect(subject.api.sendHydrationBlocked.value).toBe(false)
  await subject.api.onSend()
  expect(subject.send).not.toHaveBeenCalled()
  expect(enqueuePendingInput).toHaveBeenCalledExactlyOnceWith('Synthetic follow-up', undefined, {
    deliveryIdentity: 'synthetic-identity',
  })
})

it('keeps exact unknown-acceptance replay available while task hydration is unresolved', async () => {
  const ownership = useChatTaskOwnership()
  const send = vi.fn<TurnCommands['send']>()
    .mockRejectedValue(new TurnCommandError('transport', 'Lost acknowledgement', undefined, null))
  const receipt: TurnSendResponse = { sessionKey: KEY, taskId: 'accepted-task', taskStatus: 'completed' }
  const lookupReceipt = vi.fn<NonNullable<TurnCommands['lookupReceipt']>>()
    .mockResolvedValue({ status: 'not-found' })
  const subject = sendHarness({ taskOwnership: ownership, turnCommands: {
    send, lookupReceipt, supportsReceiptLookup: () => true,
    steer: vi.fn(), cancel: vi.fn(), supports: () => true,
  } })
  await subject.api.onSend()
  expect(send).toHaveBeenCalledOnce()
  ownership.beginHydration()
  expect(subject.api.sendHydrationBlocked.value).toBe(false)
  subject.options.inputText.value = 'A newer draft'
  lookupReceipt.mockResolvedValue({ status: 'found', response: receipt })
  await subject.api.onSend()
  expect(send).toHaveBeenCalledOnce()
  expect(lookupReceipt).toHaveBeenCalledWith(expect.objectContaining({
    kind: 'send', request: expect.objectContaining({
      params: expect.objectContaining({ message: 'Synthetic follow-up' }),
    }),
  }), expect.any(Object))
  expect(subject.options.inputText.value).toBe('A newer draft')
})
