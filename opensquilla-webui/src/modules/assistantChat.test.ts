import { describe, expect, it, vi } from 'vitest'
import { assistantChatBindingKey, createAssistantChat } from './assistantChat'
import { conversationEventSessionKey, type ConversationEvent, type ConversationEvents } from './conversationEvents'
import { createConversationEventHub } from './conversationEventHub'
import { createConversationEventTransport } from '@/adapters/gateway/conversationEventTransport'
import { InMemorySessionReadPortAdapter } from '@/adapters/inMemory/sessionReadPortInMemory'
import { createSessionReadLifecycleFactory } from './sessionReadLifecycle'
import type { TransportGapHandler } from '@/adapters/gateway/transportTypes'
import type { ConversationEventSourceHandlers } from './conversationEventHub'
import { SessionReadSessionMissingError, type SessionReadLease, type SessionReadLive, type SessionReadHistoryPage, type SessionReadMetadata } from './sessionReadLifecycle'
import { TurnCommandError, type TurnCommands } from './turnCommands'

type Mutable<T> = { -readonly [K in keyof T]: T[K] }

let sequence = 0
function fixture(sharedEvents?: ConversationEvents) {
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) }, removeItem: (key: string) => { values.delete(key) } }
  let handlers: ConversationEventSourceHandlers<ConversationEvent> | undefined
  let current = true
  const live = { sessionKey: 'child', activity: 'idle', snapshot: null, initialMetadata: { epoch: 1 }, confirmInstalled: vi.fn(), assertInstalledCurrent: vi.fn() } as unknown as Mutable<SessionReadLive>
  const history = { messages: [], hasMore: false } as unknown as Mutable<SessionReadHistoryPage>
  const metadata = { sessionKey: 'child', workspaceId: 'workspace', epoch: 1, pendingUserInputs: [], runStatus: 'idle', lastTask: null, activeTask: null } as unknown as Mutable<SessionReadMetadata>
  const close = vi.fn(async () => {})
  const lease = { live: Promise.resolve(live), criticalRequestsQueued: Promise.resolve(), metadata: Promise.resolve(metadata), retryMetadata: vi.fn(async () => metadata), history: { latest: vi.fn(async () => history) }, reconcile: vi.fn(async () => live), close } as unknown as Mutable<SessionReadLease>
  const create = vi.fn(async () => ({ key: 'child', sessionId: 'child-id' }))
  const send = vi.fn(async (..._args: Parameters<TurnCommands['send']>) => ({ ok: true }))
  const cancel = vi.fn(async () => ({ aborted: true }))
  const lookupReceipt = vi.fn(async () => ({ status: 'not-found' as const }))
  const readMessageText = vi.fn(async (_message: unknown, _signal: AbortSignal) => '')
  const readFixture = { sessionKey: 'child', metadata, latestHistory: history, live: {
    ...live, initialMetadata: metadata, activeTaskId: null, snapshotCursor: null,
    cursor: { sessionKey: 'child', sessionEpoch: 1, streamGeneration: 'generation', currentStreamSeq: 0, replayComplete: true, replayGapReason: null },
  }, liveError: undefined as Error | undefined }
  const adapter = new InMemorySessionReadPortAdapter([readFixture])
  const options = {
    parentKey: `parent-${++sequence}`, identity: 'identity-a', workspaceId: 'workspace',
    isCurrent: () => current, generation: () => 1,
    lifecycle: { create, fork: vi.fn(), rename: vi.fn(), remove: vi.fn() },
    reads: sharedEvents ? createSessionReadLifecycleFactory(adapter) : { create: () => ({ open: vi.fn(() => lease), current: () => lease }) },
    commands: { send, cancel, steer: vi.fn(), supports: () => true, lookupReceipt },
    events: sharedEvents || { subscribe: (value: ConversationEventSourceHandlers<ConversationEvent>) => { handlers = value; return vi.fn() } },
    readMessageText,
    storage, title: 'Assistant chat', onChange: vi.fn(),
  }
  const controller = createAssistantChat(options)
  const emit = (kind: string, text?: string, key = 'child', epoch = 1, streamGeneration = 'generation', extra: Record<string, unknown> = {}) => handlers?.onEvent?.({ kind: 'conversation', event: {
    kind: 'known', semanticKind: kind, sessionKey: key, taskId: 'task', turnId: 'turn',
    streamGeneration, streamSeq: ++sequence, connectionSeq: null, generationEpoch: null,
    meta: {}, payload: { key, epoch, assistant_message_id: 'answer', text, ...extra },
  } } as ConversationEvent)
  return { controller, options, values, create, send, cancel, lookupReceipt, readMessageText, live, history, metadata, lease, close, emit, readFixture, adapter, expire: () => { current = false } }
}

