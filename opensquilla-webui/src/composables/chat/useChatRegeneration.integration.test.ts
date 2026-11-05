// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, effectScope, nextTick, ref } from 'vue'
import type { UseChatSendOptions as DomainUseChatSendOptions } from './useChatSend'
import { createChatSendHarness, memoryDeliveryWal as memoryHandoffWal } from './chatSendTestHarness'
import { createV4TurnCommandsFromRpcClient } from '@/adapters/gateway/turnCommandsV4'
import { createV4AttachmentContentAccess } from '@/adapters/gateway/attachmentAccessV4'
import { httpBinaryResponse, httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import { snapshotSteerRequest, useChatSteerDelivery } from './useChatSteerDelivery'
import { useChatMessageActions } from './useChatMessageActions'
import { useChatAttachments } from './useChatAttachments'
import { useChatTaskOwnership } from './useChatTaskOwnership'
import { TurnCommandError } from '@/modules/turnCommands'
import { localizedChatErrorMessage } from '@/utils/chat/errors'
import { recoverMessageAttachments } from '@/utils/chat/regenerateAttachments'
import type { Attachment, ChatMessage, ChatPendingItem, ChatRenderedMessage, DisplayAttachment } from '@/types/chat'
import type { CollaborationMode } from '@/types/plans'
import type { BusySendMode } from './useChatPendingQueue'

const pushToast = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.clearAllMocks() })

interface UseChatSendOptions extends Omit<DomainUseChatSendOptions, 'durableDelivery'> {
  durableDelivery?: DomainUseChatSendOptions['durableDelivery']
  // Raw transport exists only inside this Adapter harness; production options
  // contain the already-typed TurnCommands port.
  rpc: { call: any }
}

type SendHarnessOverrides = Partial<UseChatSendOptions> & {
  methodAvailability?: (method: string) => boolean
}

