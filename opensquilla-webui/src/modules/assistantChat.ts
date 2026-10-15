import { createConversationSessionRuntime } from './conversationSessionRuntime'
import { conversationEventSessionKey, type ConversationEvent, type ConversationEventProjection } from './conversationEvents'
import type { ConversationEvents } from './conversationEvents'
import type { ConversationEventData } from './conversationEventContent'
import type { SessionLifecycle } from './sessionLifecycle'
import { SessionReadSessionMissingError, type SessionReadLifecycleFactory, type SessionReadPortLease, type SessionReadLease, type SessionReadSnapshotEvent, type SessionReadMetadata, type SessionReadHistoryPage, type SessionReadMessage } from './sessionReadLifecycle'
import { TurnCommandError, type TurnCommands, type TurnSendRequest } from './turnCommands'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import { clarifyRequestFromValue, userInputOutcomeFromValue } from '@/utils/chat/clarify'
import type { ReadMessageText } from '@/utils/chat/historyMessageContent'

type AssistantChatContent = {
  -readonly [K in 'contentRef' | 'previewComplete' | 'contentRevision' | 'contentAvailability' | 'contentUnavailableReason']?: SessionReadMessage[K]
}

export interface AssistantChatMessage extends AssistantChatContent {
  id: string
  messageId?: string
  role: 'user' | 'assistant'
  text: string
}
export interface AssistantChatState {
  childKey: string
  messages: AssistantChatMessage[]
  phase: 'empty' | 'loading' | 'ready' | 'sending' | 'running' | 'error'
  error: string
  requiresNewSession: boolean
  uncertainDelivery: boolean
  needsAttention: boolean
}
export interface AssistantChatOptions {
  parentKey: string
  workspaceId?: string
  identity: string
  isCurrent(): boolean
  generation(): number
  lifecycle: SessionLifecycle
  reads: SessionReadLifecycleFactory
  events: ConversationEvents
  commands: TurnCommands
  readMessageText?: ReadMessageText
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  title: string
  onChange(state: AssistantChatState): void
}

// Only session keys are persisted. A hash keeps the authority namespace compact;
// it is not an authorization boundary. The Gateway still authorizes every read.
// Messages, drafts and connection credentials never enter this local binding.
export function assistantChatBindingKey(identity: string, parentKey: string): string {
  let hash = 0xcbf29ce484222325n
  for (const byte of new TextEncoder().encode(JSON.stringify([identity, parentKey]))) {
    hash = BigInt.asUintN(64, (hash ^ BigInt(byte)) * 0x100000001b3n)
  }
  return `opensquilla.assistant-chat.v1:${hash.toString(16)}`
}

const creations = new Map<string, Promise<string>>()
const bindings = new Map<string, string>()
const FAILED_STATUSES = new Set(['failed', 'error', 'timeout', 'timed_out', 'abandoned', 'interrupted'])
const ATTENTION_STATUSES = new Set(['approval_pending', 'awaiting_input', 'waiting_input', 'clarification_pending'])
const MAX_BODY_BYTES = 512 * 1024

function bodyIdentity(message: AssistantChatMessage): string | null {
  const ref = message.contentRef
  if (!ref?.revision || !ref.sessionId || !ref.messageId || !ref.sessionKey) return null
  return JSON.stringify([message.id, message.role, message.contentRevision, ref.sessionKey, ref.sessionId,
    ref.messageId, ref.source, ref.view, ref.revision, ref.byteLength, ref.sha256])
}

function record(value: unknown): Readonly<Record<string, unknown>> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : null
}

