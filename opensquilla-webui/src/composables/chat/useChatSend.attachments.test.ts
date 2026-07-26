import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import { useChatSend, type UseChatSendOptions } from './useChatSend'
import { useChatMessageActions } from './useChatMessageActions'
import type { FoldLiveTurnMode } from './useChatTurnLog'
import type { Attachment, ChatMessage, ChatRenderedMessage } from '@/types/chat'
import {
  useChatPendingQueue,
  type BusySendMode,
} from '@/composables/chat/useChatPendingQueue'
import { FINISHED_STREAM_TASK_ID, STOPPED_STREAM_TASK_ID } from '@/utils/chat/streamEvents'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

function makeOptions(overrides: Partial<UseChatSendOptions> = {}) {
  const rpc = {
    call: vi.fn().mockResolvedValue({ sessionKey: 'agent:main:webchat:test' }),
  }
  const stream: UseChatSendOptions['stream'] = {
    isStreaming: ref(false),
    streamBubble: ref(false),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(),
    endStreaming: vi.fn(),
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
  const options: UseChatSendOptions = {
    rpc,
    inputText: ref('hello'),
    messages: ref<ChatMessage[]>([]),
    sessionKey: ref('agent:main:webchat:test'),
    pendingQueueOwnerContext: ref(null),
    busySendMode: ref<BusySendMode>('queue'),
    modelRoutingMode: ref<'off'>('off'),
    modelRoutingSettingsBusy: ref(false),
    elevatedMode: ref(''),
    runMode: ref('trusted'),
    pendingAttachments: ref<Attachment[]>([]),
    pendingSessionIntent: ref(null),
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
    popAllPendingIntoComposer: vi.fn(() => false),
    executeSlashCommand: vi.fn(async () => false),
    closeSlashMenu: vi.fn(),
    autoResizeTextarea: vi.fn(),
    scrollToBottom: vi.fn(),
    ...overrides,
  }
  return { api: useChatSend(options), options, rpc, stream }
}

describe('useChatSend attachment payloads', () => {
  it('sends the selected sandbox run mode as trusted source metadata', async () => {
    const { api, rpc } = makeOptions({
      runMode: ref('standard'),
    } as Partial<UseChatSendOptions>)

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      _source: { runMode: 'standard' },
    }))
  })

  it('serializes only sendable attachments and leaves failed attachments in the composer', async () => {
    const failed: Attachment = {
      kind: 'failed',
      local_id: 1,
      name: 'failed.pdf',
      mime: 'application/pdf',
      error: 'HTTP 500',
      file: new File(['failed'], 'failed.pdf', { type: 'application/pdf' }),
    }
    const ready: Attachment = {
      kind: 'staged',
      local_id: 2,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const pendingAttachments = ref<Attachment[]>([failed, ready])
    const { api, options, rpc } = makeOptions({ pendingAttachments })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { type: 'application/pdf', file_uuid: 'file-ready', mime: 'application/pdf', name: 'ready.pdf' },
      ],
    }))
    expect(options.messages.value[0]).toMatchObject({
      role: 'user',
      text: 'hello',
      attachments: [
        { kind: 'staged', displayId: 'local:2', renderKey: 'local:2', name: 'ready.pdf', mime: 'application/pdf' },
      ],
    })
    expect(JSON.stringify(options.messages.value[0])).not.toContain('file-ready')
    expect(JSON.stringify(options.messages.value[0])).not.toContain('failed.pdf')
    expect(pendingAttachments.value).toEqual([failed])
  })

  it('refreshes staged uploads before serializing chat.send attachments', async () => {
    const pendingAttachments = ref<Attachment[]>([
      {
        kind: 'staged',
        local_id: 1,
        name: 'ready.pdf',
        mime: 'application/pdf',
        file_uuid: 'file-expired',
        expires_at: Date.now() / 1000 - 1,
        file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
      },
    ])
    const prepareAttachmentsForSend = vi.fn(async () => {
      pendingAttachments.value = [
        {
          kind: 'staged',
          local_id: 1,
          name: 'ready.pdf',
          mime: 'application/pdf',
          file_uuid: 'file-fresh',
          expires_at: Date.now() / 1000 + 600,
          file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
        },
      ]
      return true
    })
    const { api, rpc } = makeOptions({ pendingAttachments, prepareAttachmentsForSend })

    await api.onSend()

    expect(prepareAttachmentsForSend).toHaveBeenCalledTimes(1)
    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { type: 'application/pdf', file_uuid: 'file-fresh', mime: 'application/pdf', name: 'ready.pdf' },
      ],
    }))
  })

  it('does not include attachments added while preparing an earlier send', async () => {
    const initialAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'initial.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-initial',
    }
    const addedAttachment: Attachment = {
      kind: 'staged',
      local_id: 2,
      name: 'added-later.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-added-later',
    }
    const pendingAttachments = ref<Attachment[]>([initialAttachment])
    const prepareAttachmentsForSend = vi.fn(async () => {
      pendingAttachments.value = [initialAttachment, addedAttachment]
      return true
    })
    const { api, rpc } = makeOptions({ pendingAttachments, prepareAttachmentsForSend })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      attachments: [
        { type: 'application/pdf', file_uuid: 'file-initial', mime: 'application/pdf', name: 'initial.pdf' },
      ],
    }))
    expect(pendingAttachments.value).toEqual([addedAttachment])
  })

  it('does not mutate or send when attachment preparation returns false', async () => {
    const inputText = ref('hello')
    const expiredAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-expired',
      expires_at: Date.now() / 1000 - 1,
      file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
    }
    const pendingAttachments = ref<Attachment[]>([expiredAttachment])
    const prepareAttachmentsForSend = vi.fn(async () => false)
    const { api, options, rpc, stream } = makeOptions({
      inputText,
      pendingAttachments,
      prepareAttachmentsForSend,
    })

    await api.onSend()

    expect(prepareAttachmentsForSend).toHaveBeenCalledTimes(1)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toHaveLength(0)
    expect(inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([expiredAttachment])
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('does not mutate or send when session changes during attachment preparation', async () => {
    let resolvePrepare!: (ready: boolean) => void
    let prepareContext: { isCurrent?: () => boolean } | undefined
    const inputText = ref('hello')
    const sessionKey = ref('agent:main:webchat:first')
    const stagedAttachment: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
      file: new File(['pdf'], 'ready.pdf', { type: 'application/pdf' }),
    }
    const pendingAttachments = ref<Attachment[]>([stagedAttachment])
    const prepareAttachmentsForSend = vi.fn((context?: { isCurrent?: () => boolean }) => new Promise<boolean>(resolve => {
      prepareContext = context
      resolvePrepare = resolve
    }))
    const { api, options, rpc, stream } = makeOptions({
      inputText,
      sessionKey,
      pendingAttachments,
      prepareAttachmentsForSend,
    })

    const send = api.onSend()
    sessionKey.value = 'agent:main:webchat:second'
    expect(prepareContext?.isCurrent?.()).toBe(false)
    resolvePrepare(true)
    await send

    expect(prepareAttachmentsForSend).toHaveBeenCalledTimes(1)
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toHaveLength(0)
    expect(inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([stagedAttachment])
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('does not dispatch an empty failed-only attachment draft', async () => {
    const failed: Attachment = {
      kind: 'failed',
      local_id: 1,
      name: 'failed.pdf',
      mime: 'application/pdf',
      error: 'HTTP 500',
      file: new File(['failed'], 'failed.pdf', { type: 'application/pdf' }),
    }
    const pendingAttachments = ref<Attachment[]>([failed])
    const { api, rpc } = makeOptions({
      inputText: ref(''),
      pendingAttachments,
    })

    await api.onSend()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(pendingAttachments.value).toEqual([failed])
  })

  it('restores an unknown-acceptance send for idempotent retry', async () => {
    const ready: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const pendingAttachments = ref<Attachment[]>([ready])
    const pendingSessionIntent = ref<string | null>('NEW')
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const rpc = {
      call: vi.fn().mockRejectedValue(new Error('network down')),
    }
    const { api, options } = makeOptions({
      rpc,
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
    })

    await api.onSend()

    expect(pendingAttachments.value).toEqual([ready])
    expect(options.inputText.value).toBe('hello')
    expect(pendingSessionIntent.value).toBe('NEW')
    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'Send failed: network down',
    })
  })

  it('sends pending fork target and clears it after chat.send is accepted', async () => {
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const { api, rpc } = makeOptions({ pendingForkBeforeMessageId })

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      forkBeforeMessageId: 'msg-B',
    }))
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('switches the session lifecycle when a stopped turn is edited into a child session', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const pendingForkBeforeMessageId = ref<string | null>(null)
    const adoptResponseSession = vi.fn()
    const rpcCall = vi.fn(async (method: string) => {
      if (method === 'chat.send') {
        return { sessionKey: childSessionKey, task_id: 'task-child' }
      }
      return { ok: true }
    })
    const rpc: UseChatSendOptions['rpc'] = {
      call: rpcCall as unknown as UseChatSendOptions['rpc']['call'],
    }
    const { api, options, stream } = makeOptions({
      rpc,
      sessionKey: ref(parentSessionKey),
      activeStreamSessionKey: ref(parentSessionKey),
      pendingForkBeforeMessageId,
      adoptResponseSession,
    })
    stream.isStreaming.value = true
    vi.mocked(stream.endStreaming).mockImplementation(() => {
      stream.isStreaming.value = false
    })

    api.onStop()
    pendingForkBeforeMessageId.value = 'msg-B'
    options.inputText.value = 'edited question'
    await api.onSend()

    expect(rpcCall).toHaveBeenCalledWith('chat.abort', {
      sessionKey: parentSessionKey,
      source: 'webui_stop',
    })
    expect(rpcCall).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      sessionKey: parentSessionKey,
      forkBeforeMessageId: 'msg-B',
      message: 'edited question',
    }))
    expect(adoptResponseSession).toHaveBeenCalledOnce()
    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
  })

  it('binds the accepted user message id so stop then edit sends a real fork', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('original question')
    const messages = ref<ChatMessage[]>([])
    const pendingForkBeforeMessageId = ref<string | null>(null)
    let sendCount = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.abort') return Promise.resolve({ aborted: true }) as Promise<T>
        sendCount += 1
        if (sendCount === 1) {
          return Promise.resolve({
            sessionKey: parentSessionKey,
            task_id: 'task-original',
            user_message_id: 'message-original',
            client_message_id: params?.clientMessageId,
          }) as Promise<T>
        }
        return Promise.resolve({
          sessionKey: childSessionKey,
          task_id: 'task-edited',
          user_message_id: 'message-edited',
          client_message_id: params?.clientMessageId,
        }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      messages,
      pendingForkBeforeMessageId,
      adoptResponseSession,
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    await harness.api.onSend()

    const optimisticUser = messages.value[0]
    expect(optimisticUser?.clientId).toBeTruthy()
    expect(optimisticUser?.messageId).toBe('message-original')
    expect(rpc.call).toHaveBeenNthCalledWith(1, 'chat.send', expect.objectContaining({
      clientMessageId: optimisticUser?.clientId,
    }))

    harness.api.onStop()
    const actions = useChatMessageActions({
      messages,
      inputText,
      isStreaming: harness.stream.isStreaming,
      sanitizeCopyText: text => text,
      stripTimePrefix: text => text,
      autoResizeTextarea: vi.fn(),
      sendCurrentInput: vi.fn(),
      focusComposer: vi.fn(),
      pendingForkBeforeMessageId,
    })
    actions.editMessage({
      ...optimisticUser,
      sourceIndex: 0,
    } as ChatRenderedMessage)
    expect(pendingForkBeforeMessageId.value).toBe('message-original')

    inputText.value = 'edited question'
    await harness.api.onSend()

    expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
      sessionKey: parentSessionKey,
      forkBeforeMessageId: 'message-original',
    }))
    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
  })

  it('restores the pending fork target only when chat.send explicitly rejects the attempt', async () => {
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('database busy'), {
        accepted: false,
      })),
    }
    const { api } = makeOptions({ rpc, pendingForkBeforeMessageId })

    await api.onSend()

    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
  })

  it('restores the complete rejected attempt and reuses its id and metadata', async () => {
    const ready: Attachment = {
      kind: 'staged',
      local_id: 1,
      name: 'ready.pdf',
      mime: 'application/pdf',
      file_uuid: 'file-ready',
    }
    const inputText = ref('hello')
    const pendingAttachments = ref<Attachment[]>([ready])
    const pendingSessionIntent = ref<string | null>('NEW')
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const elevatedMode = ref('enabled')
    const runMode = ref<'standard' | 'trusted' | 'full'>('standard')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('database busy'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test', task_id: 'task-new' }),
    }
    const { api, options } = makeOptions({
      rpc,
      inputText,
      pendingAttachments,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
      elevatedMode,
      runMode,
    })

    await api.onSend()

    expect(inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([ready])
    expect(pendingSessionIntent.value).toBe('NEW')
    expect(pendingForkBeforeMessageId.value).toBe('msg-B')
    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toMatchObject({
      clientRequestId: expect.any(String),
      clientMessageId: expect.any(String),
      message: 'hello',
      sessionKey: 'agent:main:webchat:test',
      intent: 'NEW',
      forkBeforeMessageId: 'msg-B',
      _source: { elevated: 'enabled', runMode: 'standard' },
      attachments: [{ file_uuid: 'file-ready' }],
    })

    // Retrying this recovered attempt must keep its original fingerprint even
    // if ambient composer settings changed after the first send.
    elevatedMode.value = ''
    runMode.value = 'full'
    await api.onSend()

    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(secondParams).toEqual(firstParams)
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
    expect(inputText.value).toBe('')
    expect(pendingAttachments.value).toEqual([])
    expect(pendingSessionIntent.value).toBeNull()
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('retries a recovered steer attempt unchanged after the active run becomes idle', async () => {
    const inputText = ref('steer this exact turn')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('response lost'), {
          accepted: false,
          retryable: true,
        }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test', task_id: 'task-steer' }),
    }
    const { api, options, stream } = makeOptions({
      rpc,
      inputText,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    await api.onSend()
    const firstParams = rpc.call.mock.calls[0]?.[1]
    expect(firstParams).toMatchObject({
      message: 'steer this exact turn',
      queueMode: 'steer',
      clientRequestId: expect.any(String),
    })
    expect(inputText.value).toBe('steer this exact turn')

    stream.isStreaming.value = false
    await api.onSend()

    expect(rpc.call.mock.calls[1]?.[1]).toEqual(firstParams)
    expect(options.messages.value.filter(message => message.role === 'user')).toHaveLength(1)
  })

  it('keeps a recovered fork gated while its canonical child response is pending', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let resolveRetry!: (value: unknown) => void
    let sendCount = 0
    const rpcCall = vi.fn(<T = unknown>(method: string, _params?: Record<string, unknown>) => {
      if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
      sendCount += 1
      if (sendCount === 1) {
        return Promise.reject(Object.assign(new Error('database busy'), {
          accepted: false,
          retryable: true,
        })) as Promise<T>
      }
      if (sendCount === 2) {
        return new Promise<T>((resolve) => {
          resolveRetry = resolve as (value: unknown) => void
        })
      }
      return Promise.resolve({ sessionKey: parentSessionKey }) as Promise<T>
    })
    const rpc = { call: rpcCall as UseChatSendOptions['rpc']['call'] }
    const enqueuePendingInput = vi.fn(() => true)
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      busySendMode: ref<BusySendMode>('steer'),
      enqueuePendingInput,
      adoptResponseSession,
    })

    await harness.api.onSend()
    harness.stream.isStreaming.value = true
    const retry = harness.api.onSend()
    await vi.waitFor(() => expect(sendCount).toBe(2))
    const ownerRequestId = String(rpcCall.mock.calls[1]?.[1]?.clientRequestId)

    inputText.value = 'follow the recovered edit'
    await harness.api.onSend()

    expect(sendCount).toBe(2)
    expect(enqueuePendingInput).toHaveBeenCalledWith('follow the recovered edit', {
      ownerRequestId,
    })

    resolveRetry({ sessionKey: childSessionKey, task_id: 'task-child' })
    await retry
    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, ownerRequestId)
  })

  it('aborts a recovered fork child that resolves after Stop during an ambient run', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let resolveRetry!: (value: unknown) => void
    let sendCount = 0
    const rpcCall = vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
      if (method === 'chat.abort') {
        if (params?.sessionKey === childSessionKey) {
          return Promise.reject(new Error('socket closed')) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }
      sendCount += 1
      if (sendCount === 1) {
        return Promise.reject(Object.assign(new Error('response lost'), {
          accepted: false,
          retryable: true,
        })) as Promise<T>
      }
      return new Promise<T>((resolve) => {
        resolveRetry = resolve as (value: unknown) => void
      })
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc: { call: rpcCall as UseChatSendOptions['rpc']['call'] },
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    await harness.api.onSend()
    harness.stream.isStreaming.value = true
    harness.options.activeStreamSessionKey.value = parentSessionKey
    const retry = harness.api.onSend()
    await vi.waitFor(() => expect(sendCount).toBe(2))

    // The ambient parent run can finish while the idempotent fork retry is
    // still waiting for its canonical child response.
    harness.stream.isStreaming.value = false
    harness.api.onStop()
    resolveRetry({ sessionKey: childSessionKey, task_id: 'task-child' })
    await retry

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(rpcCall).toHaveBeenCalledWith('chat.abort', {
      sessionKey: childSessionKey,
      taskId: 'task-child',
      source: 'webui_stale_send',
    })
    expect(harness.options.aborted.value).toBe(true)
    expect(harness.options.activeStreamTaskId.value).toBe(STOPPED_STREAM_TASK_ID)
    expect(harness.options.activeStreamSessionKey.value).toBe(childSessionKey)
    expect(harness.stream.isStreaming.value).toBe(false)
    await vi.waitFor(() => expect(harness.options.messages.value).toContainEqual(
      expect.objectContaining({
        role: 'system',
        text: 'Stop could not reach the server — the run may still be finishing.',
      }),
    ))
  })

  it('uses a new id when the user changes a recovered attempt before resending', async () => {
    const inputText = ref('hello')
    const elevatedMode = ref('enabled')
    const rpc = {
      call: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('database busy'), { accepted: false }))
        .mockResolvedValueOnce({ sessionKey: 'agent:main:webchat:test', task_id: 'task-new' }),
    }
    const { api } = makeOptions({ rpc, inputText, elevatedMode })

    await api.onSend()
    inputText.value = 'edited'
    elevatedMode.value = ''
    await api.onSend()

    const firstParams = rpc.call.mock.calls[0]?.[1]
    const secondParams = rpc.call.mock.calls[1]?.[1]
    expect(secondParams.clientRequestId).not.toBe(firstParams.clientRequestId)
    expect(secondParams).toMatchObject({ message: 'edited', _source: { runMode: 'trusted' } })
  })

  it('does not restore an attempt explicitly reported as accepted', async () => {
    const inputText = ref('hello')
    const pendingSessionIntent = ref<string | null>('NEW')
    const pendingForkBeforeMessageId = ref<string | null>('msg-B')
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('response lost'), {
        accepted: true,
        retryable: false,
      })),
    }
    const { api } = makeOptions({
      rpc,
      inputText,
      pendingSessionIntent,
      pendingForkBeforeMessageId,
    })

    await api.onSend()

    expect(inputText.value).toBe('')
    expect(pendingSessionIntent.value).toBeNull()
    expect(pendingForkBeforeMessageId.value).toBeNull()
  })

  it('ends a fresh stream when an idempotent replay is already terminal', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-old',
        replayed: true,
        task_status: 'succeeded',
      }),
    }
    const { api, options, stream } = makeOptions({ rpc })

    await api.onSend()

    expect(stream.startStreaming).toHaveBeenCalledTimes(1)
    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
    expect(options.activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
    expect(options.activeStreamSessionKey.value).toBe('')
  })

  it('surfaces the backend terminal message when a failed replay is already terminal', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-old',
        replayed: true,
        taskStatus: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'Activation failed; retry this message.',
      }),
    }
    const { api, options, stream } = makeOptions({ rpc })

    await api.onSend()

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'Activation failed; retry this message.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
  })

  it('ends the fresh stream when first acceptance reports activation failure', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-failed-before-activation',
        replayed: false,
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The accepted task could not be activated.',
      }),
    }
    const { api, options, stream } = makeOptions({ rpc })

    await api.onSend()

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.activeStreamTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
    expect(options.activeStreamSessionKey.value).toBe('')
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The accepted task could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
  })

  it('keeps a child-session activation failure after the session handoff', async () => {
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref('agent:main:webchat:parent')
    const messages = ref<ChatMessage[]>([])
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      messages.value = []
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-child-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The edited question could not be activated.',
      }),
    }
    const { api, options } = makeOptions({
      rpc,
      sessionKey,
      messages,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })

    await api.onSend()

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The edited question could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
  })

  it('keeps a hidden child-session terminal failure after the session handoff', async () => {
    const childSessionKey = 'agent:main:webchat:hidden-child'
    const sessionKey = ref('agent:main:webchat:parent')
    const messages = ref<ChatMessage[]>([])
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      messages.value = []
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-hidden-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The confirmation could not be activated.',
      }),
    }
    const { api, options } = makeOptions({ rpc, sessionKey, messages, adoptResponseSession })

    await api.dispatchHiddenSend('provider confirmation', 'Confirmed')

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The confirmation could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
  })

  it('does not leak a child terminal failure after navigation during the handoff', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const otherSessionKey = 'agent:main:webchat:other'
    const sessionKey = ref(parentSessionKey)
    let finishHandoff!: () => void
    const handoffGate = new Promise<void>((resolve) => {
      finishHandoff = resolve
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      await handoffGate
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-child-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'This failure belongs to the child session.',
      }),
    }
    const { api, options } = makeOptions({ rpc, sessionKey, adoptResponseSession })

    const send = api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledWith(
      childSessionKey,
      expect.any(String),
    ))
    sessionKey.value = otherSessionKey
    finishHandoff()
    await send

    expect(options.messages.value.some(message => message.terminalNotice)).toBe(false)
  })

  it('queues instead of steering a new child input while response handoff hydrates', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let finishHandoff!: () => void
    const handoffGate = new Promise<void>((resolve) => {
      finishHandoff = resolve
    })
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      // The real session runtime resets the parent live-turn state before
      // child subscription/history hydration has completed.
      stream.isStreaming.value = false
      await handoffGate
    })
    let sendCount = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
        sendCount += 1
        if (sendCount === 1) {
          return Promise.resolve({
            sessionKey: childSessionKey,
            task_id: 'task-child-old',
            task_status: 'failed',
            terminal_reason: 'activation_failed',
            terminal_message: 'The edited question could not be activated.',
          }) as Promise<T>
        }
        return Promise.resolve({ sessionKey: childSessionKey }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      adoptResponseSession,
      enqueuePendingInput,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })
    stream.endStreaming = vi.fn(() => {
      stream.isStreaming.value = false
    })

    const oldSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledWith(
      childSessionKey,
      expect.any(String),
    ))

    inputText.value = 'new child question'
    await harness.api.onSend()
    expect(sendCount).toBe(1)
    expect(enqueuePendingInput).toHaveBeenCalledWith('new child question', {
      ownerRequestId: expect.any(String),
    })

    finishHandoff()
    await oldSend

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(stream.isStreaming.value).toBe(false)
    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledTimes(1)
    expect(harness.options.flushDeferredPendingDrain).toHaveBeenCalledOnce()
  })

  it('schedules an adopted follow-up when terminal replay finishes before hydration', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    const activeStreamTaskId = ref('')
    let finishHydration!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHydration = resolve
    })
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      stream.isStreaming.value = false
      // A terminal replay can arrive while the handoff reset has streaming
      // false; the event handler records the FINISHED sentinel and returns.
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      await hydration
      return { authoritativeIdle: true }
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: childSessionKey,
        task_id: 'task-child-not-replayed',
      }),
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      activeStreamTaskId,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())
    inputText.value = 'follow-up for idle child'
    await harness.api.onSend()
    expect(enqueuePendingInput).toHaveBeenCalledOnce()

    finishHydration()
    await forkSend

    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    expect(harness.options.flushDeferredPendingDrain).toHaveBeenCalledOnce()
  })

  it('waits for a terminal event before draining a legacy child without a task id', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    const activeStreamTaskId = ref('')
    let finishHydration!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHydration = resolve
    })
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      stream.isStreaming.value = false
      activeStreamTaskId.value = ''
      await hydration
      return { authoritativeIdle: true }
    })
    const rpc = {
      call: vi.fn().mockResolvedValue({ sessionKey: childSessionKey }),
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      activeStreamTaskId,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())
    inputText.value = 'follow-up for legacy child'
    await harness.api.onSend()
    expect(enqueuePendingInput).toHaveBeenCalledOnce()

    finishHydration()
    await forkSend

    expect(harness.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(harness.options.flushDeferredPendingDrain).toHaveBeenCalledOnce()
    expect(stream.startStreaming).toHaveBeenCalledTimes(2)
    expect(stream.isStreaming.value).toBe(true)
  })

  it('drains a legacy child when terminal replay is authoritatively complete', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const activeStreamTaskId = ref('')
    let stream!: UseChatSendOptions['stream']
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      stream.isStreaming.value = false
      activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      return { authoritativeIdle: true }
    })
    const harness = makeOptions({
      rpc: { call: vi.fn().mockResolvedValue({ sessionKey: childSessionKey }) },
      sessionKey,
      activeStreamTaskId,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    stream = harness.stream
    stream.startStreaming = vi.fn(() => {
      stream.isStreaming.value = true
    })

    await harness.api.onSend()

    expect(stream.startStreaming).toHaveBeenCalledOnce()
    expect(stream.isStreaming.value).toBe(false)
    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
  })

  it('does not treat a failed child subscription as authoritative idle', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let finishHydration!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHydration = resolve
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      await hydration
      return { authoritativeIdle: false }
    })
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc: {
        call: vi.fn().mockResolvedValue({
          sessionKey: childSessionKey,
          task_id: 'task-child-unknown',
        }),
      },
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())
    inputText.value = 'follow-up while subscription is unavailable'
    await harness.api.onSend()
    finishHydration()
    await forkSend

    expect(enqueuePendingInput).toHaveBeenCalledOnce()
    expect(harness.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
  })

  it('queues steer input while a fork send is awaiting its canonical session', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let resolveFork!: (value: unknown) => void
    let sendCount = 0
    const rpcCall = vi.fn(<T = unknown>(method: string, _params?: Record<string, unknown>) => {
      if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
      sendCount += 1
      if (sendCount === 1) {
        return new Promise<T>((resolve) => {
          resolveFork = resolve as (value: unknown) => void
        })
      }
      return Promise.resolve({ sessionKey: parentSessionKey }) as Promise<T>
    })
    const rpc = {
      call: rpcCall as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => true)
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      busySendMode: ref<BusySendMode>('steer'),
      enqueuePendingInput,
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(sendCount).toBe(1))
    const ownerRequestId = String(rpcCall.mock.calls[0]?.[1]?.clientRequestId)

    inputText.value = 'follow the edited question'
    await harness.api.onSend()

    expect(sendCount).toBe(1)
    expect(enqueuePendingInput).toHaveBeenCalledWith('follow the edited question', {
      ownerRequestId,
    })

    resolveFork({ sessionKey: childSessionKey, task_id: 'task-child' })
    await forkSend
  })

  it('does not drain a follow-up over a restored fork draft after rejection', async () => {
    const inputText = ref('edited question')
    let rejectFork!: (reason: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>((_resolve, reject) => {
        rejectFork = reject
      })) as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => {
      inputText.value = ''
      return true
    })
    const harness = makeOptions({
      rpc,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      enqueuePendingInput,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    inputText.value = 'queued follow-up'
    await harness.api.onSend()

    rejectFork(Object.assign(new Error('database busy'), { accepted: false }))
    await forkSend

    expect(inputText.value).toBe('edited question')
    expect(enqueuePendingInput).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(harness.options.flushDeferredPendingDrain).not.toHaveBeenCalled()
    expect(harness.options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
  })

  it('does not let an old handoff gate queue input in a newly selected session', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const otherSessionKey = 'agent:main:webchat:other'
    const sessionKey = ref(parentSessionKey)
    const inputText = ref('edited question')
    let finishHandoff!: () => void
    const hydration = new Promise<void>((resolve) => {
      finishHandoff = resolve
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
      await hydration
    })
    let sendCount = 0
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method !== 'chat.send') return Promise.resolve({}) as Promise<T>
        sendCount += 1
        if (sendCount === 1) {
          return Promise.resolve({ sessionKey: childSessionKey, task_id: 'task-child' }) as Promise<T>
        }
        return Promise.resolve({ sessionKey: params?.sessionKey, task_id: 'task-other' }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const enqueuePendingInput = vi.fn(() => true)
    const harness = makeOptions({
      rpc,
      sessionKey,
      inputText,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
      enqueuePendingInput,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(adoptResponseSession).toHaveBeenCalledOnce())

    sessionKey.value = otherSessionKey
    harness.stream.isStreaming.value = false
    inputText.value = 'question for other session'
    await harness.api.onSend()

    expect(sendCount).toBe(2)
    expect(rpc.call).toHaveBeenLastCalledWith('chat.send', expect.objectContaining({
      sessionKey: otherSessionKey,
      message: 'question for other session',
    }))
    expect(enqueuePendingInput).not.toHaveBeenCalled()

    finishHandoff()
    await forkSend
    expect(harness.options.flushDeferredPendingDrain).not.toHaveBeenCalled()
  })

  it('adopts and aborts a fork child when its response arrives after Stop', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    let resolveFork!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveFork = resolve as (value: unknown) => void
          })
        }
        if (params?.sessionKey === childSessionKey) {
          return Promise.reject(new Error('socket closed')) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))
    const optimisticClientId = harness.options.messages.value[0]?.clientId
    harness.api.onStop()

    resolveFork({
      sessionKey: childSessionKey,
      task_id: 'task-child',
      user_message_id: 'message-child',
    })
    await forkSend

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: childSessionKey,
      taskId: 'task-child',
      source: 'webui_stale_send',
    })
    expect(harness.options.messages.value.find(
      message => message.clientId === optimisticClientId,
    )?.messageId).toBeUndefined()
    expect(harness.options.aborted.value).toBe(true)
    expect(harness.options.activeStreamTaskId.value).toBe(STOPPED_STREAM_TASK_ID)
    expect(harness.options.activeStreamSessionKey.value).toBe(childSessionKey)
    expect(harness.stream.isStreaming.value).toBe(false)
  })

  it('still aborts a stopped fork response after navigation to another session', async () => {
    pushToast.mockClear()
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const otherSessionKey = 'agent:main:webchat:other'
    const sessionKey = ref(parentSessionKey)
    let resolveFork!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>(method: string, params?: Record<string, unknown>) => {
        if (method === 'chat.send') {
          return new Promise<T>((resolve) => {
            resolveFork = resolve as (value: unknown) => void
          })
        }
        if (params?.sessionKey === childSessionKey) {
          return Promise.reject(new Error('socket closed')) as Promise<T>
        }
        return Promise.resolve({ aborted: true }) as Promise<T>
      }) as UseChatSendOptions['rpc']['call'],
    }
    const adoptResponseSession = vi.fn()
    const harness = makeOptions({
      rpc,
      sessionKey,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const forkSend = harness.api.onSend()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))
    harness.api.onStop()
    sessionKey.value = otherSessionKey
    harness.options.activeStreamTaskId.value = ''

    resolveFork({ sessionKey: childSessionKey, task_id: 'task-child-late' })
    await forkSend

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: childSessionKey,
      taskId: 'task-child-late',
      source: 'webui_stale_send',
    })
    expect(adoptResponseSession).not.toHaveBeenCalled()
    expect(sessionKey.value).toBe(otherSessionKey)
    await Promise.resolve()
    expect(harness.options.messages.value).not.toContainEqual(expect.objectContaining({
      role: 'system',
      text: 'Stop could not reach the server — the run may still be finishing.',
    }))
    expect(pushToast).toHaveBeenCalledWith(
      'Stop could not reach the server — the run may still be finishing.',
      { tone: 'warn', duration: 8000 },
    )
  })

  it('binds an orphan message id and reconciles history for an accepted queue error', async () => {
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('queue bookkeeping failed'), {
        accepted: true,
        retryable: false,
        details: { orphan_message_id: 'message-orphan' },
      })),
    }
    const harness = makeOptions({ rpc })

    await harness.api.onSend()

    expect(harness.options.messages.value[0]).toMatchObject({
      role: 'user',
      messageId: 'message-orphan',
    })
    expect(harness.options.scheduleHistorySync).toHaveBeenCalledOnce()
    expect(harness.options.inputText.value).toBe('')
  })

  it('adopts the child without binding its orphan id onto the parent after a dirty fork error', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    const messages = ref<ChatMessage[]>([])
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('queue bookkeeping failed'), {
        code: 'QUEUE_FULL_DIRTY',
        accepted: true,
        retryable: false,
        details: {
          session_key: childSessionKey,
          orphan_message_id: 'message-child-orphan',
        },
      })),
    }
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc,
      sessionKey,
      messages,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })

    await harness.api.onSend()

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(messages.value.find(message => message.role === 'user')?.messageId).toBeUndefined()
    expect(harness.options.scheduleHistorySync).toHaveBeenCalledOnce()
    expect(harness.stream.startStreaming).toHaveBeenCalledOnce()
    expect(harness.stream.endStreaming).toHaveBeenCalledOnce()
    expect(harness.options.schedulePendingDrainAfterTerminal).toHaveBeenCalledOnce()
    expect(sessionKey.value).toBe(childSessionKey)
  })

  it('does not abort unrelated child work for a stopped dirty fork rejection', async () => {
    const parentSessionKey = 'agent:main:webchat:parent'
    const childSessionKey = 'agent:main:webchat:child'
    const sessionKey = ref(parentSessionKey)
    let rejectSend!: (reason: unknown) => void
    const rpcCall = vi.fn(<T = unknown>(method: string) => {
      if (method === 'chat.abort') return Promise.resolve({ aborted: true }) as Promise<T>
      return new Promise<T>((_resolve, reject) => {
        rejectSend = reject
      })
    })
    const adoptResponseSession = vi.fn(async (key: string) => {
      sessionKey.value = key
    })
    const harness = makeOptions({
      rpc: { call: rpcCall as UseChatSendOptions['rpc']['call'] },
      sessionKey,
      pendingForkBeforeMessageId: ref('msg-B'),
      adoptResponseSession,
    })
    harness.stream.startStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = true
    })
    harness.stream.endStreaming = vi.fn(() => {
      harness.stream.isStreaming.value = false
    })

    const send = harness.api.onSend()
    await vi.waitFor(() => expect(rpcCall).toHaveBeenCalledWith(
      'chat.send',
      expect.objectContaining({ sessionKey: parentSessionKey }),
    ))
    harness.api.onStop()
    rejectSend(Object.assign(new Error('queue bookkeeping failed'), {
      code: 'QUEUE_FULL_DIRTY',
      accepted: true,
      retryable: false,
      details: {
        session_key: childSessionKey,
        orphan_message_id: 'message-child-orphan',
      },
    }))
    await send

    expect(adoptResponseSession).toHaveBeenCalledWith(childSessionKey, expect.any(String))
    expect(rpcCall).not.toHaveBeenCalledWith('chat.abort', expect.objectContaining({
      sessionKey: childSessionKey,
    }))
  })

  it('surfaces a terminal steer failure without ending the existing stream', async () => {
    const activeStreamTaskId = ref('task-current')
    const activeStreamSessionKey = ref('agent:main:webchat:test')
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-steer-failed',
        task_status: 'failed',
        terminal_reason: 'activation_failed',
        terminal_message: 'The steer request could not be activated.',
      }),
    }
    const { api, options, stream } = makeOptions({
      rpc,
      activeStreamTaskId,
      activeStreamSessionKey,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    await api.onSend()

    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(activeStreamTaskId.value).toBe('task-current')
    expect(activeStreamSessionKey.value).toBe('agent:main:webchat:test')
    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The steer request could not be activated.',
      errorCode: 'activation_failed',
      terminalNotice: true,
    })
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
  })

  it('does not materialize a stale steer terminal response in the newly selected session', async () => {
    let resolveSend!: (value: unknown) => void
    const rpc = {
      call: vi.fn(<T = unknown>() => new Promise<T>((resolve) => {
        resolveSend = resolve as (value: unknown) => void
      })) as UseChatSendOptions['rpc']['call'],
    }
    const sessionKey = ref('agent:main:webchat:first')
    const { api, options, stream } = makeOptions({
      rpc,
      sessionKey,
      busySendMode: ref<BusySendMode>('steer'),
    })
    stream.isStreaming.value = true

    const send = api.onSend()
    sessionKey.value = 'agent:main:webchat:second'
    resolveSend({
      sessionKey: 'agent:main:webchat:first',
      task_id: 'task-steer-failed',
      task_status: 'failed',
      terminal_reason: 'activation_failed',
      terminal_message: 'This belongs to the previous session.',
    })
    await send

    expect(options.messages.value.some(message => message.role === 'error')).toBe(false)
    expect(options.scheduleHistorySync).not.toHaveBeenCalled()
    expect(stream.endStreaming).not.toHaveBeenCalled()
  })

  it('uses terminal_reason when a terminal replay has no terminal message', async () => {
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-old',
        replayed: true,
        task_status: 'timeout',
        terminal_reason: 'Provider did not respond; retry is safe.',
      }),
    }
    const { api, options } = makeOptions({ rpc })

    await api.onSend()

    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'Provider did not respond; retry is safe.',
      errorCode: 'timeout',
    })
  })

  it('invalidates the previous task id before a fresh send is accepted', async () => {
    let resolveSend!: (value: unknown) => void
    const call: UseChatSendOptions['rpc']['call'] = <T = unknown>() => new Promise<T>((resolve) => {
      resolveSend = resolve as (value: unknown) => void
    })
    const rpc = {
      call: vi.fn(call) as UseChatSendOptions['rpc']['call'],
    }
    const activeStreamTaskId = ref('task-old')
    const activeStreamSessionKey = ref('')
    const { api } = makeOptions({ rpc, activeStreamTaskId, activeStreamSessionKey })

    const send = api.onSend()

    expect(activeStreamTaskId.value).not.toBe('task-old')
    expect(activeStreamTaskId.value).toBeTruthy()
    expect(activeStreamSessionKey.value).toBe('agent:main:webchat:test')

    resolveSend({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-new',
    })
    await send

    expect(activeStreamTaskId.value).toBe('task-new')
  })

  it('binds the accepted task through the event handler boundary', async () => {
    const bindActiveStreamTask = vi.fn()
    const rpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_id: 'task-new',
      }),
    }
    const { api } = makeOptions({ rpc, bindActiveStreamTask })

    await api.onSend()

    expect(bindActiveStreamTask).toHaveBeenCalledWith('task-new')
  })

  it('stops the whole session that owns the stream without trusting a stale task id', () => {
    const activeStreamTaskId = ref('task-old')
    const activeStreamSessionKey = ref('agent:main:webchat:old')
    const { api, rpc, stream } = makeOptions({
      sessionKey: ref('agent:main:webchat:new'),
      activeStreamTaskId,
      activeStreamSessionKey,
    })
    stream.isStreaming.value = true

    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:old',
      source: 'webui_stop',
    })
    expect(activeStreamTaskId.value).not.toBe('task-old')
  })

  it('stops an active subagent group after the parent stream has ended', () => {
    const { api, rpc, stream } = makeOptions({ canStop: () => true })
    stream.isStreaming.value = false

    api.onStop()

    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      source: 'webui_stop',
    })
  })

  it('does not let a stopped send response rebind the next turn', async () => {
    const pendingResponses: Array<(value: unknown) => void> = []
    const rpc = {
      call: vi.fn(<T = unknown>(method: string) => {
        if (method === 'chat.abort') return Promise.resolve({ aborted: true }) as Promise<T>
        return new Promise<T>((resolve) => {
          pendingResponses.push(resolve as (value: unknown) => void)
        })
      }) as UseChatSendOptions['rpc']['call'],
    }
    const inputText = ref('first')
    const messages = ref<ChatMessage[]>([])
    const activeStreamTaskId = ref('')
    const { api, stream } = makeOptions({ rpc, inputText, messages, activeStreamTaskId })
    stream.startStreaming = vi.fn(() => { stream.isStreaming.value = true })
    stream.endStreaming = vi.fn(() => { stream.isStreaming.value = false })

    const firstSend = api.onSend()
    const firstClientMessageId = messages.value[0]?.clientId
    api.onStop()

    inputText.value = 'second'
    const secondSend = api.onSend()
    const secondClientMessageId = messages.value[1]?.clientId

    pendingResponses[1]({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-B',
      message_id: 'message-B',
    })
    await secondSend
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(messages.value.find(message => message.clientId === secondClientMessageId)?.messageId)
      .toBe('message-B')

    pendingResponses[0]({
      sessionKey: 'agent:main:webchat:test',
      task_id: 'task-A',
      user_message_id: 'message-A',
    })
    await firstSend

    expect(activeStreamTaskId.value).toBe('task-B')
    expect(messages.value.find(message => message.clientId === firstClientMessageId)?.messageId)
      .toBe('message-A')
    expect(rpc.call).toHaveBeenCalledWith('chat.abort', {
      sessionKey: 'agent:main:webchat:test',
      taskId: 'task-A',
      source: 'webui_stale_send',
    })
  })
})