function makeOptions(overrides: SendHarnessOverrides = {}) {
  const { rpc: rpcOverride, methodAvailability, ...sendOverrides } = overrides
  const rpc = rpcOverride ?? {
    call: vi.fn().mockResolvedValue({ sessionKey: 'agent:main:webchat:test' }),
  }
  const turnCommands = overrides.turnCommands ?? createV4TurnCommandsFromRpcClient(
    rpc as unknown as Parameters<typeof createV4TurnCommandsFromRpcClient>[0],
  )
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
    appendToolEnd: vi.fn(),
    appendToolResult: vi.fn(),
    appendArtifact: vi.fn(),
    reconcileFinalText: vi.fn(),
    resetStreamIdleTimer: vi.fn(),
    clearStreamIdleTimer: vi.fn(),
    setStreamActivity: vi.fn(),
    showThinkingIndicator: vi.fn(),
    hideThinkingIndicator: vi.fn(),
    appendFrame: vi.fn(),
  }
  const messages = overrides.messages ?? ref<ChatMessage[]>([])
  const pendingQueue = ref<ChatPendingItem[]>([])
  const scheduleHistorySync = overrides.scheduleHistorySync ?? vi.fn()
  const steerDelivery = overrides.steerDelivery ?? useChatSteerDelivery({
    messages,
    pendingQueue,
    checkpointForUserMessage: stream.checkpointForUserMessage,
    scheduleHistorySync,
    restoreSteerIntoComposer: overrides.restoreSteerIntoComposer,
  })
  const enqueuePendingSteerAttempt = overrides.enqueuePendingSteerAttempt
    ?? ((payload) => {
      const item: ChatPendingItem = {
        pendingUiId: `pending-ui-${pendingQueue.value.length}`,
        text: payload.request.message,
        attachments: [],
        intent: null,
        ownerSessionKey: payload.request.key,
        steerAttempt: {
          phase: payload.phase || 'submitting',
          request: snapshotSteerRequest(payload.request),
        },
      }
      pendingQueue.value.push(item)
      return item
    })
  const options: UseChatSendOptions = {
    rpc,
    turnCommands,
    inputText: ref('hello'),
    messages,
    sessionKey: ref('agent:main:webchat:test'),
    pendingQueueOwnerContext: ref(null),
    hasPendingQueueWork: () => false,
    pendingInputWal: memoryHandoffWal(),
    busySendMode: ref<BusySendMode>('queue'),
    modelRoutingMode: ref<'off'>('off'),
    modelRoutingSettingsBusy: ref(false),
    elevatedMode: ref(''),
    runMode: ref('safe'),
    pendingAttachments: ref<Attachment[]>([]),
    pendingSessionIntent: ref(null),
    initialCollaborationMode: ref<CollaborationMode>('default'),
    initialRoutingMode: ref<'direct'>('direct'),
    pendingForkBeforeMessageId: ref(null),
    aborted: ref(false),
    activeStreamTaskId: ref(''),
    activeStreamSessionKey: ref(''),
    autoScroll: ref(false),
    stream,
    normalizeElevatedMode: mode => mode,
    adoptResponseSession: vi.fn(),
    scheduleHistorySync,
    schedulePendingDrainAfterTerminal: vi.fn(),
    flushDeferredPendingDrain: vi.fn(),
    isCompactInFlightForCurrentSession: () => false,
    hasPendingAttachmentWork: () => false,
    enqueuePendingInput: vi.fn(() => true),
    enqueuePendingSteerAttempt,
    steerDelivery,
    popAllPendingIntoComposer: vi.fn(() => false),
    classifySlashCommand: vi.fn(async () => 'registered' as const),
    executeSlashCommand: vi.fn(async () => false),
    closeSlashMenu: vi.fn(),
    autoResizeTextarea: vi.fn(),
    scrollToBottom: vi.fn(),
    ...sendOverrides,
  }
  if (!sendOverrides.turnCommands) {
    // Keep receipt reads on the real Adapter contract, with accepted identities
    // matching the captured wire request. The WAL and sender remain real.
    const receiptRpc = {
      async call(method: string, params?: Record<string, unknown>, requestOptions?: unknown) {
        if (method !== 'turns.receipt.get') return requestOptions
          ? options.rpc.call(method, params, requestOptions) : options.rpc.call(method, params)
        const original = params?.originalRequest as Record<string, unknown>
        const raw = await options.rpc.call(method, original)
        return {
          status: 'found', accepted: true, requestFingerprint: `sha256:${'a'.repeat(64)}`,
          receipt: {
            requestSessionKey: original.sessionKey || original.key,
            sessionKey: raw.sessionKey || raw.key || original.sessionKey || original.key,
            sessionId: 'synthetic-session-id', sessionEpoch: 1,
            clientRequestId: original.clientRequestId || original.client_request_id,
            messageId: raw.user_message_id || raw.message_id || 'synthetic-message-id',
            taskId: raw.task_id || raw.taskId || null, taskStatus: raw.task_status || null,
            ...(String(params?.operation).includes('steer') ? { steer: {
              status: 'accepted', accepted: true, replayed: true, key: original.key,
              session_key: original.key, session_id: 'synthetic-session-id',
              task_id: raw.turn_id || null, turn_id: raw.turn_id || null,
              user_message_id: raw.user_message_id || 'synthetic-steer-message',
              surface_id: null, disposition: 'steering', revision: 1, fallback_safe: false,
              ...raw, client_request_id: original.client_request_id, client_message_id: original.client_message_id,
            } } : {}),
          },
        }
      },
    }
    options.turnCommands = createV4TurnCommandsFromRpcClient(
      receiptRpc as Parameters<typeof createV4TurnCommandsFromRpcClient>[0],
      method => method === 'turns.receipt.get' || Boolean(methodAvailability?.(method)),
    )
  }
  return { ...createChatSendHarness(options), rpc, stream, pendingQueue }
}