function terminalFailure(metadata: SessionReadMetadata, history: SessionReadHistoryPage): string {
  const task = metadata.activeTask || metadata.lastTask
  const outcomes = [...(history.turnOutcomes || [])].sort((a, b) => (a.finishedAt || a.startedAt || 0) - (b.finishedAt || b.startedAt || 0))
  const recent = outcomes[outcomes.length - 1]
  const source = task || recent
  if (!source || !FAILED_STATUSES.has(String(source.status || '').toLowerCase())) return ''
  const details = record(task?.turn_outcome) || record(task?.turnOutcome) || recent?.outcome
  for (const value of [details?.terminalMessage, details?.terminal_message, task?.terminal_message,
    task?.terminalMessage, details?.reason, task?.terminal_reason, task?.terminalReason, recent?.errorClass, source.status]) {
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

/** One child conversation owner. Closing releases reads, never durable history or a running turn. */
export function createAssistantChat(options: AssistantChatOptions) {
  const binding = assistantChatBindingKey(options.identity, options.parentKey)
  const state: AssistantChatState = {
    childKey: '', messages: [], phase: 'empty', error: '', requiresNewSession: false,
    uncertainDelivery: false, needsAttention: false,
  }
  let disposed = false
  let lease: SessionReadLease | null = null
  let handle: ReturnType<typeof runtime.events.open> | null = null
  let pending: ConversationEventProjection[] = []
  let pendingChars = 0
  let installing = false
  let revision = 0
  let reading: Promise<void> | null = null
  let uncertainRequest: TurnSendRequest | null = null
  let recoveryNeeded = false
  let readInstalled = false
  let sendInFlight = false
  let contentRead: AbortController | null = null
  const runtime = createConversationSessionRuntime<ConversationEvent, SessionReadPortLease>({
    source: options.events, events: { sessionKey: conversationEventSessionKey },
  })
  let cursor = runtime.cursor.createCursor('')
  const reads = options.reads.create({
    cursor: runtime.cursor, subscriptions: runtime.subscriptions,
    getInstalledCursor: () => cursor,
    prepareReadRetirement: key => runtime.events.prepareReadRetirement(key),
  })
  const current = () => !disposed && options.isCurrent()
  const notify = () => { if (current()) options.onChange({ ...state, messages: state.messages.map(row => ({ ...row })) }) }
  const fail = (error: unknown) => {
    if (!current()) return
    state.phase = 'error'
    state.error = error instanceof Error ? error.message : String(error)
    state.requiresNewSession ||= error instanceof SessionReadSessionMissingError
    notify()
  }

  function assistant(payload: ConversationEventData) {
    const id = payload.assistant_message_id || payload.turn_id || payload.task_id || 'live-answer'
    let row = state.messages.find(message => message.id === id)
    if (!row) {
      row = { id, role: 'assistant', text: '', previewComplete: true }
      state.messages.push(row)
    }
    return row
  }

  function apply(kind: SessionReadSnapshotEvent['semanticKind'], payload: ConversationEventData) {
    if (kind === 'text-delta' && payload.presentation !== 'intermediate') {
      const row = assistant(payload)
      // Bound incremental memory; canonical history remains on the Gateway.
      const text = row.text + (payload.text || '')
      row.previewComplete = row.previewComplete !== false && text.length <= MAX_BODY_BYTES
      row.text = text.slice(-MAX_BODY_BYTES)
      delete row.contentRef
      delete row.contentRevision
      state.phase = 'running'
    } else if (kind === 'answer-generation-reset') {
      const row = assistant(payload)
      const text = payload.authoritative_text_snapshot || ''
      row.text = text.slice(-MAX_BODY_BYTES)
      row.previewComplete = text.length <= MAX_BODY_BYTES
      delete row.contentRef
      delete row.contentRevision
    } else if (kind === 'task-running' || kind === 'task-queued') {
      state.phase = 'running'
    } else if (kind === 'turn-completed') {
      if (typeof payload.finalText === 'string') {
        const row = assistant(payload)
        row.text = payload.finalText.slice(-MAX_BODY_BYTES)
        row.previewComplete = payload.finalText.length <= MAX_BODY_BYTES
        delete row.contentRef
        delete row.contentRevision
      }
      state.phase = 'ready'
    } else if (['task-failed', 'turn-failed', 'task-timed-out', 'task-abandoned'].includes(kind)) {
      state.phase = 'error'
      state.error = payload.message || payload.terminal_message || payload.reason || kind
    } else if (kind === 'task-cancelled' || kind === 'task-succeeded') {
      state.phase = 'ready'
    } else if (kind === 'tool-use-started') {
      state.phase = 'running'
    } else if (kind === 'tool-result') {
      // Use the same authoritative tool projection as the full chat approval
      // consumer; arbitrary displayed tool text is not an approval signal.
      if (userInputOutcomeFromValue(payload.approvalResult)) state.needsAttention = false
      else if (clarifyRequestFromValue(payload.approvalResult) || clarifyRequestFromValue(payload.arguments)) {
        state.needsAttention = true
      }
    } else if (kind === 'approval-requested') {
      state.needsAttention = true
    } else if (kind === 'approval-resolved') {
      state.needsAttention = false
    }
  }

  function consume(event: ConversationEventProjection) {
    if (!current() || event.sessionKey !== state.childKey || event.kind !== 'known'
      || event.semanticKind === 'process-completed' || event.semanticKind === 'cron-result') return
    const decision = runtime.cursor.acceptEvent(cursor, {
      sessionKey: event.sessionKey, sessionEpoch: event.payload.epoch,
      streamGeneration: event.streamGeneration, streamSeq: event.streamSeq,
    })
    if (!decision.accepted) return
    if (decision.reset || (event.payload.epoch !== undefined && event.payload.epoch > cursor.sessionEpoch)) {
      // A new generation/epoch needs canonical history before it can append
      // to this view; never mix a replacement answer into the old transcript.
      if (reading) recoveryNeeded = true
      else void refresh().catch(fail)
      return 'dirty' as const
    }
    cursor = decision.cursor
    apply(event.semanticKind, event.payload)
    notify()
    if (event.semanticKind === 'turn-committed') {
      if (reading) recoveryNeeded = true
      else void refresh().catch(fail)
    }
    return 'applied' as const
  }

  async function refresh() {
    if (reading) return reading
    if (!lease || !current()) return
    const active = lease
    const version = ++revision
    installing = true
    readInstalled = false
    state.phase = 'loading'
    notify()
    const operation = (async () => {
      const [live, history, metadata] = await Promise.all([
        active.reconcile(), active.history.latest({ limit: 100 }), active.retryMetadata(),
      ])
      if (!current() || lease !== active || version !== revision) return
      verifyWorkspace(metadata.workspaceId)
      live.assertInstalledCurrent?.()
      const previous = new Map(state.messages.map(row => [row.id, row]))
      const messages: AssistantChatMessage[] = history.messages.filter(row => row.role === 'user' || row.role === 'assistant')
        .map(row => ({ id: row.messageId || row.id, messageId: row.messageId || undefined, role: row.role as 'user' | 'assistant', text: row.text,
          previewComplete: row.previewComplete, contentRef: row.contentRef && { ...row.contentRef },
          contentRevision: row.contentRevision, contentAvailability: row.contentAvailability,
          contentUnavailableReason: row.contentUnavailableReason }))
      let readBody = false
      for (const row of [...messages].reverse()) {
        const old = previous.get(row.id)
        if (row.previewComplete !== false || old?.previewComplete !== true) continue
        const identity = bodyIdentity(row)
        if (identity && identity === bodyIdentity(old)) {
          row.text = old.text
          row.previewComplete = true
          continue
        }
        const ref = row.contentRef
        // Reconcile at most one already-visible full body, never hydrate an
        // entire history page on reopen. A prefix match cannot prove that the
        // canonical suffix/revision still agrees with the streamed answer.
        if (readBody || !options.readMessageText || !identity || !ref || ref.view !== 'display' || ref.sessionKey !== state.childKey
          || ref.messageId !== row.messageId || !Number.isSafeInteger(ref.byteLength) || !ref.byteLength
          || ref.byteLength > MAX_BODY_BYTES || ref.byteLength < 0
          || (row.contentRevision && row.contentRevision !== ref.revision)
          || (row.contentAvailability && row.contentAvailability !== 'ready')
          || row.contentUnavailableReason || old.text.length <= row.text.length) continue
        readBody = true
        const abort = new AbortController()
        contentRead = abort
        const timeout = setTimeout(() => abort.abort(), 15_000)
        try {
          const text = await options.readMessageText(row, abort.signal)
          if (!abort.signal.aborted && new TextEncoder().encode(text).byteLength <= MAX_BODY_BYTES) {
            row.text = text
            row.previewComplete = true
          }
        } catch { /* Keep an explicit preview with the full-conversation entry point. */ }
        finally {
          clearTimeout(timeout)
          if (contentRead === abort) contentRead = null
        }
      }
      if (!current() || lease !== active || version !== revision) return
      live.assertInstalledCurrent?.()
      state.messages = messages
      state.phase = live.activity === 'idle' ? 'ready' : 'running'
      state.error = ''
      // A history refresh alone cannot prove whether a lost send was admitted.
      // Only the original idempotency receipt can resolve uncertain delivery.
      if (uncertainRequest && options.commands.lookupReceipt) {
        const receipt = await options.commands.lookupReceipt({ kind: 'send', request: uncertainRequest })
        if (!current() || lease !== active || version !== revision) return
        if (receipt.status === 'found') { state.uncertainDelivery = false; uncertainRequest = null }
      }
      state.needsAttention = Boolean(metadata.pendingUserInputs?.length)
        || ATTENTION_STATUSES.has(String(metadata.runStatus || '').toLowerCase())
        || ATTENTION_STATUSES.has(String(metadata.activeTask?.status || '').toLowerCase())
      const durableIds = new Set(state.messages.map(row => row.id))
      for (const event of live.snapshot?.events || []) {
        const id = event.payload.assistant_message_id || event.payload.turn_id || event.payload.task_id
        if (event.semanticKind === 'text-delta' && id && durableIds.has(id)) continue
        apply(event.semanticKind, event.payload)
      }
      const failure = terminalFailure(metadata, history)
      if (failure) { state.error = failure; state.phase = 'error' }
      const signal = {
        sessionKey: state.childKey, streamGeneration: live.snapshot?.streamGeneration,
        currentStreamSeq: live.snapshot?.currentStreamSeq,
      }
      cursor = runtime.cursor.observeGeneration(cursor, signal).cursor
      cursor = runtime.cursor.advanceEpoch(cursor, metadata.epoch).cursor
      cursor = runtime.cursor.restoreSnapshot(cursor, signal)
      installing = false
      const queued = pending.splice(0)
      pendingChars = 0
      for (const event of queued) consume(event)
      await live.confirmInstalled?.()
      readInstalled = true
      notify()
    })()
    reading = operation.catch(error => {
      if (current() && lease === active && version === revision) fail(error)
      throw error
    }).finally(() => {
      reading = null; installing = false
      if (recoveryNeeded && current()) { recoveryNeeded = false; void refresh().catch(fail) }
    })
    return reading
  }

  async function attach(key: string) {
    state.childKey = key
    state.phase = 'loading'
    cursor = runtime.cursor.createCursor(key)
    installing = true
    // An empty side panel owns no read and must not join global recovery.
    // Once attached, the hub still requires real reconciliation for this child.
    runtime.events.observeRecoveryRequired(async scope => {
      if (!scope.keys.includes(key) || !current()) return false
      try { await refresh(); return current() } catch (error) { fail(error); return false }
    })
    handle = runtime.events.open(key)
    handle.observe(message => {
      if (!current() || conversationEventSessionKey(message) !== key) return
      if (message.kind === 'approval') {
        state.needsAttention = message.action === 'requested'
        notify()
        return 'applied'
      }
      if (message.kind !== 'conversation') return
      if (installing) {
        pendingChars += JSON.stringify(message.event).length
        if (pendingChars > 1024 * 1024 || pending.length >= 1024) return 'dirty'
        pending.push(message.event)
      } else return consume(message.event) || 'applied'
      return 'applied'
    })
    lease = reads.open({ sessionKey: key })
    // Observe all public read promises even when a failed bootstrap ends early.
    void lease.metadata.catch(fail)
    void lease.criticalRequestsQueued.catch(fail)
    try {
      const [, metadata] = await Promise.all([lease.live, lease.metadata])
      if (!current()) return
      verifyWorkspace(metadata.workspaceId)
      await refresh()
    } catch (error) { fail(error); throw error }
  }

  function verifyWorkspace(workspaceId: string | null) {
    if ((workspaceId || undefined) !== (options.workspaceId || undefined)) {
      state.requiresNewSession = true
      throw new Error('The saved assistant conversation belongs to a different workspace. Create a new conversation.')
    }
  }

  async function restore() {
    try {
      let saved = bindings.get(binding)
      try { saved ||= options.storage.getItem(binding) || undefined } catch { /* Memory ownership still works if storage is disabled. */ }
      if (saved && saved.length <= 1024 && saved !== options.parentKey) await attach(saved)
      else notify()
    } catch (error) { fail(error) }
  }

  async function ensureChild() {
    if (state.childKey) return state.childKey
    let creating = creations.get(binding)
    if (!creating) {
      creating = options.lifecycle.create({ title: options.title, ...(options.workspaceId ? { workspaceId: options.workspaceId } : {}) })
        .then(result => {
          if (!result.key || result.key === options.parentKey) throw new Error('Invalid assistant session identity')
          bindings.set(binding, result.key)
          try { options.storage.setItem(binding, result.key) } catch { /* Do not lose an already-created session. */ }
          return result.key
        }).finally(() => creations.delete(binding))
      creations.set(binding, creating)
    }
    const key = await creating
    if (!current()) return key
    await attach(key)
    return key
  }

  async function send(message: string): Promise<boolean> {
    if (!current() || sendInFlight || !message.trim() || state.requiresNewSession || state.uncertainDelivery
      || (state.childKey && !readInstalled)
      || ['loading', 'sending', 'running'].includes(state.phase)) return false
    sendInFlight = true
    state.phase = 'sending'; state.error = ''; notify()
    let id = ''
    let sent = false
    try {
      const key = await ensureChild()
      if (!current()) return false
      if ((state.phase as AssistantChatState['phase']) === 'running') return false
      state.phase = 'running'
      id = createClientRequestId()
      const generation = options.generation()
      const request: TurnSendRequest = { kind: 'new-turn', params: {
        sessionKey: key, message: message.trim(), clientRequestId: id, clientMessageId: id,
        source: { noMemoryCapture: true },
      } }
      state.messages.push({ id, role: 'user', text: message.trim(), previewComplete: true })
      notify()
      uncertainRequest = request
      sent = true
      const response = await options.commands.send(request, { expectedGeneration: generation, beforeDispatch: () => current() && options.generation() === generation })
      if (!current()) return false
      if (response.status === 'rejected' || response.ok === false) {
        throw new TurnCommandError('rejected', response.reason || 'Message was rejected', undefined, false)
      }
      uncertainRequest = null
      // A fast answer may finish before the admission response arrives.
      // Preserve the event reducer's terminal phase instead of reviving it.
      notify()
      return true
    } catch (error) {
      if (current() && sent && (!(error instanceof TurnCommandError) || error.accepted !== false)) {
        state.uncertainDelivery = true
      } else if (current() && sent) {
        state.messages = state.messages.filter(row => row.id !== id)
        uncertainRequest = null
      }
      fail(error)
      return false
    } finally {
      sendInFlight = false
    }
  }

  async function stop() {
    if (!current() || !state.childKey) return
    try {
      await options.commands.cancel({ sessionKey: state.childKey, source: 'assistant-chat' }, {
        expectedGeneration: options.generation(), beforeDispatch: current,
      })
      if (current()) await refresh()
    } catch (error) { fail(error) }
  }

  async function close() {
    if (disposed) return
    disposed = true; revision++
    contentRead?.abort()
    const active = lease; lease = null
    handle?.close()
    try { await active?.close() } finally { runtime.dispose() }
  }

  return { state, restore, send, stop, refresh, close,
    forget() { bindings.delete(binding); try { options.storage.removeItem(binding) } catch { /* Local storage can be unavailable. */ } },
  }
}