describe('useChatSend Ensemble image guard', () => {
  function readyAttachment(
    mime: string,
    overrides: Partial<Attachment> = {},
  ): Attachment {
    return {
      kind: 'staged',
      local_id: 91,
      name: 'input.bin',
      mime,
      file_uuid: 'file-ready',
      ...overrides,
    }
  }

  it('blocks a direct Ensemble image send before any visible or RPC mutation', async () => {
    const image = readyAttachment('image/png', { name: 'photo.png' })
    const pendingAttachments = ref<Attachment[]>([image])
    const inputText = ref('describe this')
    const prepareAttachmentsForSend = vi.fn(async () => true)
    const { api, options, rpc, stream } = makeOptions({
      inputText,
      pendingAttachments,
      modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
      prepareAttachmentsForSend,
    })

    await api.onSend()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(prepareAttachmentsForSend).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(inputText.value).toBe('describe this')
    expect(pendingAttachments.value).toEqual([image])
    expect(options.pendingSessionIntent.value).toBeNull()
    expect(options.closeSlashMenu).not.toHaveBeenCalled()
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('blocks image sends while routing settings are being written', async () => {
    const image = readyAttachment('image/webp')
    const pendingAttachments = ref<Attachment[]>([image])
    const { api, options, rpc } = makeOptions({
      pendingAttachments,
      modelRoutingMode: ref<'off'>('off'),
      modelRoutingSettingsBusy: ref(true),
    })

    await api.onSend()

    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(options.inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([image])
  })

  it.each(['queue', 'steer'] as const)(
    'does not consume an Ensemble image draft in %s mode',
    async (busySendMode) => {
      const image = readyAttachment('image/jpeg')
      const pendingAttachments = ref<Attachment[]>([image])
      const enqueuePendingInput = vi.fn(() => true)
      const { api, options, rpc, stream } = makeOptions({
        pendingAttachments,
        busySendMode: ref<BusySendMode>(busySendMode),
        modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
        enqueuePendingInput,
      })
      stream.isStreaming.value = true

      await api.onSend()

      expect(rpc.call).not.toHaveBeenCalled()
      expect(enqueuePendingInput).not.toHaveBeenCalled()
      expect(options.messages.value).toEqual([])
      expect(options.inputText.value).toBe('hello')
      expect(pendingAttachments.value).toEqual([image])
    },
  )

  it('rechecks routing after attachment preparation without consuming the draft', async () => {
    const image = readyAttachment('image/gif')
    const pendingAttachments = ref<Attachment[]>([image])
    const modelRoutingMode = ref<'off' | 'llm_ensemble'>('off')
    const prepareAttachmentsForSend = vi.fn(async () => {
      modelRoutingMode.value = 'llm_ensemble'
      return true
    })
    const { api, options, rpc } = makeOptions({
      pendingAttachments,
      modelRoutingMode,
      prepareAttachmentsForSend,
    })

    await api.onSend()

    expect(prepareAttachmentsForSend).toHaveBeenCalledOnce()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(options.messages.value).toEqual([])
    expect(options.inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([image])
  })

  it('blocks a recovered image retry after the user switches to Ensemble', async () => {
    const image = readyAttachment('image/jpg', { name: 'photo.jpg' })
    const pendingAttachments = ref<Attachment[]>([image])
    const modelRoutingMode = ref<'off' | 'llm_ensemble'>('off')
    const rpc = {
      call: vi.fn().mockRejectedValue(new Error('connection lost')),
    }
    const { api, options } = makeOptions({ rpc, pendingAttachments, modelRoutingMode })

    await api.onSend()
    expect(rpc.call).toHaveBeenCalledOnce()
    modelRoutingMode.value = 'llm_ensemble'

    await api.onSend()

    expect(rpc.call).toHaveBeenCalledOnce()
    expect(options.inputText.value).toBe('hello')
    expect(pendingAttachments.value).toEqual([image])
  })

  it('preserves an auto-drained queued image after routing switches to Ensemble', async () => {
    vi.useFakeTimers()
    try {
      const image = readyAttachment('image/png')
      const inputText = ref('queued image')
      const pendingAttachments = ref<Attachment[]>([image])
      const pendingSessionIntent = ref<string | null>(null)
      const sessionKey = ref('agent:main:webchat:test')
      const modelRoutingMode = ref<'off' | 'llm_ensemble'>('off')
      const { stream } = makeOptions()
      stream.isStreaming.value = true
      let sendCurrentInput: () => void = () => {}
      const pending = useChatPendingQueue({
        sessionKey,
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        isStreaming: stream.isStreaming,
        isBlocked: () => false,
        autoResizeTextarea: vi.fn(),
        sendCurrentInput: () => sendCurrentInput(),
        resetInputHistory: vi.fn(),
        hasComposer: () => true,
      })
      const { api, options, rpc } = makeOptions({
        inputText,
        pendingAttachments,
        pendingSessionIntent,
        sessionKey,
        modelRoutingMode,
        busySendMode: pending.busySendMode,
        stream,
        enqueuePendingInput: pending.enqueuePendingInput,
        popAllPendingIntoComposer: pending.popAllPendingIntoComposer,
      })
      sendCurrentInput = () => { void api.onSend() }

      await api.onSend()
      expect(pending.pendingQueue.value).toHaveLength(1)
      expect(inputText.value).toBe('')
      expect(pendingAttachments.value).toEqual([])

      modelRoutingMode.value = 'llm_ensemble'
      pending.schedulePendingDrainAfterTerminal()
      stream.isStreaming.value = false
      await nextTick()
      await vi.runAllTimersAsync()
      await nextTick()

      expect(pending.pendingQueue.value).toEqual([])
      expect(rpc.call).not.toHaveBeenCalled()
      expect(options.messages.value).toEqual([])
      expect(inputText.value).toBe('queued image')
      expect(pendingAttachments.value).toEqual([image])
      pending.cleanup()
    } finally {
      vi.useRealTimers()
    }
  })

  it('lets a handled local slash command run without consuming attached images', async () => {
    const image = readyAttachment('image/png')
    const pendingAttachments = ref<Attachment[]>([image])
    const inputText = ref('/status')
    const executeSlashCommand = vi.fn(async () => true)
    const { api, options, rpc } = makeOptions({
      inputText,
      pendingAttachments,
      modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
      executeSlashCommand,
    })

    await api.onSend()

    expect(executeSlashCommand).toHaveBeenCalledWith('/status')
    expect(rpc.call).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/status')
    expect(pendingAttachments.value).toEqual([image])
    expect(options.messages.value).toEqual([])
  })

  it.each(['application/pdf', 'image/svg+xml', 'image/tiff'])(
    'does not block the non-model-image MIME %s in Ensemble mode',
    async (mime) => {
      const pendingAttachments = ref<Attachment[]>([readyAttachment(mime)])
      const { api, rpc } = makeOptions({
        pendingAttachments,
        modelRoutingMode: ref<'llm_ensemble'>('llm_ensemble'),
      })

      await api.onSend()

      expect(rpc.call).toHaveBeenCalledWith('chat.send', expect.objectContaining({
        attachments: [expect.objectContaining({ mime })],
      }))
    },
  )

  it('localizes a defensive server rejection while preserving its error code', async () => {
    const rpc = {
      call: vi.fn().mockRejectedValue(Object.assign(new Error('server fallback text'), {
        code: 'ensemble_multimodal_unsupported',
        retryable: false,
      })),
    }
    const { api, options } = makeOptions({ rpc })

    await api.onSend()

    expect(options.messages.value[options.messages.value.length - 1]).toMatchObject({
      role: 'error',
      errorCode: 'ensemble_multimodal_unsupported',
      text: "Ensemble doesn't support image input yet. Switch to single-model routing and try again.",
    })
  })

  it('localizes a terminal response code but leaves an unknown server message unchanged', async () => {
    const knownRpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_status: 'failed',
        terminal_reason: 'ensemble_multimodal_unsupported',
        terminal_message: 'server fallback text',
      }),
    }
    const known = makeOptions({ rpc: knownRpc })
    await known.api.onSend()
    expect(known.options.messages.value[known.options.messages.value.length - 1]).toMatchObject({
      errorCode: 'ensemble_multimodal_unsupported',
      text: "Ensemble doesn't support image input yet. Switch to single-model routing and try again.",
    })

    const unknownRpc = {
      call: vi.fn().mockResolvedValue({
        sessionKey: 'agent:main:webchat:test',
        task_status: 'failed',
        terminal_reason: 'provider_custom_failure',
        terminal_message: 'Provider supplied this exact explanation.',
      }),
    }
    const unknown = makeOptions({ rpc: unknownRpc })
    await unknown.api.onSend()
    expect(unknown.options.messages.value[unknown.options.messages.value.length - 1]).toMatchObject({
      errorCode: 'provider_custom_failure',
      text: 'Provider supplied this exact explanation.',
    })
  })
})