const imageBytes = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1kAAAAASUVORK5CYII='
const secondBytes = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
const originalText = 'Describe the left shape.'
const session = 'agent:main:webchat:test'
const imageBlob = (data = imageBytes) => new Blob([Uint8Array.from(atob(data), char => char.charCodeAt(0))], { type: 'image/png' })
const inlineImage = (name = 'shape-card.png', data = imageBytes): DisplayAttachment => ({
  kind: 'inline', displayId: name, renderKey: name, name, mime: 'image/png', downloadData: data,
})
const retainedImage = (name = 'shape-card.png', sha = 'a'.repeat(64)): DisplayAttachment => ({
  kind: 'file', displayId: name, renderKey: name, name, mime: 'image/png', sha256_ref: sha,
})
const unrelatedDraft: Attachment = { kind: 'inline', local_id: 99, name: 'draft-only.png', mime: 'image/png', data: secondBytes }
const deferred = <T>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function scenario(attachments: DisplayAttachment[] = [inlineImage()], options: {
  draft?: Attachment[]
  text?: string
  taskOwnership?: DomainUseChatSendOptions['taskOwnership']
  requestBinary?: NonNullable<Parameters<typeof httpTransportTestDouble>[0]>['requestBinary']
  requestJson?: NonNullable<Parameters<typeof httpTransportTestDouble>[0]>['requestJson']
} = {}) {
  const originalUser: ChatMessage = { role: 'user', ts: '2026-11-05T06:00:00.000Z', messageId: 'user-original', text: options.text ?? originalText, attachments }
  const answer: ChatMessage = { role: 'assistant', ts: '2026-11-05T06:00:01.000Z', messageId: 'assistant-original', text: 'triangle' }
  const requestBinary = options.requestBinary ?? vi.fn(async () => httpBinaryResponse(imageBlob(), { filename: 'shape-card.png' }))
  const http = httpTransportTestDouble({ requestBinary, requestJson: options.requestJson })
  const content = createV4AttachmentContentAccess(http)
  const h = makeOptions({ taskOwnership: options.taskOwnership, methodAvailability: method => method === 'skills.candidates', inputText: ref('unrelated draft text'), messages: ref([originalUser, answer]), pendingAttachments: ref(options.draft ?? []) })
  const scope = effectScope()
  scopes.push(scope)
  const actions = scope.run(() => useChatMessageActions({
    messages: h.options.messages, inputText: h.options.inputText,
    pendingAttachments: h.options.pendingAttachments,
    isStreaming: h.stream.isStreaming, sessionIdentity: () => h.options.sessionKey.value,
    sanitizeCopyText: text => text, stripTimePrefix: text => text,
    autoResizeTextarea: vi.fn(), focusComposer: vi.fn(),
    readAttachments: (message, signal) => recoverMessageAttachments(content, message, h.options.sessionKey.value, signal),
    sendRegeneratedInput: h.api.sendRegeneratedInput,
    sendUsageBarrierReplay: vi.fn(async () => false),
    pendingForkBeforeMessageId: h.options.pendingForkBeforeMessageId,
  }))!
  const clicked = { ...answer, sourceIndex: 1 } as ChatRenderedMessage
  const sent = () => h.rpc.call.mock.calls.filter(([method]: unknown[]) => method === 'chat.send')
  return { ...h, actions, originalUser, answer, clicked, requestBinary, sent, scope, content }
}

