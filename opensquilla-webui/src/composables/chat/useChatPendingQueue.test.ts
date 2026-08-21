import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import {
  useChatPendingQueue,
  type UseChatPendingQueueOptions,
} from './useChatPendingQueue'
import type { Attachment, ChatPendingItem, HiddenControlDispatchResult } from '@/types/chat'
import {
  createPendingInputWal,
  type PendingInputWal,
  type PendingInputWalRecord,
  type ResponseHandoffWalRecord,
} from '@/utils/chat/pendingInputWal'

function makeQueue(
  dispatchPendingItem?: (item: ChatPendingItem, ownerSessionKey: string) => Promise<
    'accepted' | 'deferred' | 'not_sent' | 'retryable_failure'
  >,
  isBlocked: () => boolean = () => false,
  dispatchHiddenControl?: (
    item: ChatPendingItem,
    ownerSessionKey: string,
  ) => Promise<'accepted' | 'deferred' | 'not_sent' | 'retryable_failure'>,
  onHiddenControlDispatchResult?: (result: HiddenControlDispatchResult) => void | boolean,
  overrides: Partial<UseChatPendingQueueOptions> = {},
) {
  const sessionKey = ref('agent:main:webchat:test')
  const inputText = ref('')
  const pendingAttachments = ref<Attachment[]>([])
  const pendingSessionIntent = ref<string | null>(null)
  const isStreaming = ref(false)
  const sendCurrentInput = vi.fn()
  const defaultWal = memoryWal().wal
  const queue = useChatPendingQueue({
    sessionKey,
    inputText,
    pendingAttachments,
    pendingSessionIntent,
    isStreaming,
    isBlocked,
    autoResizeTextarea: vi.fn(),
    sendCurrentInput,
    resetInputHistory: vi.fn(),
    hasComposer: () => true,
    dispatchPendingItem,
    dispatchHiddenControl,
    onHiddenControlDispatchResult,
    pendingInputWal: defaultWal,
    supportsMethod: () => false,
    ...overrides,
  })

  return {
    inputText,
    pendingAttachments,
    pendingSessionIntent,
    queue,
    sendCurrentInput,
    sessionKey,
  }
}

function pendingUiId(
  queue: ReturnType<typeof useChatPendingQueue>,
  index: number,
): string {
  const id = queue.pendingQueue.value[index]?.pendingUiId
  expect(id).toBeTruthy()
  return id!
}

function memoryWal(initial: PendingInputWalRecord[] = []) {
  const records = new Map(initial.map(record => [record.pendingInputId, record]))
  const handoffs = new Map<string, ResponseHandoffWalRecord>()
  const wal: PendingInputWal = {
    put: vi.fn(async record => {
      records.set(record.pendingInputId, structuredClone(record))
    }),
    list: vi.fn(async sessionKey => [...records.values()]
      .filter(record => record.sessionKey === sessionKey)
      .sort((left, right) => (
        (left.position ?? Number.MAX_SAFE_INTEGER)
        - (right.position ?? Number.MAX_SAFE_INTEGER)
        || left.createdAt - right.createdAt
      ))),
    putMany: vi.fn(async nextRecords => {
      for (const record of nextRecords) {
        records.set(record.pendingInputId, structuredClone(record))
      }
    }),
    commitOrder: vi.fn(async (
      sessionKey: string,
      orderedIds: string[],
      expectedWalRevisions: Record<string, number>,
    ) => {
      const current = [...records.values()].filter(record => record.sessionKey === sessionKey)
      if (
        current.length !== orderedIds.length
        || orderedIds.some(id => !current.some(record => record.pendingInputId === id))
      ) throw new Error('conflict')
      const committed = orderedIds.map((pendingInputId, position) => {
        const record = records.get(pendingInputId)!
        const revision = record.walRevision ?? 1
        if (expectedWalRevisions[pendingInputId] !== revision) throw new Error('conflict')
        const next = {
          ...record,
          position,
          walRevision: revision + 1,
          updatedAt: Date.now(),
        }
        records.set(pendingInputId, structuredClone(next))
        return next
      })
      return { records: committed }
    }),
    putHandoff: vi.fn(async record => {
      handoffs.set(record.ownerRequestId, structuredClone(record))
    }),
    listHandoffs: vi.fn(async () => [...handoffs.values()].map(record => (
      structuredClone(record)
    ))),
    acceptHandoff: vi.fn(async (ownerRequestId, acceptedSessionKey) => {
      const existing = handoffs.get(ownerRequestId)
      if (!existing) throw new Error('missing handoff')
      const handoff = {
        ...existing,
        state: 'accepted' as const,
        acceptedSessionKey,
        updatedAt: Date.now(),
      }
      handoffs.set(ownerRequestId, handoff)
      const moved = [...records.values()]
        .filter(record => record.ownerRequestId === ownerRequestId)
        .map(record => ({
          ...record,
          sessionKey: acceptedSessionKey,
          ownerRequestId: undefined,
          state: 'saving' as const,
          walRevision: (record.walRevision ?? 1) + 1,
          updatedAt: Date.now(),
        }))
      for (const record of moved) records.set(record.pendingInputId, structuredClone(record))
      return { handoff, records: moved }
    }),
    deleteHandoff: vi.fn(async ownerRequestId => { handoffs.delete(ownerRequestId) }),
    delete: vi.fn(async pendingInputId => {
      records.delete(pendingInputId)
    }),
    close: vi.fn(),
  }
  return { records, handoffs, wal }
}

class TestBroadcastChannel {
  static readonly channels = new Map<string, Set<TestBroadcastChannel>>()

  onmessage: ((event: MessageEvent) => void) | null = null

  constructor(private readonly name: string) {
    const peers = TestBroadcastChannel.channels.get(name) || new Set()
    peers.add(this)
    TestBroadcastChannel.channels.set(name, peers)
  }

  postMessage(message: unknown) {
    for (const peer of TestBroadcastChannel.channels.get(this.name) || []) {
      if (peer === this) continue
      queueMicrotask(() => peer.onmessage?.(new MessageEvent('message', {
        data: structuredClone(message),
      })))
    }
  }

  close() {
    const peers = TestBroadcastChannel.channels.get(this.name)
    peers?.delete(this)
    if (peers?.size === 0) TestBroadcastChannel.channels.delete(this.name)
  }
}

