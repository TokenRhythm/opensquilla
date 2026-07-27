import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import {
  useChatPendingQueue,
  type PendingQueueOwnerContext,
} from '@/composables/chat/useChatPendingQueue'
import type { Attachment, ChatMessage } from '@/types/chat'
import type { FoldLiveTurnMode } from './useChatTurnLog'
import { useChatSend, type UseChatSendOptions } from './useChatSend'
import { useChatSessionRuntime } from './useChatSessionRuntime'

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast: vi.fn() }),
}))

describe('chat send session handoff', () => {
  it('resumes one deferred queue drain after response hydration releases', async () => {
    vi.useFakeTimers()
    try {
      const sessionKey = ref('agent:main:webchat:child')
      const ownerContext = ref<PendingQueueOwnerContext | null>({
        sessionKey: sessionKey.value,
        ownerRequestId: 'request-child',
      })
      const inputText = ref('')
      const pendingAttachments = ref<Attachment[]>([])
      const pendingSessionIntent = ref<string | null>(null)
      const isStreaming = ref(false)
      const sendCurrentInput = vi.fn()
      const pendingQueue = useChatPendingQueue({
        sessionKey,
        ownerContext,
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        isStreaming,
        isBlocked: () => ownerContext.value?.sessionKey === sessionKey.value,
        autoResizeTextarea: vi.fn(),
        sendCurrentInput,
        resetInputHistory: vi.fn(),
        hasComposer: () => true,
      })

      // The child terminal replay can precede both history hydration and the
      // user's follow-up. Preserve it without draining through the handoff gate.
      pendingQueue.schedulePendingDrainAfterTerminal()
      inputText.value = 'follow-up after edit'
      pendingQueue.enqueuePendingInput(inputText.value)
      await vi.advanceTimersByTimeAsync(50)

      expect(pendingQueue.pendingQueue.value).toHaveLength(1)
      expect(sendCurrentInput).not.toHaveBeenCalled()

      ownerContext.value = null
      pendingQueue.flushDeferredPendingDrain()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(pendingQueue.pendingQueue.value).toHaveLength(0)
      expect(inputText.value).toBe('follow-up after edit')
      expect(sendCurrentInput).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
    }
  })

  it('adopts a fork child response through the full session lifecycle', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const messages = ref<ChatMessage[]>([
      { role: 'assistant', text: 'stopped partial answer', ts: '2026-07-22T00:00:00Z' },
    ])
    const pendingSessionIntent = ref<string | null>(null)
    const currentEpoch = ref(7)
    const lastStreamSeq = ref(19)
    const activeTaskGroups = ref(new Set(['parent-task-group']))
    const aborted = ref(true)
    const isStreaming = ref(false)
    const activeStreamTaskId = ref('')
    const activeStreamSessionKey = ref('')
    const inputText = ref('edited question')
    const pendingAttachments = ref<Attachment[]>([])
    const pendingQueueOwnerContext = ref<PendingQueueOwnerContext | null>(null)
    const trace: string[] = []
    let resolveSend!: (value: unknown) => void
    let sendCurrentInput: () => void = () => {}
    let dispatchHiddenControl: (providerText: string, displayText: string) => void = () => {}

    const persistSession = vi.fn((key: string) => {
      trace.push(`persist:${key}`)
      sessionKey.value = key
      // The real event-handler watcher clears the prior task binding on the
      // next Vue flush when the session key changes.
      void Promise.resolve().then(() => {
        activeStreamTaskId.value = ''
      })
    })
    const unsubscribeSession = vi.fn(() => {
      trace.push(`unsubscribe:${sessionKey.value}`)
    })
    const subscribeSession = vi.fn(async () => {
      trace.push(`subscribe:${sessionKey.value}`)
      await Promise.resolve()
      // The child subscription snapshot is authoritative and restores the
      // binding after the session-key watcher has cleared the parent state.
      activeStreamTaskId.value = 'task-child'
    })
    const loadHistory = vi.fn(() => {
      trace.push(`history:${sessionKey.value}:${messages.value.length}`)
    })
    const resetStreamLiveTurnState = vi.fn(() => {
      trace.push(`reset:${sessionKey.value}`)
      isStreaming.value = false
    })
    const pendingQueueRuntime = useChatPendingQueue({
      sessionKey,
      ownerContext: pendingQueueOwnerContext,
      inputText,
      pendingAttachments,
      pendingSessionIntent,
      isStreaming,
      isBlocked: () => pendingQueueOwnerContext.value?.sessionKey === sessionKey.value,
      autoResizeTextarea: vi.fn(),
      sendCurrentInput: () => sendCurrentInput(),
      resetInputHistory: vi.fn(),
      hasComposer: () => true,
      dispatchHiddenControl: (providerText, displayText) => {
        dispatchHiddenControl(providerText, displayText)
      },
    })
    inputText.value = 'existing parent follow-up'
    pendingQueueRuntime.enqueuePendingInput(
      inputText.value,
      { ownerRequestId: 'older-parent-request' },
    )
    pendingQueueRuntime.enqueueHiddenControl(
      { text: 'existing parent control', displayText: 'Existing parent control' },
      { ownerRequestId: 'older-parent-request' },
    )
    inputText.value = 'edited question'

    const sessionRuntime = useChatSessionRuntime({
      sessionKey,
      messages,
      pendingSessionIntent,
      routerDecisionPending: ref({ tier: 'c1' }),
      currentEpoch,
      lastStreamSeq,
      activeTaskGroups,
      aborted,
      lastHeaderRole: ref('assistant'),
      lastHeaderDay: ref('2026-07-22'),
      usageAccum: ref({
        input: 10,
        output: 20,
        cacheRead: 5,
        cacheWrite: 2,
        cost: 0.01,
        routedTurns: 1,
        sessionSaved: 1,
      }),
      usageModel: ref('test-model'),
      createSessionKey: vi.fn(() => childSessionKey),
      persistSession,
      unsubscribeSession,
      subscribeSession,
      loadHistory,
      loadCurrentSessionUsage: vi.fn(),
      applySessionRunState: vi.fn(),
      setCompactInFlight: vi.fn(),
      hideCompactStatus: vi.fn(),
      clearPendingQueue: pendingQueueRuntime.clearPendingQueue,
      switchPendingQueue: pendingQueueRuntime.switchPendingQueue,
      adoptPendingQueue: pendingQueueRuntime.adoptPendingQueue,
      resetSavingsPopupCooldown: vi.fn(),
      restoreWidgetState: vi.fn(),
      resetStreamLiveTurnState,
    })

    const stream: UseChatSendOptions['stream'] = {
      isStreaming,
      streamBubble: ref(false),
      streamHasVisibleOutput: ref(false),
      startStreaming: vi.fn(() => {
        isStreaming.value = true
      }),
      endStreaming: vi.fn(() => {
        isStreaming.value = false
      }),
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
      useReducer: ref<FoldLiveTurnMode>(false),
    }
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>((resolve) => {
        resolveSend = resolve as (value: unknown) => void
      })) as UseChatSendOptions['rpc']['call'],
    }
    const send = useChatSend({
      rpc,
      inputText,
      messages,
      sessionKey,
      pendingQueueOwnerContext,
      busySendMode: pendingQueueRuntime.busySendMode,
      modelRoutingMode: ref<'off'>('off'),
      modelRoutingSettingsBusy: ref(false),
      elevatedMode: ref(''),
      runMode: ref('trusted'),
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId: ref('msg-B'),
      aborted,
      activeStreamTaskId,
      activeStreamSessionKey,
      autoScroll: ref(false),
      stream,
      normalizeElevatedMode: mode => mode,
      adoptResponseSession: sessionRuntime.adoptResponseSession,
      scheduleHistorySync: vi.fn(),
      schedulePendingDrainAfterTerminal: pendingQueueRuntime.schedulePendingDrainAfterTerminal,
      flushDeferredPendingDrain: pendingQueueRuntime.flushDeferredPendingDrain,
      isCompactInFlightForCurrentSession: () => false,
      hasPendingAttachmentWork: () => false,
      enqueuePendingInput: pendingQueueRuntime.enqueuePendingInput,
      enqueueHiddenControl: pendingQueueRuntime.enqueueHiddenControl,
      popAllPendingIntoComposer: pendingQueueRuntime.popAllPendingIntoComposer,
      executeSlashCommand: vi.fn(async () => false),
      closeSlashMenu: vi.fn(),
      autoResizeTextarea: vi.fn(),
      scrollToBottom: vi.fn(),
    })
    sendCurrentInput = send.onSend
    dispatchHiddenControl = send.dispatchHiddenSend

    const firstSend = send.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))

    inputText.value = 'queued follow-up'
    pendingAttachments.value = [{
      kind: 'staged',
      local_id: 42,
      name: 'queued.txt',
      mime: 'text/plain',
      file_uuid: 'file-queued',
    }]
    pendingQueueRuntime.enqueuePendingInput(inputText.value)
    pendingQueueRuntime.enqueueHiddenControl({
      text: 'hidden control',
      displayText: 'Hidden control',
    })
    resolveSend({
      sessionKey: childSessionKey,
      task_id: 'task-child',
    })
    await firstSend

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      sessionKey: parentSessionKey,
      forkBeforeMessageId: 'msg-B',
    }))
    expect(trace).toEqual([
      `unsubscribe:${parentSessionKey}`,
      `persist:${childSessionKey}`,
      `reset:${childSessionKey}`,
      `subscribe:${childSessionKey}`,
      `history:${childSessionKey}:0`,
    ])
    expect(sessionKey.value).toBe(childSessionKey)
    expect(messages.value).toEqual([])
    expect(currentEpoch.value).toBe(0)
    expect(lastStreamSeq.value).toBe(0)
    expect(activeTaskGroups.value.size).toBe(0)
    expect(aborted.value).toBe(false)
    expect(activeStreamTaskId.value).toBe('task-child')
    expect(activeStreamSessionKey.value).toBe(childSessionKey)
    expect(pendingQueueRuntime.pendingQueue.value).toHaveLength(2)
    expect(pendingQueueRuntime.pendingQueue.value).toMatchObject([
      {
        text: 'queued follow-up',
        attachments: [expect.objectContaining({ local_id: 42, file_uuid: 'file-queued' })],
        intent: null,
        ownerSessionKey: childSessionKey,
      },
      {
        text: 'hidden control',
        attachments: [],
        intent: null,
        hiddenControl: true,
        displayTextOverride: 'Hidden control',
        ownerSessionKey: childSessionKey,
      },
    ])

    // The route watcher observes the child key after persistSession. It must be
    // an idempotent no-op instead of repeating the handoff.
    await sessionRuntime.switchToSession(childSessionKey)
    expect(trace).toHaveLength(5)

    // A visible parent item that predated this chat.send was parked instead of
    // being misdelivered to the fork child. Stale hidden controls are dropped,
    // because replaying them after returning to the parent would confirm an old run.
    await sessionRuntime.switchToSession(parentSessionKey)
    expect(pendingQueueRuntime.pendingQueue.value).toMatchObject([
      {
        text: 'existing parent follow-up',
        ownerSessionKey: parentSessionKey,
        ownerRequestId: 'older-parent-request',
      },
    ])
  })
})