describe('image editing through the attachment reader and real sender', () => {
  const edit = (h: ReturnType<typeof scenario>) => h.actions.editMessage({
    ...h.originalUser, sourceIndex: 0, displayRole: 'user',
  } as ChatRenderedMessage)

  it('edits an image-only first message without losing the image and sends an exact fork', async () => {
    const h = scenario([inlineImage()], { text: '', draft: [unrelatedDraft] })
    expect(await edit(h)).toBe(true)
    expect(h.options.inputText.value).toBe('')
    expect(h.options.pendingAttachments.value).toEqual([
      expect.objectContaining({ name: 'shape-card.png', data: imageBytes }),
    ])
    expect(h.sent()).toHaveLength(0)
    await h.api.onSend()
    expect(h.sent()).toHaveLength(1)
    expect(h.sent()[0]![1]).toMatchObject({
      forkBeforeMessageId: 'user-original', attachments: [{ name: 'shape-card.png', data: imageBytes }],
    })
    expect(h.sent()[0]![1].message).not.toBe('unrelated draft text')
    expect(h.originalUser.attachments).toEqual([inlineImage()])
  })

  it('edits text and sends every original inline and historical image in order', async () => {
    const h = scenario([inlineImage(), retainedImage('second.png', 'b'.repeat(64))], {
      draft: [unrelatedDraft],
      requestBinary: vi.fn(async () => httpBinaryResponse(imageBlob(secondBytes), { filename: 'second.png' })),
    })
    expect(await edit(h)).toBe(true)
    h.options.inputText.value = 'Compare the two images instead.'
    await h.api.onSend()
    expect(h.sent()[0]![1]).toMatchObject({
      message: 'Compare the two images instead.', forkBeforeMessageId: 'user-original',
      attachments: [{ name: 'shape-card.png', data: imageBytes }, { name: 'second.png', data: secondBytes }],
    })
    expect(h.requestBinary).toHaveBeenCalledWith(`/api/v1/attachments/${'b'.repeat(64)}`, {
      sessionKey: session, signal: expect.any(AbortSignal), timeoutMs: 0,
    })
  })

  it('honors attachment removal and a new image added while editing', async () => {
    const h = scenario([inlineImage(), inlineImage('second.png', secondBytes)])
    expect(await edit(h)).toBe(true)
    h.options.pendingAttachments.value.splice(0, 1)
    h.options.pendingAttachments.value.push({ ...unrelatedDraft, name: 'added.png' })
    await h.api.onSend()
    expect(h.sent()[0]![1].attachments).toEqual([
      expect.objectContaining({ name: 'second.png', data: secondBytes }),
      expect.objectContaining({ name: 'added.png', data: secondBytes }),
    ])
    expect(h.originalUser.attachments).toHaveLength(2)
  })

  it('restores the complete transcript and the prior image draft when edit is cancelled', async () => {
    const h = scenario([retainedImage()], { text: '', draft: [unrelatedDraft] })
    const transcript = h.options.messages.value
    expect(await edit(h)).toBe(true)
    expect(h.actions.cancelEdit()).toBe(true)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.inputText.value).toBe('unrelated draft text')
    expect(h.options.pendingForkBeforeMessageId.value).toBeNull()
    expect(h.sent()).toHaveLength(0)
  })

  it('does not leak the existing image draft into a text-only edited message', async () => {
    const h = scenario([], { draft: [unrelatedDraft] })
    expect(await edit(h)).toBe(true)
    expect(h.options.pendingAttachments.value).toHaveLength(0)
    await h.api.onSend()
    expect(h.sent()[0]![1].attachments ?? []).toHaveLength(0)
    expect(h.sent()[0]![1].forkBeforeMessageId).toBe('user-original')
  })

  it('preserves native workspace references through edit and send', async () => {
    const workspaceFile = { workspaceId: 'workspace-original', relativePath: 'uploads/shape-card.png', name: 'shape-card.png', mime: 'image/png', size: 123 }
    const h = scenario([{ kind: 'file', displayId: 'native-image', renderKey: 'native-image', name: workspaceFile.name, mime: workspaceFile.mime, workspaceFile }])
    expect(await edit(h)).toBe(true)
    await h.api.onSend()
    expect(h.sent()[0]![1]).toMatchObject({ workspaceFiles: [workspaceFile] })
    expect(h.sent()[0]![1].attachments ?? []).toHaveLength(0)
    expect(h.requestBinary).not.toHaveBeenCalled()
  })

  it('keeps the edited input recoverable after a definite send rejection', async () => {
    const h = scenario([inlineImage()], { draft: [unrelatedDraft] })
    const transcript = h.options.messages.value
    expect(await edit(h)).toBe(true)
    h.rpc.call.mockRejectedValue(new TurnCommandError('rejected', 'Rejected', 'INVALID_PARAMS', false, false))
    await h.api.onSend()
    expect(h.options.pendingAttachments.value).toEqual([
      expect.objectContaining({ name: 'shape-card.png', data: imageBytes }),
    ])
    expect(h.options.pendingForkBeforeMessageId.value).toBe('user-original')
    expect(h.actions.cancelEdit()).toBe(true)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
  })

  it('restores the prior draft after a rejected edit send refreshes its staged upload token', async () => {
    const now = Date.now()
    const upload = vi.fn()
      .mockResolvedValueOnce({ file_uuid: 'initial-upload', expires_at: now / 1000 + 60, ttl_seconds: 60 })
      .mockResolvedValueOnce({ file_uuid: 'fresh-upload', expires_at: now / 1000 + 7200, ttl_seconds: 3600 })
    const h = scenario([retainedImage()], {
      draft: [unrelatedDraft], requestJson: upload,
      requestBinary: vi.fn(async () => httpBinaryResponse(new Blob([new Uint8Array(2_000_001)], { type: 'image/png' }))),
    })
    const unusedArtifact = vi.fn(async () => { throw new Error('Unexpected artifact access') })
    const staging = h.scope.run(() => useChatAttachments({
      ...h.content, fetchArtifact: unusedArtifact, openArtifact: unusedArtifact,
      openArtifactBlob: unusedArtifact, clearPreviewStorage: unusedArtifact,
    }))!
    h.options.prepareAttachmentsForSend = staging.prepareAttachmentsForSend
    const transcript = h.options.messages.value
    expect(await edit(h)).toBe(true)
    expect(h.options.pendingAttachments.value[0]?.file_uuid).toBe('initial-upload')
    h.rpc.call.mockRejectedValue(new TurnCommandError('rejected', 'Rejected', 'INVALID_PARAMS', false, false))
    const clock = vi.spyOn(Date, 'now').mockReturnValue(now + 120_000)
    try {
      await h.api.onSend()
      expect(upload).toHaveBeenCalledTimes(2)
      expect(h.sent()[0]![1].attachments).toEqual([
        expect.objectContaining({ name: 'shape-card.png', file_uuid: 'fresh-upload' }),
      ])
      expect(h.actions.cancelEdit()).toBe(true)
      expect(h.options.messages.value).toBe(transcript)
      expect(h.options.inputText.value).toBe('unrelated draft text')
      expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    } finally { clock.mockRestore() }
  })

  it('keeps the conversation and draft intact when recovering one of the images fails', async () => {
    const h = scenario([inlineImage(), retainedImage('missing.png')], {
      draft: [unrelatedDraft], requestBinary: vi.fn(async () => { throw new Error('Attachment expired') }),
    })
    const transcript = h.options.messages.value
    expect(await edit(h)).toBe(false)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.inputText.value).toBe('unrelated draft text')
    expect(h.options.pendingForkBeforeMessageId.value).toBeNull()
    expect(h.sent()).toHaveLength(0)
  })

  it.each(['blocked', 'rejected'] as const)(
    'retains cancellation of an unsubmitted later edit when regenerating an earlier turn is %s', async failure => {
      const h = scenario([inlineImage()], { draft: [unrelatedDraft] })
      h.options.messages.value.push(
        { role: 'user', text: 'Second image', messageId: 'user-second', ts: null, attachments: [inlineImage('second.png', secondBytes)] },
        { role: 'assistant', text: 'Second answer', messageId: 'answer-second', ts: null },
      )
      const transcript = h.options.messages.value
      expect(await h.actions.editMessage({ ...transcript[2]!, sourceIndex: 2 } as ChatRenderedMessage)).toBe(true)
      expect(h.options.messages.value).toHaveLength(2)
      if (failure === 'blocked') h.options.isCompactInFlightForCurrentSession = () => true
      else h.rpc.call.mockRejectedValue(new TurnCommandError('rejected', 'Rejected', 'INVALID_PARAMS', false, false))
      expect(await h.actions.regenerateMessage(h.clicked)).toBe(false)
      expect(h.actions.cancelEdit()).toBe(true)
      expect(h.options.messages.value).toBe(transcript)
      expect(h.options.messages.value).toHaveLength(4)
      expect(h.options.inputText.value).toBe('unrelated draft text')
      expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
      expect(h.options.pendingForkBeforeMessageId.value).toBeNull()
    },
  )

  it.each(['navigate', 'navigate-away-and-back', 'cancel', 'dispose', 'draft-text', 'draft-image'] as const)(
    'does not apply a late recovered edit after %s', async change => {
      const read = deferred<ReturnType<typeof httpBinaryResponse>>()
      const requestBinary = vi.fn(() => read.promise)
      const h = scenario([retainedImage()], { draft: [unrelatedDraft], requestBinary })
      const transcript = h.options.messages.value
      const editing = edit(h)
      await vi.waitFor(() => expect(requestBinary).toHaveBeenCalledOnce())
      if (change === 'navigate' || change === 'navigate-away-and-back') {
        h.options.sessionKey.value = 'agent:main:webchat:elsewhere'
        if (change === 'navigate-away-and-back') h.options.sessionKey.value = session
      } else if (change === 'cancel') h.actions.cancelEdit()
      else if (change === 'dispose') h.scope.stop()
      else if (change === 'draft-text') h.options.inputText.value = 'newer text'
      else h.options.pendingAttachments.value.push({ ...unrelatedDraft, local_id: 100, name: 'newer.png' })
      read.resolve(httpBinaryResponse(imageBlob()))
      expect(await editing).toBe(false)
      expect(h.options.messages.value).toBe(transcript)
      expect(h.options.pendingForkBeforeMessageId.value).toBeNull()
      expect(h.options.inputText.value).toBe(change === 'draft-text' ? 'newer text' : 'unrelated draft text')
      expect(h.options.pendingAttachments.value.map(a => a.name)).toEqual(change === 'draft-image' ? ['draft-only.png', 'newer.png'] : ['draft-only.png'])
      expect(h.sent()).toHaveLength(0)
    },
  )
})