describe('useChatPendingQueue delivery state', () => {
  it('fails closed when the IndexedDB global accessor itself throws', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'indexedDB')
    Object.defineProperty(globalThis, 'indexedDB', {
      configurable: true,
      get() {
        throw new Error('blocked storage accessor')
      },
    })
    try {
      expect(createPendingInputWal()).toBeNull()
    } finally {
      if (descriptor) Object.defineProperty(globalThis, 'indexedDB', descriptor)
      else Reflect.deleteProperty(globalThis, 'indexedDB')
    }
  })

  it('persists the browser WAL before clearing the composer', async () => {
    const { wal } = memoryWal()
    let releaseFirstPut: (() => void) | undefined
    vi.mocked(wal.put).mockImplementationOnce(() => new Promise<void>(resolve => {
      releaseFirstPut = resolve
    }))
    const { inputText, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    inputText.value = 'survives a refresh'

    const queued = queue.enqueuePendingInput(inputText.value)
    expect(inputText.value).toBe('survives a refresh')
    expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('saving')

    releaseFirstPut?.()
    await expect(queued).resolves.toBe(true)
    expect(inputText.value).toBe('')
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('local_only')
    })
    queue.cleanup()
  })

  it.each([
    {
      mutation: 'push',
      mutate: (attachments: Attachment[], replacement: Attachment) => {
        attachments.push(replacement)
      },
    },
    {
      mutation: 'splice',
      mutate: (attachments: Attachment[], replacement: Attachment) => {
        attachments.splice(0, 1, replacement)
      },
    },
    {
      mutation: 'replace',
      mutate: (attachments: Attachment[], replacement: Attachment) => {
        attachments[0] = replacement
      },
    },
    {
      mutation: 'in-place field mutation',
      mutate: (attachments: Attachment[], replacement: Attachment) => {
        if (attachments[0]) attachments[0].name = replacement.name
      },
    },
  ])('keeps attachment edits made with $mutation while the WAL write is pending', async ({
    mutate,
  }) => {
    const { wal } = memoryWal()
    let releaseFirstPut: (() => void) | undefined
    vi.mocked(wal.put).mockImplementationOnce(() => new Promise<void>(resolve => {
      releaseFirstPut = resolve
    }))
    const { inputText, pendingAttachments, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    const original: Attachment = {
      kind: 'staged',
      local_id: 101,
      name: 'original.txt',
      mime: 'text/plain',
      file_uuid: 'upload-original',
    }
    const replacement: Attachment = {
      kind: 'staged',
      local_id: 102,
      name: 'later.txt',
      mime: 'text/plain',
      file_uuid: 'upload-later',
    }
    inputText.value = 'queue the original attachment'
    pendingAttachments.value = [original]

    const queued = queue.enqueuePendingInput(inputText.value)
    mutate(pendingAttachments.value, replacement)
    const composerAfterMutation = [...pendingAttachments.value]

    releaseFirstPut?.()
    await expect(queued).resolves.toBe(true)
    expect(inputText.value).toBe('queue the original attachment')
    expect(pendingAttachments.value).toEqual(composerAfterMutation)
    expect(queue.pendingQueue.value[0]?.attachments).toMatchObject([{
      local_id: 101,
      name: 'original.txt',
    }])
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('local_only')
    })
    queue.cleanup()
  })

  it('keeps a newer draft entered while the WAL write is pending', async () => {
    const { wal } = memoryWal()
    let releaseFirstPut: (() => void) | undefined
    vi.mocked(wal.put).mockImplementationOnce(() => new Promise<void>(resolve => {
      releaseFirstPut = resolve
    }))
    const { inputText, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    inputText.value = 'queued draft'

    const queued = queue.enqueuePendingInput(inputText.value)
    inputText.value = 'newer draft typed during persistence'

    releaseFirstPut?.()
    await expect(queued).resolves.toBe(true)
    expect(inputText.value).toBe('newer draft typed during persistence')
    expect(queue.pendingQueue.value[0]?.text).toBe('queued draft')
    queue.cleanup()
  })

  it('hydrates after the route supplies a session key post-setup', async () => {
    const delayedSessionKey = ref('')
    const { wal } = memoryWal([{
      schemaVersion: 1,
      pendingInputId: 'pending-late-session',
      sessionKey: 'agent:main:webchat:late-session',
      clientRequestId: 'request-late-session',
      clientMessageId: 'message-late-session',
      text: 'restore after route resolution',
      attachments: [],
      intent: null,
      state: 'local_only',
      createdAt: 1,
      updatedAt: 1,
    }])
    const { queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        sessionKey: delayedSessionKey,
        pendingInputWal: wal,
        supportsMethod: () => false,
      },
    )
    expect(queue.pendingQueue.value).toEqual([])

    delayedSessionKey.value = 'agent:main:webchat:late-session'
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value).toMatchObject([{
        pendingInputId: 'pending-late-session',
        text: 'restore after route resolution',
      }])
    })
    queue.cleanup()
  })

  it('keeps a legacy local-only cancellation until server ownership can be disproved', async () => {
    const { wal, records } = memoryWal([{
      schemaVersion: 1,
      pendingInputId: 'pending-before-provenance',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'request-before-provenance',
      clientMessageId: 'message-before-provenance',
      text: 'legacy ambiguous draft',
      attachments: [],
      intent: null,
      state: 'local_only',
      createdAt: 1,
      updatedAt: 2,
    }])
    const { queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]).toMatchObject({
        pendingInputId: 'pending-before-provenance',
        pendingPersistenceState: 'local_only',
        pendingMayHaveServerCopy: true,
      })
    })

    expect(queue.removePendingChip(pendingUiId(queue, 0))).toBe(true)
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('cancelling')
      expect(records.get('pending-before-provenance')).toMatchObject({
        state: 'cancelling',
        mayHaveServerCopy: true,
      })
    })
    queue.cleanup()
  })

  it('keeps the draft when the browser WAL write fails', async () => {
    const { wal } = memoryWal()
    vi.mocked(wal.put).mockRejectedValueOnce(new Error('quota exceeded'))
    const onPendingPersistenceError = vi.fn()
    const { inputText, pendingAttachments, pendingSessionIntent, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, onPendingPersistenceError },
    )
    inputText.value = 'keep this exact draft'
    const attachment: Attachment = {
      kind: 'staged',
      local_id: 91,
      name: 'keep-me.txt',
      mime: 'text/plain',
      file_uuid: 'upload-keep-me',
    }
    pendingAttachments.value = [attachment]
    pendingSessionIntent.value = 'project:keep-intent'

    await expect(queue.enqueuePendingInput(inputText.value)).resolves.toBe(false)
    expect(inputText.value).toBe('keep this exact draft')
    expect(pendingAttachments.value).toEqual([attachment])
    expect(pendingSessionIntent.value).toBe('project:keep-intent')
    expect(queue.pendingQueue.value).toEqual([])
    expect(onPendingPersistenceError).toHaveBeenCalledWith('wal_failed')
    queue.cleanup()
  })

  it('fails closed with the complete composer payload when IndexedDB is unavailable', () => {
    const onPendingPersistenceError = vi.fn()
    const { inputText, pendingAttachments, pendingSessionIntent, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: null,
        onPendingPersistenceError,
      },
    )
    const attachment: Attachment = {
      kind: 'staged',
      local_id: 92,
      name: 'retain.pdf',
      mime: 'application/pdf',
      file_uuid: 'retain-upload',
    }
    inputText.value = 'retain text, attachment and intent'
    pendingAttachments.value = [attachment]
    pendingSessionIntent.value = 'project:retain-intent'

    expect(queue.enqueuePendingInput(inputText.value)).toBe(false)
    expect(inputText.value).toBe('retain text, attachment and intent')
    expect(pendingAttachments.value).toEqual([attachment])
    expect(pendingSessionIntent.value).toBe('project:retain-intent')
    expect(queue.pendingQueue.value).toEqual([])
    expect(onPendingPersistenceError).toHaveBeenCalledWith('wal_failed')
    queue.cleanup()
  })

  it('writes attachment WAL before clearing and sends only durable upload tokens', async () => {
    const { wal, records } = memoryWal()
    const enqueueCalls: Record<string, unknown>[] = []
    const rpcCall = vi.fn(
      async (method: string, params: Record<string, unknown> = {}): Promise<unknown> => {
        if (method === 'sessions.pending_inputs.list') return { items: [] }
        if (method === 'sessions.pending_inputs.enqueue') {
          enqueueCalls.push(structuredClone(params))
          return { requestFingerprint: 'sha256:durable-attachment', revision: 1 }
        }
        throw new Error(`unexpected method: ${method}`)
      },
    )
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const { inputText, pendingAttachments, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )
    inputText.value = 'queue with attachment'
    pendingAttachments.value = [{
      kind: 'staged',
      local_id: 9,
      name: 'queued.txt',
      mime: 'text/plain',
      size: 12,
      file_uuid: 'upload-token',
    }]

    await expect(queue.enqueuePendingInput(inputText.value)).resolves.toBe(true)
    expect(inputText.value).toBe('')
    expect(pendingAttachments.value).toEqual([])
    await vi.waitFor(() => expect(enqueueCalls).toHaveLength(1))
    expect(enqueueCalls[0]).toMatchObject({
      message: 'queue with attachment',
      displayText: 'queue with attachment',
      attachments: [{
        type: 'text/plain',
        mime: 'text/plain',
        name: 'queued.txt',
        file_uuid: 'upload-token',
      }],
    })
    expect(JSON.stringify(enqueueCalls[0])).not.toContain('local_id')
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('staged')
    })
    const record = [...records.values()][0]
    expect(record?.attachments).toMatchObject([{
      local_id: 9,
      name: 'queued.txt',
      durable_material: true,
    }])
    expect(JSON.stringify(record)).not.toContain('upload-token')
    queue.cleanup()
  })

  it('reuploads once when a Gateway restart loses the expiring upload', async () => {
    const { wal } = memoryWal()
    const enqueueCalls: Record<string, unknown>[] = []
    const rpcCall = vi.fn(
      async (method: string, params: Record<string, unknown> = {}): Promise<unknown> => {
        if (method === 'sessions.pending_inputs.list') return { items: [] }
        if (method === 'sessions.pending_inputs.enqueue') {
          enqueueCalls.push(structuredClone(params))
          if (enqueueCalls.length === 1) {
            throw Object.assign(new Error('restart lost upload'), {
              code: 'ATTACHMENT_LOST_IN_RESTART',
              accepted: false,
            })
          }
          return { requestFingerprint: 'sha256:restaged', revision: 1 }
        }
        throw new Error(`unexpected method: ${method}`)
      },
    )
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const prepareAttachmentsForSend = vi.fn(async ({ attachments }: {
      attachments: Attachment[]
    }) => {
      const staged = attachments[0]
      if (staged?.expires_at === 0) staged.file_uuid = 'upload-token-after-restart'
      return true
    })
    const { inputText, pendingAttachments, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
        prepareAttachmentsForSend,
      },
    )
    inputText.value = 'recover attachment'
    pendingAttachments.value = [{
      kind: 'staged',
      local_id: 10,
      name: 'restart.txt',
      mime: 'text/plain',
      file_uuid: 'upload-token-before-restart',
      file: new File(['restart'], 'restart.txt', { type: 'text/plain' }),
      expires_at: Date.now() + 60_000,
    }]

    await expect(queue.enqueuePendingInput(inputText.value)).resolves.toBe(true)
    await vi.waitFor(() => expect(enqueueCalls).toHaveLength(2))
    expect(enqueueCalls[0]?.attachments).toMatchObject([
      { file_uuid: 'upload-token-before-restart' },
    ])
    expect(enqueueCalls[1]?.attachments).toMatchObject([
      { file_uuid: 'upload-token-after-restart' },
    ])
    expect(enqueueCalls[1]?.pendingInputId).toBe(enqueueCalls[0]?.pendingInputId)
    expect(enqueueCalls[1]?.clientRequestId).toBe(enqueueCalls[0]?.clientRequestId)
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('staged')
    })
    queue.cleanup()
  })

  it('retries an unknown enqueue result with the same durable identities', async () => {
    const { wal } = memoryWal()
    const enqueueCalls: Record<string, unknown>[] = []
    const rpcCall = vi.fn(
      async (method: string, params: Record<string, unknown> = {}): Promise<unknown> => {
        if (method === 'sessions.pending_inputs.list') return { items: [] }
        if (method === 'sessions.pending_inputs.enqueue') {
          enqueueCalls.push(structuredClone(params))
          throw new Error('response lost')
        }
        throw new Error(`unexpected method: ${method}`)
      },
    )
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const { inputText, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )
    inputText.value = 'stage exactly once'

    await expect(queue.enqueuePendingInput(inputText.value)).resolves.toBe(true)
    await vi.waitFor(() => expect(enqueueCalls).toHaveLength(1))
    expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('saving')

    await queue.hydratePendingQueue()
    await vi.waitFor(() => expect(enqueueCalls).toHaveLength(2))
    expect(enqueueCalls[1]).toEqual(enqueueCalls[0])
    queue.cleanup()
  })

  it('hydrates server-owned attachment metadata without exposing upload tokens', async () => {
    const { wal } = memoryWal()
    const rpcCall = vi.fn(
      async (
        method: string,
        params?: Record<string, unknown>,
      ): Promise<unknown> => {
        void params
        if (method === 'sessions.pending_inputs.list') {
          return {
            items: [{
              pendingInputId: 'pending-server-material',
              clientRequestId: 'request-server-material',
              clientMessageId: 'message-server-material',
              requestFingerprint: 'sha256:server-material',
              revision: 1,
              message: 'restored from server',
              attachments: [{
                name: 'restored.txt',
                mime: 'text/plain',
                type: 'text/plain',
                size: 42,
              }],
            }],
          }
        }
        throw new Error(`unexpected method: ${method}`)
      },
    )
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const { queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )

    await vi.waitFor(() => expect(queue.pendingQueue.value).toHaveLength(1))
    expect(queue.pendingQueue.value[0]).toMatchObject({
      pendingInputId: 'pending-server-material',
      pendingPersistenceState: 'staged',
      attachments: [{
        kind: 'staged',
        name: 'restored.txt',
        mime: 'text/plain',
        size: 42,
        durable_material: true,
      }],
    })
    expect(rpcCall).toHaveBeenCalledTimes(1)
    expect(queue.editPendingItem(pendingUiId(queue, 0))).toBe(false)
    expect(queue.popPendingTail()).toBe(false)
    expect(queue.popAllPendingIntoComposer()).toBe(false)
    expect(queue.pendingQueue.value).toHaveLength(1)
    queue.cleanup()
  })

  it('restores a server-staged literal slash escape from its display text', async () => {
    const { wal, records } = memoryWal()
    const rpcCall = vi.fn(async (method: string): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') {
        return {
          items: [{
            pendingInputId: 'pending-literal-slash',
            clientRequestId: 'request-literal-slash',
            clientMessageId: 'message-literal-slash',
            requestFingerprint: 'sha256:literal-slash',
            revision: 1,
            message: '/coding',
            displayText: '//coding',
            attachments: [],
          }],
        }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => {
        void params
        return rpcCall(method) as Promise<T>
      },
    }
    const { queue } = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      rpc,
      supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
    })

    await vi.waitFor(() => expect(queue.pendingQueue.value).toHaveLength(1))
    expect(queue.pendingQueue.value[0]).toMatchObject({
      text: '//coding',
      pendingPersistenceState: 'staged',
    })
    expect(records.get('pending-literal-slash')?.text).toBe('//coding')
    queue.cleanup()
  })

  it('restores the confirmed plain-text marker for a server-staged unknown slash', async () => {
    const { wal, records } = memoryWal()
    const rpcCall = vi.fn(async (method: string): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') {
        return {
          items: [{
            pendingInputId: 'pending-unknown-slash',
            clientRequestId: 'request-unknown-slash',
            clientMessageId: 'message-unknown-slash',
            requestFingerprint: 'sha256:unknown-slash',
            revision: 1,
            message: '/gamemode creative',
            confirmedPlainText: true,
            attachments: [],
          }],
        }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => {
        void params
        return rpcCall(method) as Promise<T>
      },
    }
    const { queue } = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      rpc,
      supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
    })

    await vi.waitFor(() => expect(queue.pendingQueue.value).toHaveLength(1))
    expect(queue.pendingQueue.value[0]).toMatchObject({
      text: '/gamemode creative',
      confirmedPlainText: true,
      pendingPersistenceState: 'staged',
    })
    expect(records.get('pending-unknown-slash')?.confirmedPlainText).toBe(true)
    queue.cleanup()
  })

  it('strips an ACK-lost upload token when server reconciliation proves ownership', async () => {
    const { wal, records } = memoryWal([{
      schemaVersion: 1,
      pendingInputId: 'pending-ack-lost-material',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'request-ack-lost-material',
      clientMessageId: 'message-ack-lost-material',
      text: 'reconcile attachment ownership',
      attachments: [{
        kind: 'staged',
        local_id: 8,
        name: 'ack-lost.txt',
        mime: 'text/plain',
        file_uuid: 'ephemeral-upload-token',
      }],
      intent: null,
      state: 'saving',
      createdAt: 1,
      updatedAt: 1,
    }])
    const rpcCall = vi.fn(async (
      method: string,
      params?: Record<string, unknown>,
    ): Promise<unknown> => {
      void params
      if (method !== 'sessions.pending_inputs.list') {
        throw new Error(`unexpected method: ${method}`)
      }
      return {
        items: [{
          pendingInputId: 'pending-ack-lost-material',
          clientRequestId: 'request-ack-lost-material',
          clientMessageId: 'message-ack-lost-material',
          requestFingerprint: 'sha256:ack-lost-material',
          revision: 1,
          message: 'reconcile attachment ownership',
          attachments: [{
            name: 'ack-lost.txt',
            mime: 'text/plain',
            size: 9,
          }],
        }],
      }
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const { queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )

    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('staged')
    })
    const record = records.get('pending-ack-lost-material')
    expect(record?.attachments).toMatchObject([{
      name: 'ack-lost.txt',
      durable_material: true,
    }])
    expect(JSON.stringify(record)).not.toContain('ephemeral-upload-token')
    queue.cleanup()
  })

  it('cancels an ACK-lost server row before deleting its saving WAL entry', async () => {
    const { wal } = memoryWal()
    const cancelCalls: Record<string, unknown>[] = []
    const rpcCall = vi.fn(
      async (method: string, params: Record<string, unknown> = {}): Promise<unknown> => {
        if (method === 'sessions.pending_inputs.list') return { items: [] }
        if (method === 'sessions.pending_inputs.enqueue') throw new Error('response lost')
        if (method === 'sessions.pending_inputs.cancel') {
          cancelCalls.push(structuredClone(params))
          return { cancelled: true }
        }
        throw new Error(`unexpected method: ${method}`)
      },
    )
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const { inputText, queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )
    inputText.value = 'cancel after lost acknowledgement'

    await expect(queue.enqueuePendingInput(inputText.value)).resolves.toBe(true)
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('saving')
    })
    const pendingInputId = queue.pendingQueue.value[0]?.pendingInputId

    expect(queue.removePendingChip(pendingUiId(queue, 0))).toBe(true)
    await vi.waitFor(() => expect(queue.pendingQueue.value).toHaveLength(0))
    expect(cancelCalls).toEqual([expect.objectContaining({ pendingInputId })])
    expect(cancelCalls[0]).not.toHaveProperty('expectedRevision')
    expect(vi.mocked(wal.delete)).toHaveBeenCalledWith(pendingInputId)
    queue.cleanup()
  })

  it('keeps an ACK-unknown cancellation durable across an older Gateway', async () => {
    const { wal, records } = memoryWal()
    const serverRows = new Map<string, Record<string, unknown>>()
    const initialRpcCall = vi.fn(async (
      method: string,
      params: Record<string, unknown> = {},
    ): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') return { items: [] }
      if (method === 'sessions.pending_inputs.enqueue') {
        const pendingInputId = String(params.pendingInputId || '')
        serverRows.set(pendingInputId, {
          ...structuredClone(params),
          requestFingerprint: 'sha256:mixed-version-ack-lost',
          revision: 1,
        })
        throw new Error('enqueue committed but acknowledgement was lost')
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const initialRpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        initialRpcCall(method, params) as Promise<T>
      ),
    }
    const initial = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc: initialRpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )
    initial.inputText.value = 'cancel across a Gateway downgrade'

    await expect(initial.queue.enqueuePendingInput(initial.inputText.value)).resolves.toBe(true)
    await vi.waitFor(() => expect(serverRows.size).toBe(1))
    await vi.waitFor(() => {
      expect(initial.queue.pendingQueue.value[0]).toMatchObject({
        pendingPersistenceState: 'saving',
        pendingMayHaveServerCopy: true,
      })
    })
    const pendingInputId = initial.queue.pendingQueue.value[0]?.pendingInputId
    expect(records.get(pendingInputId!)?.mayHaveServerCopy).toBe(true)
    initial.queue.cleanup()

    const legacy = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    await vi.waitFor(() => {
      expect(legacy.queue.pendingQueue.value[0]).toMatchObject({
        pendingInputId,
        pendingPersistenceState: 'local_only',
        pendingMayHaveServerCopy: true,
      })
    })

    expect(legacy.queue.removePendingChip(pendingUiId(legacy.queue, 0))).toBe(true)
    await vi.waitFor(() => {
      expect(legacy.queue.pendingQueue.value[0]).toMatchObject({
        pendingInputId,
        pendingPersistenceState: 'cancelling',
        pendingMayHaveServerCopy: true,
      })
      expect(records.get(pendingInputId!)?.state).toBe('cancelling')
      expect(records.get(pendingInputId!)?.mayHaveServerCopy).toBe(true)
    })
    legacy.queue.cleanup()

    const cancelCalls: Record<string, unknown>[] = []
    const restoredRpcCall = vi.fn(async (
      method: string,
      params: Record<string, unknown> = {},
    ): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') {
        return { items: [...serverRows.values()].map(row => structuredClone(row)) }
      }
      if (method === 'sessions.pending_inputs.cancel') {
        cancelCalls.push(structuredClone(params))
        serverRows.delete(String(params.pendingInputId || ''))
        return { cancelled: true }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const restoredRpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        restoredRpcCall(method, params) as Promise<T>
      ),
    }
    const restored = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc: restoredRpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )

    await vi.waitFor(() => {
      expect(restored.queue.pendingQueue.value).toEqual([])
      expect(records.size).toBe(0)
      expect(serverRows.size).toBe(0)
      expect(cancelCalls.length).toBeGreaterThan(0)
    })
    expect(cancelCalls.every(call => call.pendingInputId === pendingInputId)).toBe(true)
    expect(restoredRpcCall.mock.calls.map(call => call[0])).not.toContain(
      'sessions.pending_inputs.enqueue',
    )
    restored.queue.cleanup()
  })

  it('propagates a cancellation tombstone without another tab resurrecting the draft', async () => {
    vi.stubGlobal(
      'BroadcastChannel',
      TestBroadcastChannel as unknown as typeof BroadcastChannel,
    )
    const { wal, records } = memoryWal()
    const first = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    const second = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      { pendingInputWal: wal, supportsMethod: () => false },
    )
    try {
      first.inputText.value = 'cancel this in every tab'
      await expect(first.queue.enqueuePendingInput(first.inputText.value)).resolves.toBe(true)
      await vi.waitFor(() => {
        expect(first.queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('local_only')
      })
      await second.queue.hydratePendingQueue()
      expect(second.queue.pendingQueue.value).toMatchObject([{
        text: 'cancel this in every tab',
      }])

      expect(first.queue.removePendingChip(pendingUiId(first.queue, 0))).toBe(true)
      await vi.waitFor(() => {
        expect(first.queue.pendingQueue.value).toEqual([])
        expect(second.queue.pendingQueue.value).toEqual([])
        expect(records.size).toBe(0)
      })

      await second.queue.hydratePendingQueue()
      expect(second.queue.pendingQueue.value).toEqual([])
    } finally {
      first.queue.cleanup()
      second.queue.cleanup()
      vi.unstubAllGlobals()
      TestBroadcastChannel.channels.clear()
    }
  })

  it('resolves a queued action by stable UI identity after a peer deletion shifts indexes', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'peer removes this first row'
    await queue.enqueuePendingInput(inputText.value)
    inputText.value = 'edit this surviving row'
    await queue.enqueuePendingInput(inputText.value)
    const removedId = pendingUiId(queue, 0)
    const survivingId = pendingUiId(queue, 1)

    expect(queue.removePendingChip(removedId)).toBe(true)
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value.map(item => item.pendingUiId)).toEqual([survivingId])
    })

    expect(queue.editPendingItem(survivingId)).toBe(true)
    await vi.waitFor(() => {
      expect(queue.pendingQueue.value).toEqual([])
      expect(inputText.value).toBe('edit this surviving row')
    })
    queue.cleanup()
  })

  it('hydrates a cancelling WAL as cancel-only without restoring the server row', async () => {
    const { wal } = memoryWal([{
      schemaVersion: 1,
      pendingInputId: 'pending-cancel-only',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'request-cancel-only',
      clientMessageId: 'message-cancel-only',
      text: 'local delete intent',
      attachments: [{
        kind: 'staged',
        local_id: 71,
        name: 'local-delete.txt',
        mime: 'text/plain',
        durable_material: true,
      }],
      intent: 'retain-cancel-intent',
      state: 'cancelling',
      createdAt: 1,
      updatedAt: 2,
    }])
    let releaseCancel: (() => void) | undefined
    const cancelGate = new Promise<void>(resolve => { releaseCancel = resolve })
    const rpcCall = vi.fn(async (method: string): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') {
        return {
          items: [{
            pendingInputId: 'pending-cancel-only',
            clientRequestId: 'request-cancel-only',
            clientMessageId: 'message-cancel-only',
            requestFingerprint: 'sha256:stale-server-row',
            revision: 9,
            message: 'stale server payload',
            attachments: [{
              name: 'must-not-overwrite.txt',
              mime: 'text/plain',
            }],
          }],
        }
      }
      if (method === 'sessions.pending_inputs.cancel') {
        await cancelGate
        return { cancelled: true }
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => {
        void params
        return rpcCall(method) as Promise<T>
      },
    }
    const { queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )

    await vi.waitFor(() => {
      expect(queue.pendingQueue.value[0]).toMatchObject({
        pendingInputId: 'pending-cancel-only',
        pendingPersistenceState: 'cancelling',
        text: 'local delete intent',
        intent: 'retain-cancel-intent',
        attachments: [{ name: 'local-delete.txt' }],
      })
    })
    expect(rpcCall.mock.calls.map(call => call[0])).not.toContain(
      'sessions.pending_inputs.enqueue',
    )

    releaseCancel?.()
    await vi.waitFor(() => expect(queue.pendingQueue.value).toEqual([]))
    expect(rpcCall.mock.calls.map(call => call[0]).every(method => (
      method === 'sessions.pending_inputs.list'
      || method === 'sessions.pending_inputs.cancel'
    ))).toBe(true)
    queue.cleanup()
  })

  it('retains a reclassified draft when the cancel acknowledgement is lost', async () => {
    const { wal, records } = memoryWal([{
      schemaVersion: 1,
      pendingInputId: 'pending-retain-after-cancel',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'request-retain-after-cancel',
      clientMessageId: 'message-retain-after-cancel',
      text: '/gamemode creative',
      attachments: [],
      intent: null,
      confirmedPlainText: true,
      state: 'staged',
      mayHaveServerCopy: true,
      requestFingerprint: 'sha256:retain-after-cancel',
      serverRevision: 3,
      createdAt: 1,
      updatedAt: 2,
    }])
    let serverRowExists = true
    let cancelCalls = 0
    const rpcCall = vi.fn(async (method: string): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') {
        return {
          items: serverRowExists
            ? [{
                pendingInputId: 'pending-retain-after-cancel',
                clientRequestId: 'request-retain-after-cancel',
                clientMessageId: 'message-retain-after-cancel',
                requestFingerprint: 'sha256:retain-after-cancel',
                revision: 3,
                message: '/gamemode creative',
                confirmedPlainText: true,
                attachments: [],
              }]
            : [],
        }
      }
      if (method === 'sessions.pending_inputs.cancel') {
        cancelCalls += 1
        if (cancelCalls === 1) {
          serverRowExists = false
          throw new Error('cancel committed but acknowledgement was lost')
        }
        return { cancelled: true, alreadyMissing: true }
      }
      if (method === 'sessions.pending_inputs.enqueue') {
        throw new Error('retained drafts must never be re-staged')
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => {
        void params
        return rpcCall(method) as Promise<T>
      },
    }
    const initial = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )
    await vi.waitFor(() => {
      expect(initial.queue.pendingQueue.value[0]?.pendingPersistenceState).toBe('staged')
    })
    const item = initial.queue.pendingQueue.value[0]!

    await expect(initial.queue.cancelDurableItem(
      item,
      { retainAfterCancel: true },
    )).resolves.toBe(false)
    expect(records.get('pending-retain-after-cancel')).toMatchObject({
      state: 'cancelling',
      retainAfterCancel: true,
    })
    item.deliveryState = 'retryable'

    await initial.queue.hydratePendingQueue()
    await vi.waitFor(() => {
      expect(initial.queue.pendingQueue.value[0]).toMatchObject({
        text: '/gamemode creative',
        pendingPersistenceState: 'local_only',
        pendingMayHaveServerCopy: false,
        pendingRetainAfterCancel: true,
      })
      expect(initial.queue.pendingQueue.value[0]?.deliveryState).toBeUndefined()
    })
    expect(records.get('pending-retain-after-cancel')).toMatchObject({
      state: 'local_only',
      mayHaveServerCopy: false,
      retainAfterCancel: true,
    })
    expect(cancelCalls).toBe(2)
    initial.queue.cleanup()

    const restored = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )
    await vi.waitFor(() => {
      expect(restored.queue.pendingQueue.value[0]).toMatchObject({
        text: '/gamemode creative',
        pendingPersistenceState: 'local_only',
      })
    })
    expect(rpcCall.mock.calls.map(call => call[0])).not.toContain(
      'sessions.pending_inputs.enqueue',
    )

    expect(restored.queue.editPendingItem(pendingUiId(restored.queue, 0))).toBe(true)
    await vi.waitFor(() => {
      expect(restored.queue.pendingQueue.value).toEqual([])
      expect(restored.inputText.value).toBe('/gamemode creative')
      expect(records.has('pending-retain-after-cancel')).toBe(false)
    })
    restored.queue.cleanup()
  })

  it.each([
    'PENDING_INPUT_CANCELLED',
    'PENDING_INPUT_ALREADY_DISPATCHED',
  ])('accepts durable server outcome %s when a saving tab missed the peer broadcast', async (code) => {
    const { wal, records } = memoryWal([{
      schemaVersion: 1,
      pendingInputId: 'pending-cancelled-on-server',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: 'request-cancelled-on-server',
      clientMessageId: 'message-cancelled-on-server',
      text: 'must not be resurrected',
      attachments: [],
      intent: null,
      state: 'saving',
      createdAt: 1,
      updatedAt: 2,
    }])
    const rpcCall = vi.fn(async (method: string): Promise<unknown> => {
      if (method === 'sessions.pending_inputs.list') return { items: [] }
      if (method === 'sessions.pending_inputs.enqueue') {
        throw Object.assign(new Error('terminal pending outcome'), {
          code,
          accepted: false,
        })
      }
      throw new Error(`unexpected method: ${method}`)
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => {
        void params
        return rpcCall(method) as Promise<T>
      },
    }
    const { queue } = makeQueue(
      undefined,
      () => false,
      undefined,
      undefined,
      {
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
      },
    )

    await vi.waitFor(() => {
      expect(queue.pendingQueue.value).toEqual([])
      expect(records.size).toBe(0)
    })
    expect(rpcCall.mock.calls.map(call => call[0])).toContain(
      'sessions.pending_inputs.enqueue',
    )
    queue.cleanup()
  })

  it('restores a durable draft without overwriting the active composer', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'newer operator draft'

    await expect(
      queue.enqueueRecoveredInput('/meta meta-paper-write -- recovered'),
    ).resolves.toBe(true)
    expect(inputText.value).toBe('newer operator draft')
    expect(queue.pendingQueue.value).toMatchObject([{
      text: '/meta meta-paper-write -- recovered',
      attachments: [],
      intent: null,
    }])
    queue.cleanup()
  })

  it('deduplicates a hidden control by durable session/request identity', () => {
    const { queue } = makeQueue()
    const item = {
      text: 'provider confirmation',
      displayText: 'Confirmed',
      clientRequestId: 'stable-hidden-request',
      sessionKey: 'agent:main:webchat:test',
    }

    expect(queue.enqueueHiddenControl(item)).toBe(true)
    expect(queue.enqueueHiddenControl(item)).toBe(true)
    expect(queue.pendingQueue.value).toHaveLength(1)
    queue.cleanup()
  })

  it('fails closed when a hidden-control cancellation cannot be persisted', () => {
    let canPersistCancellation = false
    const onResult = vi.fn(() => canPersistCancellation)
    const { queue } = makeQueue(undefined, () => false, undefined, onResult)
    queue.enqueueHiddenControl({
      text: 'provider confirmation',
      displayText: 'Confirmed',
      clientRequestId: 'must-remain-sendable',
      sessionKey: 'agent:main:webchat:test',
    })

    queue.clearPendingQueue()
    expect(queue.pendingQueue.value).toHaveLength(1)
    canPersistCancellation = true
    expect(queue.removePendingChip(pendingUiId(queue, 0))).toBe(true)
    expect(queue.pendingQueue.value).toEqual([])
    queue.cleanup()
  })

  it('leases one item for steer and consumes it only after confirmed acceptance', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'send this now'
    await queue.enqueuePendingInput(inputText.value)
    inputText.value = 'must wait'
    await queue.enqueuePendingInput(inputText.value)

    const item = queue.beginPendingDelivery(pendingUiId(queue, 0))
    expect(item?.deliveryState).toBe('steering')
    expect(queue.beginPendingDelivery(pendingUiId(queue, 0))).toBeNull()
    expect(queue.beginPendingDelivery(pendingUiId(queue, 1))).toBeNull()

    queue.settlePendingDelivery(item!, 'retryable_failure')
    expect(queue.pendingQueue.value[0]?.deliveryState).toBe('retryable')
    expect(queue.beginPendingDelivery(pendingUiId(queue, 1))).toBeNull()

    expect(queue.beginPendingDelivery(pendingUiId(queue, 0))).toBe(item)
    queue.settlePendingDelivery(item!, 'accepted')
    expect(queue.pendingQueue.value.map(pending => pending.text)).toEqual(['must wait'])
    queue.cleanup()
  })

  it('settles an accepted steer after its queue was parked by response handoff', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'belongs to another run'
    await queue.enqueuePendingInput(inputText.value, { ownerRequestId: 'owner-a' })
    const item = queue.beginPendingDelivery(pendingUiId(queue, 0))

    await queue.adoptPendingQueue('agent:main:webchat:child', 'owner-b')
    expect(queue.pendingQueue.value).toEqual([])

    queue.settlePendingDelivery(item!, 'accepted')
    queue.switchPendingQueue('agent:main:webchat:test')
    expect(queue.pendingQueue.value).toEqual([])
    queue.cleanup()
  })

  it('never rehydrates or auto-dispatches an accepted steer after repeated session returns', async () => {
    vi.useFakeTimers()
    const sessionA = 'agent:main:webchat:A'
    const sessionB = 'agent:main:webchat:B'
    const record: PendingInputWalRecord = {
      schemaVersion: 1,
      pendingInputId: 'pending-consumed-steer',
      sessionKey: sessionB,
      clientRequestId: 'request-consumed-steer',
      clientMessageId: 'message-consumed-steer',
      text: 'consume this steer once',
      attachments: [],
      intent: null,
      state: 'staged',
      mayHaveServerCopy: true,
      requestFingerprint: 'sha256:consumed-steer',
      serverRevision: 1,
      position: 0,
      walRevision: 1,
      createdAt: 1,
      updatedAt: 1,
    }
    const { wal, records } = memoryWal([record])
    let serverRows: Array<Record<string, unknown>> = [{
      pendingInputId: record.pendingInputId,
      clientRequestId: record.clientRequestId,
      clientMessageId: record.clientMessageId,
      requestFingerprint: record.requestFingerprint,
      message: record.text,
      attachments: [],
      position: record.position,
      revision: record.serverRevision,
    }]
    const listedSessionKeys: string[] = []
    const rpcCall = vi.fn(async (
      method: string,
      params?: Record<string, unknown>,
    ): Promise<unknown> => {
      if (method !== 'sessions.pending_inputs.list') {
        throw new Error(`unexpected method: ${method}`)
      }
      const key = String(params?.key || '')
      listedSessionKeys.push(key)
      return {
        items: key === sessionB ? structuredClone(serverRows) : [],
      }
    })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: <T = unknown>(method: string, params?: Record<string, unknown>) => (
        rpcCall(method, params) as Promise<T>
      ),
    }
    const sessionKey = ref(sessionB)
    const dispatchPendingItem = vi.fn(async () => 'accepted' as const)
    const { queue } = makeQueue(
      dispatchPendingItem,
      () => false,
      undefined,
      undefined,
      {
        sessionKey,
        pendingInputWal: wal,
        rpc,
        supportsMethod: method => method.startsWith('sessions.pending_inputs.'),
        connectionState: ref('connected'),
      },
    )

    const returnToBAndSignalReady = async () => {
      const previousBLists = listedSessionKeys.filter(key => key === sessionB).length
      queue.switchPendingQueue(sessionB)
      sessionKey.value = sessionB
      await vi.waitFor(() => {
        expect(listedSessionKeys.filter(key => key === sessionB).length)
          .toBeGreaterThan(previousBLists)
      })
      await vi.waitFor(() => expect(queue.pendingQueue.value).toEqual([]))

      // This is the same drain signal ChatView emits when livePhase becomes ready.
      queue.schedulePendingDrainAfterTerminal()
      queue.flushDeferredPendingDrain()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(queue.pendingQueue.value).toEqual([])
      expect(dispatchPendingItem).not.toHaveBeenCalled()
    }

    try {
      await vi.waitFor(() => expect(queue.pendingQueue.value).toHaveLength(1))
      const steering = queue.beginPendingDelivery(pendingUiId(queue, 0))
      expect(steering).not.toBeNull()

      // The server consumes the durable row atomically with accepting the steer.
      serverRows = []
      queue.switchPendingQueue(sessionA)
      sessionKey.value = sessionA
      queue.settlePendingDelivery(steering!, 'accepted')
      await nextTick()
      await vi.waitFor(() => expect(records.size).toBe(0))

      await returnToBAndSignalReady()
      queue.switchPendingQueue(sessionA)
      sessionKey.value = sessionA
      await nextTick()
      await returnToBAndSignalReady()

      expect(dispatchPendingItem).not.toHaveBeenCalled()
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('parks an in-flight steer with its source session and exact request snapshot', () => {
    const { queue, sessionKey } = makeQueue()
    const item = queue.enqueuePendingSteerAttempt({
      request: {
        key: sessionKey.value,
        message: 'keep this source-bound steer',
        expected_turn_id: 'turn-source',
        client_request_id: 'request-source',
        client_message_id: 'client-source',
        surface_id: 'webui',
        _source: { elevated: 'enabled', runMode: 'safe' },
      },
      phase: 'submitting',
    })
    expect(item).not.toBeNull()

    queue.switchPendingQueue('agent:main:webchat:other')
    sessionKey.value = 'agent:main:webchat:other'
    expect(queue.pendingQueue.value).toEqual([])

    queue.switchPendingQueue('agent:main:webchat:test')
    sessionKey.value = 'agent:main:webchat:test'
    expect(queue.pendingQueue.value).toEqual([item])
    expect(queue.pendingQueue.value[0]?.steerAttempt).toMatchObject({
      phase: 'submitting',
      request: {
        key: 'agent:main:webchat:test',
        message: 'keep this source-bound steer',
        expected_turn_id: 'turn-source',
        client_request_id: 'request-source',
        client_message_id: 'client-source',
        _source: { elevated: 'enabled', runMode: 'safe' },
      },
    })
    queue.cleanup()
  })

  it('keeps five ordinary slots plus one independent transport-owned steer slot', async () => {
    const { queue, sessionKey } = makeQueue()
    const request = {
      key: sessionKey.value,
      message: 'transport-owned steer',
      expected_turn_id: 'turn-capacity',
      client_request_id: 'request-capacity',
      client_message_id: 'client-capacity',
      surface_id: 'webui',
    }

    expect(queue.enqueuePendingSteerAttempt({ request })).not.toBeNull()
    for (let index = 0; index < 5; index += 1) {
      await expect(queue.enqueuePendingPayload({ text: `ordinary-${index}` })).resolves.toBe(true)
    }

    expect(queue.pendingQueue.value).toHaveLength(6)
    expect(queue.canQueueMore.value).toBe(false)
    expect(queue.enqueuePendingPayload({ text: 'ordinary-overflow' })).toBe(false)
    expect(queue.enqueuePendingSteerAttempt({
      request: {
        ...request,
        client_request_id: 'request-capacity-second',
        client_message_id: 'client-capacity-second',
      },
    })).toBeNull()
    queue.cleanup()
  })

  it.each(['steering', 'retryable'] satisfies Array<
    Exclude<ChatPendingItem['deliveryState'], undefined>
  >)('defers automatic drain for any %s item and resumes after the state clears', async (state) => {
    vi.useFakeTimers()
    const { inputText, queue, sendCurrentInput } = makeQueue()
    try {
      inputText.value = 'queue head'
      await queue.enqueuePendingInput(inputText.value)
      inputText.value = 'delivery barrier'
      await queue.enqueuePendingInput(inputText.value)
      queue.pendingQueue.value[1]!.deliveryState = state

      queue.schedulePendingDrainAfterTerminal()
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)

      expect(queue.pendingQueue.value.map(item => item.text))
        .toEqual(['queue head', 'delivery barrier'])
      expect(sendCurrentInput).not.toHaveBeenCalled()

      queue.pendingQueue.value[1]!.deliveryState = undefined
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['delivery barrier'])
      expect(inputText.value).toBe('queue head')
      expect(sendCurrentInput).toHaveBeenCalledOnce()
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('auto-drains through the composer-preserving dispatcher after a steer settles', async () => {
    vi.useFakeTimers()
    const dispatchPendingItem = vi.fn(async () => 'accepted' as const)
    const { inputText, queue } = makeQueue(dispatchPendingItem)
    try {
      inputText.value = 'explicit steer'
      await queue.enqueuePendingInput(inputText.value)
      inputText.value = 'next queued item'
      await queue.enqueuePendingInput(inputText.value)
      const steering = queue.beginPendingDelivery(pendingUiId(queue, 0))
      inputText.value = 'draft written while steering'

      queue.schedulePendingDrainAfterTerminal()
      queue.settlePendingDelivery(steering!, 'accepted')
      await nextTick()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(dispatchPendingItem).toHaveBeenCalledWith(expect.objectContaining({
        text: 'next queued item',
      }), 'agent:main:webchat:test')
      expect(inputText.value).toBe('draft written while steering')
      expect(queue.pendingQueue.value).toEqual([])
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('pauses terminal drain while reordering and resumes with the new queue head', async () => {
    vi.useFakeTimers()
    const dispatchPendingItem = vi.fn(async () => 'accepted' as const)
      const { inputText, queue } = makeQueue(dispatchPendingItem)
    try {
      inputText.value = 'first queued message'
      await queue.enqueuePendingInput(inputText.value)
      inputText.value = 'second queued message'
      await queue.enqueuePendingInput(inputText.value)

      const firstPendingUiId = queue.pendingQueue.value[0]!.pendingUiId
      expect(queue.beginPendingReorder(0)).toBe(true)
      expect(queue.beginPendingDelivery(firstPendingUiId)).toBeNull()
      queue.schedulePendingDrainAfterTerminal()
      await vi.advanceTimersByTimeAsync(50)
      expect(dispatchPendingItem).not.toHaveBeenCalled()

      expect(queue.reorderPendingItem(0, 1)).toBe(true)
      expect(queue.pendingQueue.value.map(item => item.text)).toEqual([
        'second queued message',
        'first queued message',
      ])
      queue.endPendingReorder()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(dispatchPendingItem).toHaveBeenCalledWith(expect.objectContaining({
        text: 'second queued message',
      }), 'agent:main:webchat:test')
      expect(queue.pendingQueue.value.map(item => item.text)).toEqual([
        'first queued message',
      ])
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it('refuses reordering when any queued item owns delivery state', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'ordinary follow-up'
    await queue.enqueuePendingInput(inputText.value)
    inputText.value = 'retryable follow-up'
    await queue.enqueuePendingInput(inputText.value)
    queue.pendingQueue.value[1]!.deliveryState = 'retryable'

    expect(queue.beginPendingReorder(0)).toBe(false)
    expect(queue.reorderPendingItem(0, 1)).toBe(false)
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual([
      'ordinary follow-up',
      'retryable follow-up',
    ])
    queue.cleanup()
  })

  it('keeps a deferred auto-drain live until transient attachment work clears', async () => {
    vi.useFakeTimers()
    const attachmentBusy = ref(false)
    let callCount = 0
    const dispatchPendingItem = vi.fn(async () => {
      callCount += 1
      if (callCount === 1) {
        attachmentBusy.value = true
        return 'deferred' as const
      }
      return 'accepted' as const
    })
    const { inputText, queue } = makeQueue(
      dispatchPendingItem,
      () => attachmentBusy.value,
    )
    try {
      inputText.value = 'send after attachment work'
      await queue.enqueuePendingInput(inputText.value)
      queue.schedulePendingDrainAfterTerminal()

      await vi.advanceTimersByTimeAsync(50)
      await nextTick()
      expect(dispatchPendingItem).toHaveBeenCalledOnce()
      expect(queue.pendingQueue.value).toHaveLength(1)

      // The deferred signal must survive the blocked timer without spinning.
      await vi.advanceTimersByTimeAsync(50)
      expect(dispatchPendingItem).toHaveBeenCalledOnce()

      attachmentBusy.value = false
      queue.flushDeferredPendingDrain()
      await vi.advanceTimersByTimeAsync(50)
      await nextTick()

      expect(dispatchPendingItem).toHaveBeenCalledTimes(2)
      expect(queue.pendingQueue.value).toEqual([])
    } finally {
      queue.cleanup()
      vi.useRealTimers()
    }
  })

  it.each(['visible', 'hidden'] as const)(
    'never dispatches an A-session %s lease after switching to B before nextTick',
    async kind => {
      vi.useFakeTimers()
      const dispatchPendingItem = vi.fn(async () => 'accepted' as const)
      const dispatchHiddenControl = vi.fn(async () => 'accepted' as const)
      const { inputText, queue, sessionKey } = makeQueue(
        dispatchPendingItem,
        () => false,
        dispatchHiddenControl,
      )
      try {
        if (kind === 'hidden') {
          queue.enqueueHiddenControl({
            text: 'A hidden control',
            displayText: 'A control',
          })
        } else {
          inputText.value = 'A visible follow-up'
          await queue.enqueuePendingInput(inputText.value)
        }
        queue.schedulePendingDrainAfterTerminal()

        vi.advanceTimersByTime(50)
        queue.switchPendingQueue('agent:main:webchat:B')
        sessionKey.value = 'agent:main:webchat:B'
        await nextTick()

        expect(dispatchPendingItem).not.toHaveBeenCalled()
        expect(dispatchHiddenControl).not.toHaveBeenCalled()
        expect(queue.pendingQueue.value).toEqual([])
      } finally {
        queue.cleanup()
        vi.useRealTimers()
      }
    },
  )

  it('does not remove a steering item through remove or clear', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'in flight'
    await queue.enqueuePendingInput(inputText.value)
    inputText.value = 'not started'
    await queue.enqueuePendingInput(inputText.value)
    queue.pendingQueue.value[0]!.deliveryState = 'steering'

    expect(queue.removePendingChip(pendingUiId(queue, 0))).toBe(false)
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['in flight', 'not started'])

    queue.clearPendingQueue()
    await vi.waitFor(() => expect(queue.pendingQueue.value).toHaveLength(1))
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['in flight'])
    expect(queue.pendingQueue.value[0]?.deliveryState).toBe('steering')
    queue.cleanup()
  })

  it('lets an operator retry or remove a terminal hidden-control failure', () => {
    const { queue } = makeQueue()
    queue.enqueueHiddenControl({
      text: 'provider confirmation',
      displayText: 'Confirmed',
    })
    const hidden = queue.pendingQueue.value[0]!
    hidden.deliveryState = 'retryable'

    expect(queue.beginPendingDelivery(hidden.pendingUiId)).toBeNull()
    expect(queue.beginPendingDelivery(hidden.pendingUiId, true)).toBe(hidden)
    queue.settlePendingDelivery(hidden, 'retryable_failure')
    expect(hidden.deliveryState).toBe('retryable')
    expect(queue.removePendingChip(hidden.pendingUiId)).toBe(true)
    expect(queue.pendingQueue.value).toEqual([])
    queue.cleanup()
  })

  it('keeps steer-owned items out of composer recovery paths', async () => {
    const { inputText, queue } = makeQueue()
    inputText.value = 'ordinary follow-up'
    await queue.enqueuePendingInput(inputText.value)
    inputText.value = 'ambiguous steer'
    await queue.enqueuePendingInput(inputText.value)
    queue.pendingQueue.value[1]!.deliveryState = 'retryable'

    expect(queue.popPendingTail()).toBe(true)
    await vi.waitFor(() => expect(inputText.value).toBe('ordinary follow-up'))
    expect(inputText.value).toBe('ordinary follow-up')
    expect(queue.pendingQueue.value.map(item => item.text)).toEqual(['ambiguous steer'])

    inputText.value = 'existing draft'
    expect(queue.popAllPendingIntoComposer()).toBe(false)
    expect(inputText.value).toBe('existing draft')
    expect(queue.pendingQueue.value[0]?.deliveryState).toBe('retryable')
    queue.cleanup()
  })

  it('recovers owner rows after refresh by atomically accepting the handoff', async () => {
    const parent = 'agent:main:webchat:test'
    const child = 'agent:main:webchat:child'
    const ownerRequestId = 'fork-request-refresh'
    const { wal, handoffs } = memoryWal()
    await wal.putHandoff!({
      schemaVersion: 1,
      ownerRequestId,
      requestSessionKey: parent,
      clientRequestId: ownerRequestId,
      clientMessageId: 'fork-message-refresh',
      composerText: 'fork me',
      recoveryAttachments: [],
      params: {
        sessionKey: parent,
        clientRequestId: ownerRequestId,
        clientMessageId: 'fork-message-refresh',
        message: 'fork me',
        forkBeforeMessageId: 'message-before-fork',
      },
      state: 'submitting',
      createdAt: 1,
      updatedAt: 1,
    })

    const first = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      supportsMethod: () => false,
    })
    first.inputText.value = 'follow-up A'
    await first.queue.enqueuePendingInput(first.inputText.value, { ownerRequestId })
    first.inputText.value = 'follow-up B'
    await first.queue.enqueuePendingInput(first.inputText.value, { ownerRequestId })
    first.queue.cleanup()

    const reloaded = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      supportsMethod: () => false,
    })
    await reloaded.queue.hydratePendingQueue(parent)
    expect(reloaded.queue.pendingQueue.value.map(item => item.ownerRequestId)).toEqual([
      ownerRequestId,
      ownerRequestId,
    ])

    await reloaded.queue.recoverPendingQueueHandoff(parent, child, ownerRequestId)
    expect(reloaded.queue.pendingQueue.value).toEqual([])
    expect(handoffs.get(ownerRequestId)).toMatchObject({
      state: 'accepted',
      acceptedSessionKey: child,
    })

    reloaded.queue.switchPendingQueue(child)
    reloaded.sessionKey.value = child
    await nextTick()
    await reloaded.queue.hydratePendingQueue(child)
    expect(reloaded.queue.pendingQueue.value.map(item => item.text)).toEqual([
      'follow-up A',
      'follow-up B',
    ])
    expect(reloaded.queue.pendingQueue.value.map(item => ({
      ownerSessionKey: item.ownerSessionKey,
      ownerRequestId: item.ownerRequestId,
    }))).toEqual([
      { ownerSessionKey: child, ownerRequestId: undefined },
      { ownerSessionKey: child, ownerRequestId: undefined },
    ])
    reloaded.queue.cleanup()
  })

  it('persists a local-only reorder across remount with WAL revision CAS', async () => {
    const { wal } = memoryWal()
    const first = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      supportsMethod: () => false,
    })
    for (const text of ['A', 'B', 'C']) {
      first.inputText.value = text
      await first.queue.enqueuePendingInput(text)
    }
    await vi.waitFor(() => expect(first.queue.pendingQueue.value.every(item => (
      item.pendingPersistenceState === 'local_only'
    ))).toBe(true))
    expect(first.queue.beginPendingReorder(2)).toBe(true)
    expect(first.queue.reorderPendingItem(2, 0)).toBe(true)
    await first.queue.endPendingReorder()
    expect(first.queue.pendingQueue.value.map(item => item.text)).toEqual(['C', 'A', 'B'])
    first.queue.cleanup()

    const reloaded = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      supportsMethod: () => false,
    })
    await reloaded.queue.hydratePendingQueue(reloaded.sessionKey.value)
    expect(reloaded.queue.pendingQueue.value.map(item => item.text)).toEqual(['C', 'A', 'B'])
    reloaded.queue.cleanup()
  })

  it('commits a staged reorder through the batch RPC before releasing drain', async () => {
    const sessionKey = 'agent:main:webchat:test'
    const initial = ['A', 'B', 'C'].map((text, position): PendingInputWalRecord => ({
      schemaVersion: 1,
      pendingInputId: `pending-${text}`,
      sessionKey,
      clientRequestId: `request-${text}`,
      clientMessageId: `message-${text}`,
      text,
      attachments: [],
      intent: null,
      state: 'staged',
      mayHaveServerCopy: true,
      requestFingerprint: `fingerprint-${text}`,
      serverRevision: 1,
      position,
      walRevision: 1,
      createdAt: position + 1,
      updatedAt: position + 1,
    }))
    const { wal, records } = memoryWal(initial)
    let serverRows = initial.map(record => ({
      pendingInputId: record.pendingInputId,
      clientRequestId: record.clientRequestId,
      clientMessageId: record.clientMessageId,
      requestFingerprint: record.requestFingerprint,
      message: record.text,
      attachments: [],
      position: record.position,
      revision: record.serverRevision,
    }))
    const rpcCall = vi.fn(async (method: string, params?: Record<string, unknown>) => {
        if (method === 'sessions.pending_inputs.list') return { items: serverRows }
        if (method === 'sessions.pending_inputs.reorder') {
          const requested = params?.items as Array<{
            pendingInputId: string
            expectedRevision: number
          }>
          serverRows = requested.map((item, position) => ({
            ...serverRows.find(row => row.pendingInputId === item.pendingInputId)!,
            position,
            revision: item.expectedRevision + 1,
          }))
          return { items: serverRows }
        }
        return {}
      })
    const rpc: NonNullable<UseChatPendingQueueOptions['rpc']> = {
      call: rpcCall as unknown as NonNullable<UseChatPendingQueueOptions['rpc']>['call'],
    }
    const first = makeQueue(undefined, () => false, undefined, undefined, {
      pendingInputWal: wal,
      rpc,
      supportsMethod: method => [
        'sessions.pending_inputs.enqueue',
        'sessions.pending_inputs.reorder',
      ].includes(method),
      connectionState: ref('connected'),
    })
    await first.queue.hydratePendingQueue(sessionKey)
    expect(first.queue.beginPendingReorder(2)).toBe(true)
    expect(first.queue.reorderPendingItem(2, 0)).toBe(true)
    await first.queue.endPendingReorder()

    expect(rpcCall).toHaveBeenCalledWith('sessions.pending_inputs.reorder', {
      key: sessionKey,
      items: [
        { pendingInputId: 'pending-C', expectedRevision: 1 },
        { pendingInputId: 'pending-A', expectedRevision: 1 },
        { pendingInputId: 'pending-B', expectedRevision: 1 },
      ],
    })
    expect(first.queue.pendingQueue.value.map(item => item.text)).toEqual(['C', 'A', 'B'])
    expect([...records.values()]
      .sort((left, right) => (left.position ?? 0) - (right.position ?? 0))
      .map(record => record.text)).toEqual(['C', 'A', 'B'])
    first.queue.cleanup()
  })
})