describe('assistant side conversation lifecycle', () => {
  it('leaves global recovery to the main read until a child exists, and after closing', async () => {
    let gap!: TransportGapHandler
    const transport = createConversationEventTransport({
      subscribe: () => ({ close() {} }),
      subscribeGap: handler => { gap = handler; return { close() {} } },
    })
    const main = createConversationEventHub(transport, { sessionKey: conversationEventSessionKey })
    main.prepareReadRetirement('main')
    const recoverMain = vi.fn(async () => true)
    main.observeRecoveryRequired(recoverMain)
    const f = fixture(transport)
    await f.controller.restore()
    await expect(gap({ global: true })).resolves.toBe(true)
    expect(f.create).not.toHaveBeenCalled()
    await f.controller.send('question')
    f.readFixture.liveError = new Error('child reconciliation failed')
    await expect(gap({ global: true })).resolves.toBe(false)
    expect(f.controller.state.error).toBe('child reconciliation failed')
    f.readFixture.liveError = undefined
    await expect(gap({ global: true })).resolves.toBe(true)
    await f.controller.close()
    expect(f.adapter.activeLeaseCount).toBe(0)
    await expect(gap({ global: true })).resolves.toBe(true)
    expect(recoverMain).toHaveBeenCalledTimes(4)
    main.dispose()
  })

  it('reconciles streamed full text against the current canonical body instead of installing its preview', async () => {
    const f = fixture()
    await f.controller.send('question')
    const sharedPrefix = 'a'.repeat(16 * 1024)
    f.emit('text-delta', sharedPrefix + 'old streamed suffix')
    f.emit('turn-completed')
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: sharedPrefix,
      previewComplete: false, contentRevision: 'r1', contentRef: {
        version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer',
        view: 'display', revision: 'r1', byteLength: 20000,
      } }] as never
    f.readMessageText.mockResolvedValue(sharedPrefix + 'canonical suffix')
    f.emit('turn-committed')
    await f.controller.refresh()
    expect(f.readMessageText).toHaveBeenCalledOnce()
    expect(f.controller.state.messages[0]).toMatchObject({ text: sharedPrefix + 'canonical suffix', previewComplete: true, contentRevision: 'r1' })
    await f.controller.refresh()
    expect(f.readMessageText).toHaveBeenCalledOnce()
    f.history.messages = f.history.messages.map(row => ({ ...row, contentRevision: 'r2', contentRef: { ...row.contentRef!, revision: 'r2' } }))
    f.readMessageText.mockResolvedValue(sharedPrefix + 'revised canonical suffix')
    await f.controller.refresh()
    expect(f.readMessageText).toHaveBeenCalledTimes(2)
    expect(f.controller.state.messages[0]?.text).toBe(sharedPrefix + 'revised canonical suffix')
    await f.controller.close()
  })

  it('retains preview identity on reopen without automatically fetching historical bodies', async () => {
    const f = fixture()
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    const contentRef = { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r1', byteLength: 20000 }
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'bounded preview', previewComplete: false,
      contentRef, contentRevision: 'r1', contentAvailability: 'ready' }] as never
    await f.controller.restore()
    expect(f.controller.state.messages[0]).toMatchObject({ previewComplete: false, contentRef, contentRevision: 'r1', contentAvailability: 'ready' })
    expect(f.readMessageText).not.toHaveBeenCalled()
    await f.controller.close()
  })

  it.each(['missing-reference', 'preparing', 'oversized', 'failed-read'])('keeps an explicit canonical preview without an old suffix when hydration is %s', async reason => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'shared prefix plus stale streamed suffix')
    const contentRef = { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r2', byteLength: reason === 'oversized' ? 1024 * 1024 : 20000 }
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'shared prefix', previewComplete: false,
      contentRevision: 'r2', contentRef: reason === 'missing-reference' ? undefined : contentRef,
      contentAvailability: reason === 'preparing' ? 'preparing' : 'ready' }] as never
    f.readMessageText.mockRejectedValue(new Error('content unavailable'))
    await f.controller.refresh()
    expect(f.controller.state.messages[0]).toMatchObject({ text: 'shared prefix', previewComplete: false, contentRevision: 'r2' })
    expect(f.readMessageText).toHaveBeenCalledTimes(reason === 'failed-read' ? 1 : 0)
    await f.controller.close()
  })

  it('cancels an in-flight body read on close and never installs its late result', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'full streamed body')
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'full', previewComplete: false,
      contentRef: { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r1', byteLength: 20000 } }] as never
    let finish!: (text: string) => void
    f.readMessageText.mockImplementation(async () => new Promise(resolve => { finish = resolve }))
    const refresh = f.controller.refresh()
    await vi.waitFor(() => expect(f.readMessageText).toHaveBeenCalledOnce())
    const signal = f.readMessageText.mock.calls[0]![1]
    await f.controller.close()
    expect(signal.aborted).toBe(true)
    const calls = f.options.onChange.mock.calls.length
    finish('late canonical text')
    await refresh
    expect(f.options.onChange).toHaveBeenCalledTimes(calls)
    expect(f.controller.state.messages.some(row => row.text === 'late canonical text')).toBe(false)
  })

  it('gates a new send while canonical body reconciliation is installing', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'full streamed body')
    f.emit('turn-completed')
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'full', previewComplete: false,
      contentRef: { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r1', byteLength: 20000 } }] as never
    let finish!: (text: string) => void
    f.readMessageText.mockImplementation(async () => new Promise(resolve => { finish = resolve }))
    const refresh = f.controller.refresh()
    await vi.waitFor(() => expect(f.readMessageText).toHaveBeenCalledOnce())
    expect(f.controller.state.phase).toBe('loading')
    expect(await f.controller.send('next draft')).toBe(false)
    expect(f.send).toHaveBeenCalledOnce()
    finish('canonical body')
    await refresh
    expect(await f.controller.send('next draft')).toBe(true)
    expect(f.controller.state.messages.map(row => row.text)).toEqual(['canonical body', 'next draft'])
    await f.controller.close()
  })

  it('leaves loading on a failed manual refresh and requires a successful retry before sending', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('turn-completed')
    vi.mocked(f.lease.reconcile).mockRejectedValueOnce(new Error('reconcile unavailable'))
    await expect(f.controller.refresh()).rejects.toThrow('reconcile unavailable')
    expect(f.controller.state.phase).toBe('error')
    expect(f.controller.state.error).toBe('reconcile unavailable')
    expect(await f.controller.send('next draft')).toBe(false)
    await f.controller.refresh()
    expect(f.controller.state.phase).toBe('ready')
    expect(await f.controller.send('next draft')).toBe(true)
    await f.controller.close()
  })

  it('limits automatic reconciliation to one bounded body and rejects an oversized result', async () => {
    const f = fixture()
    await f.controller.send('question')
    for (const id of ['first', 'second']) f.emit('text-delta', 'complete streamed answer', 'child', 1, 'generation', { assistant_message_id: id })
    f.history.messages = ['first', 'second'].map(id => ({ id, messageId: id, role: 'assistant', text: 'preview', previewComplete: false,
      contentRef: { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: id, view: 'display', revision: 'r1', byteLength: 20000 },
    })) as never
    f.readMessageText.mockResolvedValue('a'.repeat(512 * 1024 + 1))
    await f.controller.refresh()
    expect(f.readMessageText).toHaveBeenCalledOnce()
    expect(f.readMessageText.mock.calls[0]![0]).toMatchObject({ id: 'second' })
    expect(f.controller.state.messages.map(row => ({ text: row.text, complete: row.previewComplete }))).toEqual([
      { text: 'preview', complete: false }, { text: 'preview', complete: false },
    ])
    await f.controller.close()
  })

  it('re-reads canonical history when a newer commit arrives during body hydration', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'complete streamed answer')
    const row = { id: 'answer', messageId: 'answer', role: 'assistant', text: 'preview', previewComplete: false,
      contentRevision: 'r1', contentRef: { version: 1, sessionKey: 'child', sessionId: 'physical-child', messageId: 'answer', view: 'display', revision: 'r1', byteLength: 20000 } }
    f.history.messages = [row] as never
    let finish!: (text: string) => void
    f.readMessageText.mockImplementationOnce(async () => new Promise(resolve => { finish = resolve }))
    const refresh = f.controller.refresh()
    await vi.waitFor(() => expect(f.readMessageText).toHaveBeenCalledOnce())
    f.history.messages = [{ ...row, contentRevision: 'r2', contentRef: { ...row.contentRef, revision: 'r2' } }] as never
    f.readMessageText.mockResolvedValue('latest canonical revision')
    f.emit('turn-committed')
    finish('earlier canonical revision')
    await refresh
    await vi.waitFor(() => expect(f.controller.state.messages[0]?.text).toBe('latest canonical revision'))
    expect(f.readMessageText).toHaveBeenCalledTimes(2)
    await f.controller.close()
  })

  it('creates lazily and shares only the workspace without forking history', async () => {
    const f = fixture()
    await f.controller.restore()
    expect(f.create).not.toHaveBeenCalled()
    expect(await f.controller.send('hello')).toBe(true)
    expect(f.create).toHaveBeenCalledWith({ title: 'Assistant chat', workspaceId: 'workspace' })
    expect(f.options.lifecycle.fork).not.toHaveBeenCalled()
    expect(f.send.mock.calls[0]?.[0]).toMatchObject({ kind: 'new-turn', params: { sessionKey: 'child', message: 'hello' } })
    expect([...f.values.values()]).toEqual(['child'])
    await f.controller.close()
  })

  it('puts the user before an early streamed answer and does not revive completed turns on ACK', async () => {
    const f = fixture()
    f.send.mockImplementation(async () => {
      f.emit('text-delta', 'answer')
      f.emit('turn-completed')
      return { ok: true }
    })
    await f.controller.send('question')
    expect(f.controller.state.messages.map(row => row.role)).toEqual(['user', 'assistant'])
    expect(f.controller.state.phase).toBe('ready')
    await f.controller.close()
  })

  it('reuses the child after closing without cancelling or deleting it', async () => {
    const f = fixture()
    await f.controller.send('question')
    await f.controller.close()
    expect(f.close).toHaveBeenCalledOnce()
    expect(f.cancel).not.toHaveBeenCalled()
    expect(f.options.lifecycle.remove).not.toHaveBeenCalled()
    const reopened = createAssistantChat(f.options)
    await reopened.restore()
    expect(reopened.state.childKey).toBe('child')
    expect(f.create).toHaveBeenCalledOnce()
    await reopened.close()
  })

  it('fences another session and all late events after a parent/epoch change', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'wrong task', 'parent')
    f.expire()
    f.emit('text-delta', 'late answer')
    expect(f.controller.state.messages.map(row => row.text)).toEqual(['question'])
    expect(await f.controller.send('late send')).toBe(false)
    await f.controller.close()
  })

  it('does not send a turn after creation completes for a superseded owner', async () => {
    const f = fixture()
    f.create.mockImplementation(async () => { f.expire(); return { key: 'child', sessionId: 'child-id' } })
    expect(await f.controller.send('question')).toBe(false)
    expect(f.send).not.toHaveBeenCalled()
    expect([...f.values.values()]).toEqual(['child'])
    await f.controller.close()
  })

  it('keeps a created child when localStorage writes fail', async () => {
    const f = fixture()
    f.options.storage.setItem = () => { throw new Error('storage disabled') }
    await f.controller.send('question')
    await f.controller.close()
    const reopened = createAssistantChat(f.options)
    await reopened.restore()
    expect(reopened.state.childKey).toBe('child')
    expect(f.create).toHaveBeenCalledOnce()
    await reopened.close()
  })

  it('requires explicit replacement when a saved child has been deleted', async () => {
    const f = fixture()
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    f.lease.live = Promise.reject(new SessionReadSessionMissingError('missing child'))
    await f.controller.restore()
    expect(f.controller.state.requiresNewSession).toBe(true)
    expect(await f.controller.send('question')).toBe(false)
    expect(f.create).not.toHaveBeenCalled()
    await f.controller.close()
  })

  it('does not treat history refresh as proof of uncertain delivery', async () => {
    const f = fixture()
    f.send.mockRejectedValue(new TurnCommandError('transport', 'lost response'))
    await f.controller.send('question')
    expect(f.controller.state.uncertainDelivery).toBe(true)
    await f.controller.refresh()
    expect(f.lookupReceipt).toHaveBeenCalledOnce()
    expect(f.controller.state.uncertainDelivery).toBe(true)
    expect(await f.controller.send('question')).toBe(false)
    expect(f.send).toHaveBeenCalledOnce()
    await f.controller.close()
  })

  it('does not duplicate durable text when the active snapshot overlaps it', async () => {
    const f = fixture()
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'hello' }] as never
    f.live.snapshot = { sessionKey: 'child', taskId: 'task', events: [{ semanticKind: 'text-delta', payload: { assistant_message_id: 'answer', text: 'hello' } }] }
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    await f.controller.restore()
    expect(f.controller.state.messages.map(row => row.text)).toEqual(['hello'])
    await f.controller.close()
  })

  it('uses a distinct persisted namespace for each Gateway authority and parent', () => {
    expect(assistantChatBindingKey('A', 'parent')).not.toBe(assistantChatBindingKey('B', 'parent'))
    expect(assistantChatBindingKey('A', 'parent')).not.toBe(assistantChatBindingKey('A', 'other'))
    expect(assistantChatBindingKey('private-token-context', 'parent')).not.toContain('private-token-context')
  })

  it('rejects a stale session epoch after installing current metadata', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'stale', 'child', 0)
    expect(f.controller.state.messages.map(row => row.text)).toEqual(['question'])
    await f.controller.close()
  })

  it('refreshes canonical history on a stream-generation replacement instead of concatenating', async () => {
    const f = fixture()
    await f.controller.send('question')
    f.emit('text-delta', 'first')
    f.history.messages = [{ id: 'answer', messageId: 'answer', role: 'assistant', text: 'replacement' }] as never
    f.live.snapshot = { sessionKey: 'child', taskId: 'task', streamGeneration: 'replacement', currentStreamSeq: 10000, events: [] }
    f.emit('text-delta', 'duplicate suffix', 'child', 1, 'replacement')
    await f.controller.refresh()
    expect(f.controller.state.messages.map(row => row.text)).toEqual(['replacement'])
    await f.controller.close()
  })

  it('does not classify an explicit rejection as uncertain delivery', async () => {
    const f = fixture()
    f.send.mockResolvedValue({ ok: false })
    await f.controller.send('question')
    expect(f.controller.state.uncertainDelivery).toBe(false)
    expect(f.controller.state.messages).toEqual([])
    await f.controller.close()
  })

  it('requires explicit replacement when the persisted child workspace differs', async () => {
    const f = fixture()
    f.lease.metadata = Promise.resolve({ workspaceId: 'other' }) as never
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    await f.controller.restore()
    expect(f.controller.state.requiresNewSession).toBe(true)
    expect(await f.controller.send('question')).toBe(false)
    expect(f.send).not.toHaveBeenCalled()
    await f.controller.close()
  })

  it('reports rejected metadata reads without sending or leaking an unhandled promise', async () => {
    const f = fixture()
    f.lease.metadata = Promise.reject(new Error('read unavailable'))
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    await f.controller.restore()
    expect(f.controller.state.error).toBe('read unavailable')
    expect(f.send).not.toHaveBeenCalled()
    await f.controller.close()
  })

  it.each(['pending-input', 'approval-status', 'active-task-status'])('restores attention from full metadata: %s', async kind => {
    const f = fixture()
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    if (kind === 'pending-input') f.metadata.pendingUserInputs = [{ id: 'clarification' }]
    if (kind === 'approval-status') f.metadata.runStatus = 'approval_pending'
    if (kind === 'active-task-status') f.metadata.activeTask = { status: 'approval_pending' }
    await f.controller.restore()
    expect(f.controller.state.needsAttention).toBe(true)
    await f.controller.refresh()
    expect(f.controller.state.needsAttention).toBe(true)
    await f.controller.close()
  })

  it.each(['last-task', 'history-outcome'])('restores a failed turn after a refresh or reopen: %s', async source => {
    const f = fixture()
    f.values.set(assistantChatBindingKey(f.options.identity, f.options.parentKey), 'child')
    if (source === 'last-task') f.metadata.lastTask = { status: 'failed', turn_outcome: { terminal_message: 'Provider unavailable' } }
    else f.history.turnOutcomes = [{ status: 'failed', finishedAt: 10, outcome: { terminalMessage: 'Provider unavailable' } }] as never
    await f.controller.restore()
    expect(f.controller.state.phase).toBe('error')
    expect(f.controller.state.error).toBe('Provider unavailable')
    f.emit('turn-committed')
    await f.controller.refresh()
    expect(f.controller.state.error).toBe('Provider unavailable')
    await f.controller.close()
  })

  it('does not allow a second send while the first admission ACK is pending after early completion', async () => {
    const f = fixture()
    let acknowledge!: (value: { ok: boolean }) => void
    f.send.mockImplementation(async () => {
      f.emit('turn-completed')
      return await new Promise<{ ok: boolean }>(resolve => { acknowledge = resolve })
    })
    const first = f.controller.send('first')
    await vi.waitFor(() => expect(acknowledge).toBeTypeOf('function'))
    expect(f.controller.state.phase).toBe('ready')
    expect(await f.controller.send('second')).toBe(false)
    expect(f.send).toHaveBeenCalledOnce()
    acknowledge({ ok: true })
    await first
    await f.controller.close()
  })

  it('shows attention immediately for authoritative live clarification and clears its resolved result', async () => {
    const f = fixture()
    await f.controller.send('question')
    const request = { kind: 'user_input', paused: true, request_id: 'input-1', clarify_schema: { fields: [{ name: 'answer', type: 'string', prompt: 'Which one?' }] } }
    f.emit('tool-result', undefined, 'child', 1, 'generation', { approvalResult: request })
    expect(f.controller.state.needsAttention).toBe(true)
    f.emit('tool-result', undefined, 'child', 1, 'generation', { approvalResult: { kind: 'user_input', paused: false, request_id: 'input-1', status: 'answered' } })
    expect(f.controller.state.needsAttention).toBe(false)
    await f.controller.close()
  })
})