describe('image regeneration through the attachment reader and real sender', () => {
  it('sends the original image again and renders it on the accepted fork', async () => {
    const h = scenario()
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()).toHaveLength(1)
    expect(h.sent()[0]![1]).toMatchObject({
      message: originalText, forkBeforeMessageId: 'user-original',
      attachments: [{ name: 'shape-card.png', mime: 'image/png', data: imageBytes }],
    })
    expect(h.options.messages.value.find(row => row.role === 'user')?.attachments).toEqual([
      expect.objectContaining({ name: 'shape-card.png', data: imageBytes }),
    ])
    expect(h.originalUser.attachments).toEqual([inlineImage()])
    expect(h.requestBinary).not.toHaveBeenCalled()
  })

  it('regenerates an image-only user message with the ordinary image-only prompt', async () => {
    const control = makeOptions({ inputText: ref(''), pendingAttachments: ref<Attachment[]>([{ kind: 'inline', local_id: 1, name: 'shape-card.png', mime: 'image/png', data: imageBytes }]) })
    await control.api.onSend()
    const ordinary = control.rpc.call.mock.calls.find(([method]: unknown[]) => method === 'chat.send')![1]
    const h = scenario([inlineImage()], { text: '' })
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()[0]![1]).toMatchObject({ message: ordinary.message, attachments: [{ data: imageBytes }] })
  })

  it('replays original skills and path references alongside the original image', async () => {
    const h = scenario([inlineImage()], { text: 'Compare this image with the referenced file.\n/workspace/chart.png' })
    h.originalUser.localPathReferences = ['/workspace/chart.png']
    h.originalUser.selectedSkills = [{ name: 'inspect-chart', instanceId: 'skill-1', digest: 'a'.repeat(64) }]
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()[0]![1]).toMatchObject({
      localPathReferences: ['/workspace/chart.png'], selectedSkills: h.originalUser.selectedSkills,
      attachments: [{ name: 'shape-card.png', data: imageBytes }],
    })
  })

  it('does not truncate the visible transcript until the regenerated input is accepted', async () => {
    const h = scenario()
    const response = deferred<{ sessionKey: string }>()
    h.rpc.call.mockImplementation((method: string) => method === 'chat.send'
      ? response.promise : Promise.resolve({ sessionKey: session }))
    const transcript = h.options.messages.value
    const regeneration = h.actions.regenerateMessage(h.clicked)
    await vi.waitFor(() => expect(h.sent()).toHaveLength(1))
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.messages.value).toHaveLength(2)
    response.resolve({ sessionKey: session })
    expect(await regeneration).toBe(true)
    expect(h.options.messages.value).not.toBe(transcript)
    expect(h.options.messages.value.find(row => row.role === 'user')?.attachments).toHaveLength(1)
  })

  it('recovers a reopened history image with the owning session before resending it', async () => {
    const h = scenario([retainedImage()])
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.requestBinary).toHaveBeenCalledWith(`/api/v1/attachments/${'a'.repeat(64)}`, {
      sessionKey: session, signal: expect.any(AbortSignal), timeoutMs: 0,
    })
    expect(h.sent()[0]![1]).toMatchObject({ attachments: [{ name: 'shape-card.png', data: imageBytes }] })
  })

  it('preserves every image and its order when inline and retained images are mixed', async () => {
    const h = scenario([inlineImage(), retainedImage('second.png', 'b'.repeat(64))], {
      requestBinary: vi.fn(async () => httpBinaryResponse(imageBlob(secondBytes), { filename: 'second.png' })),
    })
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()[0]![1]).toMatchObject({ attachments: [
      { name: 'shape-card.png', data: imageBytes }, { name: 'second.png', data: secondBytes },
    ] })
  })

  it('keeps an unrelated composer draft untouched and excludes it from the request', async () => {
    const h = scenario([inlineImage()], { draft: [unrelatedDraft] })
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()[0]![1]).toMatchObject({ attachments: [{ name: 'shape-card.png', data: imageBytes }] })
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.inputText.value).toBe('unrelated draft text')
  })

  it('does not attach the current image draft when regenerating a text-only input', async () => {
    const h = scenario([], { draft: [unrelatedDraft] })
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()[0]![1].attachments ?? []).toHaveLength(0)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
  })

  it('blocks regeneration until task hydration resolves, even before any task is known', async () => {
    const ownership = useChatTaskOwnership(false)
    // Exercise the hydration fence independently of the aggregate work gate.
    ownership.hasAuthoritativeWork = computed(() => false)
    const h = scenario([inlineImage()], { draft: [unrelatedDraft], taskOwnership: ownership })
    const transcript = h.options.messages.value
    expect(ownership.runningTaskId.value).toBe('')
    expect(ownership.queuedTaskIds.value.size).toBe(0)
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(false)
    expect(h.sent()).toHaveLength(0)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.inputText.value).toBe('unrelated draft text')
  })

  it('shows definite rejection feedback without replacing the transcript or composer draft', async () => {
    const h = scenario([inlineImage()], { draft: [unrelatedDraft] })
    const failure = new TurnCommandError('rejected', 'Raw upstream detail must stay private', 'INVALID_PARAMS', false, false)
    h.rpc.call.mockRejectedValue(failure)
    const transcript = h.options.messages.value
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(false)
    expect(h.sent()).toHaveLength(1)
    expect(pushToast).toHaveBeenCalledWith(localizedChatErrorMessage('INVALID_PARAMS', ''), { tone: 'danger' })
    expect(pushToast.mock.calls.some(call => String(call[0]).includes(failure.message))).toBe(false)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.messages.value).toHaveLength(2)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.inputText.value).toBe('unrelated draft text')
  })

  it('resends the original native workspace reference without mixing a composer image', async () => {
    const workspaceFile = { workspaceId: 'workspace-original', relativePath: 'uploads/shape-card.png', name: 'shape-card.png', mime: 'image/png', size: 123 }
    const original: DisplayAttachment = { kind: 'file', displayId: 'native-image', renderKey: 'native-image', name: workspaceFile.name, mime: workspaceFile.mime, workspaceFile }
    const h = scenario([original], { draft: [unrelatedDraft] })
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(true)
    expect(h.sent()[0]![1]).toMatchObject({ workspaceFiles: [workspaceFile] })
    expect(h.sent()[0]![1].attachments ?? []).toHaveLength(0)
    expect(h.requestBinary).not.toHaveBeenCalled()
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.messages.value.find(row => row.role === 'user')?.attachments).toEqual([
      expect.objectContaining({ workspaceFile }),
    ])
  })

  it('keeps the transcript and draft intact if any original image cannot be recovered', async () => {
    const h = scenario([inlineImage(), retainedImage('missing.png')], {
      draft: [unrelatedDraft], requestBinary: vi.fn(async () => { throw new Error('Attachment expired') }),
    })
    const transcript = h.options.messages.value
    expect(await h.actions.regenerateMessage(h.clicked)).toBe(false)
    expect(h.sent()).toHaveLength(0)
    expect(h.options.messages.value).toBe(transcript)
    expect(h.options.messages.value).toHaveLength(2)
    expect(h.options.pendingForkBeforeMessageId.value).toBe(null)
    expect(h.options.pendingAttachments.value).toEqual([unrelatedDraft])
    expect(h.options.inputText.value).toBe('unrelated draft text')
  })

  it.each(['navigate', 'navigate-away-and-back', 'cancel', 'dispose'] as const)(
    'does not send a late recovered image after %s', async change => {
      const read = deferred<ReturnType<typeof httpBinaryResponse>>()
      const requestBinary = vi.fn(() => read.promise)
      const h = scenario([retainedImage()], { requestBinary })
      const transcript = h.options.messages.value
      const regeneration = h.actions.regenerateMessage(h.clicked)
      await vi.waitFor(() => expect(requestBinary).toHaveBeenCalledTimes(1))
      if (change === 'navigate' || change === 'navigate-away-and-back') {
        h.options.sessionKey.value = 'agent:main:webchat:elsewhere'
        if (change === 'navigate-away-and-back') h.options.sessionKey.value = session
      } else if (change === 'cancel') {
        h.actions.cancelEdit()
      } else h.scope.stop()
      read.resolve(httpBinaryResponse(imageBlob()))
      expect(await regeneration).toBe(false)
      await nextTick()
      expect(h.sent()).toHaveLength(0)
      expect(h.options.messages.value).toBe(transcript)
      expect(h.options.pendingForkBeforeMessageId.value).toBe(null)
    },
  )
})
