// @vitest-environment happy-dom

import { afterEach, expect, it, vi } from 'vitest'
import { computed, createApp, effectScope, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import ChatComposer from '@/components/chat/ChatComposer.vue'
import { createChatSendHarness } from '@/composables/chat/chatSendTestHarness'
import type { UseChatSendOptions } from '@/composables/chat/useChatSend'
import { useChatSteerDelivery } from '@/composables/chat/useChatSteerDelivery'
import { useChatTaskOwnership } from '@/composables/chat/useChatTaskOwnership'
import { useChatRecoveryNotice } from '@/composables/chat/useChatRecoveryNotice'
import { useChatSessionBootstrap } from '@/composables/chat/useChatSessionBootstrap'
import { useChatSessionSubscription } from '@/composables/chat/useChatSessionSubscription'
import { useActiveProjectWorkspace } from '@/composables/useActiveProjectWorkspace'
import { createConversationRuntime } from '@/modules/conversationRuntime'
import { SessionReadFailure, type SessionReadLease, type SessionReadLifecycle,
  type SessionReadMetadata } from '@/modules/sessionReadLifecycle'
import { type TurnCommands } from '@/modules/turnCommands'
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

type RecoveryMode = 'pending' | 'nonretryable' | 'incomplete' | 'retryable' | 'local-timeout' | 'transient'

function metadataComposition(mode: RecoveryMode = 'retryable') {
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
  let resolveMetadata!: (value: SessionReadMetadata) => void
  let rejectMetadata!: (cause: Error) => void
  const pendingMetadata = new Promise<SessionReadMetadata>((resolve, reject) => { resolveMetadata = resolve; rejectMetadata = reject })
  const confirmInstalled = vi.fn(async () => {})
  let serverHealthy = false
  const retryMetadata = vi.fn(async (): Promise<SessionReadMetadata> => {
    if (serverHealthy) return metadata
    if (mode === 'local-timeout') return new Promise(() => {})
    if (mode === 'transient') return metadata
    throw new SessionReadFailure('busy', 'Still busy', true)
  })
  const lease = {
    criticalRequestsQueued: Promise.resolve(),
    live: Promise.resolve({ sessionKey: KEY, activity: 'idle', activeTaskId: null,
      initialMetadata: { ...metadata, hydrationComplete: false }, snapshot: null,
      reloadRequired: null, confirmInstalled }),
    metadata: pendingMetadata, retryMetadata, close: vi.fn(async () => {}),
  } as unknown as SessionReadLease
  const sessionKey = ref(KEY)
  let applicationAllowed = () => true
  const subscription = scope.run(() => useChatSessionSubscription({
    metadataApplicationAllowed: () => applicationAllowed(),
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
    connectionState: ref('connected'),
    metadata: { state: subscription.metadataState, runAttempt: subscription.runMetadataAttempt,
      cancelAttempt: subscription.cancelMetadataAttempt }, cancelHistory: vi.fn(),
    cancelSubscription: subscription.cancelActiveSubscription,
  }))!
  applicationAllowed = bootstrap.canApplyMetadata
  const recoveryState = computed(() => bootstrap.livePhase.value === 'degraded' ? 'live-degraded' as const
    : bootstrap.livePhase.value === 'connecting' ? 'live-connecting' as const : null)
  const noticeVisible = scope.run(() => useChatRecoveryNotice(recoveryState, ref(false), ref(false), bootstrap.liveRecoveryTerminal))!
  const liveBlocked = computed(() => bootstrap.livePhase.value !== 'ready' ? 'Connecting' : null)
  // ChatView leaves project errors to the send preflight; only pending or
  // unavailable project state disables the composer directly.
  const blocked = computed(() => liveBlocked.value
    || (['resolving', 'unavailable', 'removed'].includes(project.status.value) ? 'Project resolving' : ''))
  const subject = sendHarness({ taskOwnership: ownership, sendBlockedReason: liveBlocked })
  let sending: ReturnType<typeof subject.api.onSend> = Promise.resolve(undefined)
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
  return {
    bootstrap, ownership, confirmInstalled, retryMetadata, subject, noticeVisible,
    metadata, resolveMetadata, rejectMetadata, el, onSend,
    sending: () => sending,
    setServerHealthy: () => { serverHealthy = true },
    cleanup() { app.unmount(); bootstrap.cancelSessionBootstrap(); scope.stop() },
  }
}

it.each(['pending', 'nonretryable', 'incomplete', 'retryable', 'local-timeout', 'transient'] as const)('bounds metadata recovery while preserving live readiness and safe Send: %s', async mode => {
  const fixture = metadataComposition(mode)
  const { bootstrap, ownership, confirmInstalled, retryMetadata, subject, noticeVisible,
    metadata, resolveMetadata, rejectMetadata, el, onSend } = fixture
  try {
    await expect(bootstrap.startSessionBootstrap({ includeHistory: false }).live)
      .resolves.toMatchObject({ authoritative: true })
    expect(confirmInstalled).toHaveBeenCalledOnce()
    expect(bootstrap.livePhase.value).toBe('ready')
    expect(ownership.hydrationResolved.value).toBe(false)
    if (mode === 'incomplete') resolveMetadata({ ...metadata, hydrationComplete: false })
    else if (mode === 'nonretryable') rejectMetadata(new SessionReadFailure('unavailable', 'Unavailable', false))
    else if (mode === 'retryable' || mode === 'local-timeout' || mode === 'transient') rejectMetadata(new SessionReadFailure('busy', 'Busy', true))
    const button = el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    subject.options.inputText.value = 'Edited while metadata is recovering'
    if (mode === 'transient') {
      await vi.advanceTimersByTimeAsync(500)
      await nextTick()
      expect(bootstrap.metadataRecovery!.phase.value).toBe('ready')
      expect(ownership.hydrationResolved.value).toBe(true)
      expect(button.disabled).toBe(false)
      expect(subject.options.inputText.value).toBe('Edited while metadata is recovering')
      button.click()
      await nextTick()
      await fixture.sending()
      expect(subject.send).toHaveBeenCalledOnce()
      expect(bootstrap.livePhase.value).toBe('ready')
      expect(noticeVisible.value).toBe(false)
    } else {
      await vi.advanceTimersByTimeAsync(121_000)
      await nextTick()
      expect(bootstrap.livePhase.value).toBe('ready')
      expect(bootstrap.liveRecoveryTerminal.value).toBe(false)
      expect(bootstrap.metadataRecovery!.phase.value).toBe('error')
      expect(bootstrap.metadataRecovery!.terminal.value).toBe(true)
      expect(noticeVisible.value).toBe(false)
      expect(ownership.hydrationResolved.value).toBe(false)
      expect(button.disabled).toBe(true)
      expect(subject.options.inputText.value).toBe('Edited while metadata is recovering')
      button.click()
      await nextTick()
      expect(onSend).not.toHaveBeenCalled()
      await subject.api.onSend()
      expect(subject.send).not.toHaveBeenCalled()
      const attempts = retryMetadata.mock.calls.length
      await vi.advanceTimersByTimeAsync(20_000)
      expect(retryMetadata).toHaveBeenCalledTimes(attempts)
      // A late initial response cannot clear terminal state or manufacture idle.
      resolveMetadata(metadata)
      await vi.advanceTimersByTimeAsync(1)
      expect(bootstrap.metadataRecovery!.terminal.value).toBe(true)
      expect(ownership.hydrationResolved.value).toBe(false)
      if (mode === 'incomplete' || mode === 'nonretryable') expect(attempts).toBe(0)
      else expect(attempts).toBeGreaterThan(0)
    }
  } finally { fixture.cleanup() }
})

it('restores Send through explicit metadata recovery without restarting ready live updates or sending the edited draft', async () => {
  const f = metadataComposition()
  try {
    await f.bootstrap.startSessionBootstrap({ includeHistory: false }).live
    f.rejectMetadata(new SessionReadFailure('busy', 'Metadata is temporarily busy', true))
    f.subject.options.inputText.value = 'Edited draft that only I will send'
    await vi.advanceTimersByTimeAsync(120_000)
    await nextTick()
    const button = f.el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(f.bootstrap.livePhase.value).toBe('ready')
    expect(f.bootstrap.metadataRecovery!.explicitRetryAvailable.value).toBe(true)
    expect(f.ownership.hydrationResolved.value).toBe(false)
    expect(button.disabled).toBe(true)
    const attemptsAtExpiry = f.retryMetadata.mock.calls.length
    f.setServerHealthy()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(f.retryMetadata).toHaveBeenCalledTimes(attemptsAtExpiry)

    await expect(f.bootstrap.retrySession()).resolves.toBe(true)
    await nextTick()
    expect(f.confirmInstalled).toHaveBeenCalledOnce()
    expect(f.bootstrap.livePhase.value).toBe('ready')
    expect(f.bootstrap.metadataRecovery!.phase.value).toBe('ready')
    expect(f.bootstrap.metadataRecovery!.explicitRetryAvailable.value).toBe(false)
    expect(f.ownership.hydrationResolved.value).toBe(true)
    expect(f.bootstrap.sessionRetryBusy.value).toBe(false)
    expect(button.disabled).toBe(false)
    expect(f.subject.options.inputText.value).toBe('Edited draft that only I will send')
    expect(f.subject.send).not.toHaveBeenCalled()
    button.click()
    await nextTick()
    await f.sending()
    expect(f.subject.send).toHaveBeenCalledOnce()
  } finally { f.cleanup() }
})

it('shares explicit recovery while preserving a 180-second server wait before restoring Send', async () => {
  const f = metadataComposition()
  try {
    await f.bootstrap.startSessionBootstrap({ includeHistory: false }).live
    f.rejectMetadata(new SessionReadFailure('busy', 'Wait for server capacity', true, 180_000))
    await vi.advanceTimersByTimeAsync(120_000)
    f.setServerHealthy()
    f.subject.options.inputText.value = 'Draft stays editable during the server wait'
    const first = f.bootstrap.retrySession()
    const repeated = f.bootstrap.retrySession()
    expect(f.bootstrap.sessionRetryBusy.value).toBe(true)
    expect(f.retryMetadata).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(59_999)
    expect(f.retryMetadata).not.toHaveBeenCalled()
    const button = f.el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(button.disabled).toBe(true)
    expect(f.subject.send).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await expect(first).resolves.toBe(true)
    await expect(repeated).resolves.toBe(true)
    await nextTick()
    expect(f.retryMetadata).toHaveBeenCalledOnce()
    expect(f.confirmInstalled).toHaveBeenCalledOnce()
    expect(f.bootstrap.sessionRetryBusy.value).toBe(false)
    expect(f.ownership.hydrationResolved.value).toBe(true)
    expect(button.disabled).toBe(false)
    expect(f.subject.options.inputText.value).toBe('Draft stays editable during the server wait')
    expect(f.subject.send).not.toHaveBeenCalled()
  } finally { f.cleanup() }
})

it('does not extend a manual episode through repeat calls when the server wait exceeds its deadline', async () => {
  const f = metadataComposition()
  try {
    await f.bootstrap.startSessionBootstrap({ includeHistory: false }).live
    f.rejectMetadata(new SessionReadFailure('busy', 'Long server wait', true, 300_000))
    await vi.advanceTimersByTimeAsync(120_000)
    f.setServerHealthy()
    const first = f.bootstrap.retrySession()
    await vi.advanceTimersByTimeAsync(60_000)
    const repeated = f.bootstrap.retrySession()
    await vi.advanceTimersByTimeAsync(60_000)
    await expect(first).resolves.toBe(false)
    await expect(repeated).resolves.toBe(false)
    expect(f.bootstrap.sessionRetryBusy.value).toBe(false)
    expect(f.bootstrap.metadataRecovery!.explicitRetryAvailable.value).toBe(true)
    expect(f.ownership.hydrationResolved.value).toBe(false)
    expect(f.retryMetadata).not.toHaveBeenCalled()
    // A later deliberate click can start another bounded episode, preserving
    // the same absolute server floor rather than sleeping another 300 seconds.
    const later = f.bootstrap.retrySession()
    await vi.advanceTimersByTimeAsync(59_999)
    expect(f.retryMetadata).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await expect(later).resolves.toBe(true)
    expect(f.retryMetadata).toHaveBeenCalledOnce()
    expect(f.ownership.hydrationResolved.value).toBe(true)
    expect(f.confirmInstalled).toHaveBeenCalledOnce()
    expect(f.subject.send).not.toHaveBeenCalled()
  } finally { f.cleanup() }
})

it('preserves a nonretryable metadata rejection when the generic session retry is invoked', async () => {
  const f = metadataComposition()
  try {
    await f.bootstrap.startSessionBootstrap({ includeHistory: false }).live
    f.rejectMetadata(new SessionReadFailure('unavailable', 'Access denied', false))
    await vi.advanceTimersByTimeAsync(120_000)
    expect(f.bootstrap.metadataRecovery!.explicitRetryAvailable.value).toBe(false)
    f.setServerHealthy()
    await expect(f.bootstrap.retrySession()).resolves.toBe(false)
    expect(f.retryMetadata).not.toHaveBeenCalled()
    expect(f.ownership.hydrationResolved.value).toBe(false)
    expect(f.confirmInstalled).toHaveBeenCalledOnce()
    expect(f.bootstrap.sessionRetryBusy.value).toBe(false)
    expect(f.subject.send).not.toHaveBeenCalled()
  } finally { f.cleanup() }
})
