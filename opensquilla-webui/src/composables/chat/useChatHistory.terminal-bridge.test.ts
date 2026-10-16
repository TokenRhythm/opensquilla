// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, effectScope, ref } from 'vue'
import chatViewSource from '@/views/ChatView.vue?raw'
import type { ChatMessage, ChatRunStatus, ChatRunStatusSource, ChatTurnOutcome } from '@/types/chat'
import { SessionReadFailure, type SessionReadHistoryPage, type SessionReadLease } from '@/modules/sessionReadLifecycle'
import { useChatHistory } from './useChatHistory'
import { useChatStream } from './useChatStream'
import { useChatSessionViewCache } from './useChatSessionViewCache'
import { chatTaskId, useChatTaskOwnership } from './useChatTaskOwnership'

// Run the composition root's actual callback with the real stream and task
// owners. A copied callback would let the wiring silently discard the answer.
const terminalBody = chatViewSource.match(/onTerminalTask:[\s\S]*?=> \{([\s\S]*?)\n  \},\n\}\)/)?.[1]
if (!terminalBody) throw new Error('ChatView terminal history callback is missing')
const applyTerminal = new Function(
  'outcome', 'historyInstalled', 'activeStreamTaskId', 'runStatus', 'chatTaskId',
  'sessionViewCache', 'sessionKey', 'taskOwnership', 'resetStreamLiveTurnState',
  'chatStream', 'messages', 'FINISHED_STREAM_TASK_ID', 'STOPPED_STREAM_TASK_ID', 'applySessionRunState',
  terminalBody,
)

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function page(ids: string[], hasMore = false, terminal = false): SessionReadHistoryPage {
  return {
    messages: ids.map(id => ({
      id, messageId: id, transcriptId: null, role: id === 'user-live' ? 'user' : 'assistant',
      text: id === 'answer-live' ? 'complete answer' : id, createdAt: null,
      reasoningContent: null, routerDecision: null, artifacts: [], toolCalls: [], timeline: [],
      attachments: [], promptAnnotations: [], usage: null, model: null, inputTokens: null, outputTokens: null,
      provenance: { kind: null, sourceSessionKey: null, sourceTool: null }, additional: {},
      turnContext: id.endsWith('-live')
        ? { turnId: 'turn-live', promotedTurnId: null, appliedIteration: null, activityMarkers: [], additional: {} }
        : null,
    })),
    hasMore, oldestCursor: ids[0] ?? null, newestCursor: ids[ids.length - 1] ?? null,
    scope: 'latestWindow', loadedCount: ids.length, pageSize: 50,
    canonicalAvailable: true, canonicalComplete: true, compactionSummaries: [], additional: {},
    turnOutcomes: terminal ? [{
      taskId: 'task-live', turnId: 'turn-live', status: 'succeeded', startedAt: 1, finishedAt: 2,
      outcome: {}, errorClass: null, retryable: null, activitySnapshot: null, usage: null, additional: {},
      replayProof: { usageCallIndex: null, noPriorProviderDispatch: null, replaySafe: null,
        retryAfterMs: null, userMessageId: 'user-live', terminalMessage: null },
    }] : [],
  }
}

const cleanups: Array<() => void> = []
afterEach(() => { for (const cleanup of cleanups.splice(0)) cleanup(); vi.useRealTimers() })

async function harness() {
  const scope = effectScope()
  const messages = ref<ChatMessage[]>([])
  const sessionKey = ref('A')
  const deliveryIdentity = ref<string | null>('gateway:owner')
  const activeStreamTaskId = ref('task-live')
  const runStatus = ref<ChatRunStatus>({ status: 'running', label: '', task: { task_id: 'task-live' } })
  const taskOwnership = useChatTaskOwnership()
  taskOwnership.noteRunning('task-live')
  const applySessionRunState = vi.fn((source: ChatRunStatusSource | null | undefined) => {
    runStatus.value = { status: (source?.run_status ?? 'idle') as ChatRunStatus['status'], label: '',
      task: source?.active_task || source?.last_task || null }
  })
  const stream = scope.run(() => useChatStream({
    messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(false), runStatus,
    applySessionRunState, renderMarkdown: text => text, stripDirectiveTags: text => text,
    stripGeneratedArtifactMarkers: text => text, scrollToBottom: vi.fn(),
  }))!
  const cache = scope.run(() => useChatSessionViewCache({
    sessionKey, deliveryIdentity, messages, waitForLive: true,
    capturePresentation: () => ({ stoppableTaskId: activeStreamTaskId.value, text: 'visible partial answer' }),
  }))!
  const retirePreview = vi.spyOn(cache, 'retirePreview')
  const historyInstalled = vi.spyOn(cache, 'historyInstalled')
  const latest = vi.fn<() => Promise<SessionReadHistoryPage>>()
  const before = vi.fn<() => Promise<SessionReadHistoryPage>>()
  const after = vi.fn<() => Promise<SessionReadHistoryPage>>()
  const lease = { history: { latest, before, after } } as unknown as SessionReadLease
  let currentLease = lease
  const notify = vi.fn((outcome: ChatTurnOutcome, context?: { historyInstalled: boolean }) => applyTerminal(
    outcome, context?.historyInstalled ?? false, activeStreamTaskId, runStatus, chatTaskId,
    cache, sessionKey, taskOwnership, stream.resetLiveTurnState,
    stream, messages, 'finished', 'stopped', applySessionRunState,
  ))
  const history = scope.run(() => useChatHistory({
    sessionReadLeaseReader: { current: () => currentLease }, sessionKey, deliveryIdentity, messages,
    lastHeaderRole: ref(''), lastHeaderDay: ref(''), autoScroll: ref(false),
    preserveLiveTail: computed(() => stream.isStreaming.value),
    stripTimePrefix: text => text, scrollToBottom: vi.fn(), onTerminalTask: notify,
    onHistoryInstalled: historyInstalled,
  }))!
  cleanups.push(() => { history.cleanup(); stream.cleanup(); scope.stop() })
  latest.mockResolvedValueOnce(page(['m4'], true))
  before.mockResolvedValueOnce(page(['m3'], false))
  await history.loadHistory()
  await history.loadEarlierHistory()
  messages.value.push({ role: 'user', messageId: 'user-live', turnId: 'turn-live', text: 'question', ts: null })
  stream.startStreaming()
  stream.setAssistantMessageId('answer-live')
  stream.appendDelta('visible partial answer')
  cache.liveInstalled('A')
  cache.beginLiveRead('A')
  historyInstalled.mockClear()
  return { history, latest, after, messages, stream, sessionKey, deliveryIdentity, activeStreamTaskId, taskOwnership,
    runStatus, notify, cache, historyInstalled, retirePreview, replaceLease: () => { currentLease = { ...lease } } }
}

describe('terminal history bridge keeps the visible answer', () => {
  it('releases execution ownership but keeps output through bridge failure and replaces it once on retry', async () => {
    const h = await harness()
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockRejectedValueOnce(new SessionReadFailure('busy', 'temporary read contention', true))
    expect((await h.history.reconcileHistory())?.ok).toBe(false)
    expect(h.stream.isStreaming.value).toBe(false)
    expect(h.taskOwnership.hasAuthoritativeWork.value).toBe(false)
    expect(h.taskOwnership.stopTargetTaskId.value).toBe('')
    expect(h.messages.value.find(message => message.messageId === 'answer-live')).toMatchObject({
      text: 'visible partial answer', turnId: 'turn-live',
    })
    expect(h.retirePreview).not.toHaveBeenCalled()
    expect(h.historyInstalled).not.toHaveBeenCalled()

    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockResolvedValueOnce(page(['m5', 'user-live', 'answer-live'], false, true))
    expect((await h.history.reconcileHistory())?.ok).toBe(true)
    expect(h.messages.value.filter(message => message.messageId === 'answer-live')).toMatchObject([
      { text: 'complete answer', turnId: 'turn-live', restoredFromHistory: true },
    ])
    expect(h.retirePreview).toHaveBeenCalled()
  })

  it('retains a terminal answer while bounded bridge pages have not reached it', async () => {
    vi.useFakeTimers()
    const h = await harness()
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockResolvedValueOnce(page(['m5'], true)).mockResolvedValueOnce(page(['m6'], true))
    expect(await h.history.reconcileHistory()).toMatchObject({ ok: true, historyContinuation: true })
    expect(h.messages.value.find(message => message.messageId === 'answer-live')?.text).toBe('visible partial answer')
    expect(h.retirePreview).not.toHaveBeenCalled()
    expect(h.historyInstalled).not.toHaveBeenCalled()
    expect(h.taskOwnership.hasAuthoritativeWork.value).toBe(false)
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockResolvedValueOnce(page(['m7', 'user-live', 'answer-live'], false, true))
    await vi.advanceTimersByTimeAsync(50)
    expect(h.messages.value.filter(message => message.messageId === 'answer-live')).toMatchObject([
      { text: 'complete answer', restoredFromHistory: true },
    ])
  })

  it('keeps a cached answer when live is ready but the history bridge is still partial', async () => {
    vi.useFakeTimers()
    const h = await harness()
    // A warm return has a detached preview, with history and live readiness
    // independently owned. The live result alone cannot replace that preview.
    h.sessionKey.value = 'B'
    h.sessionKey.value = 'A'
    h.cache.liveInstalled('A')
    expect(h.cache.showingPreview.value).toBe(true)
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockResolvedValueOnce(page(['m5'], true)).mockResolvedValueOnce(page(['m6'], true))
    expect(await h.history.reconcileHistory()).toMatchObject({ ok: true, historyContinuation: true })
    expect(h.cache.showingPreview.value).toBe(true)
    expect(h.cache.presentation.value?.text).toBe('visible partial answer')
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockResolvedValueOnce(page(['m7', 'user-live', 'answer-live'], false, true))
    await vi.advanceTimersByTimeAsync(50)
    expect(h.cache.showingPreview.value).toBe(false)
    expect(h.cache.messages.value.filter(message => message.messageId === 'answer-live')).toMatchObject([
      { text: 'complete answer', restoredFromHistory: true },
    ])
  })

  it.each(['session', 'lease', 'identity'])('does not install a late bridge after its %s changes', async change => {
    const h = await harness()
    const bridge = deferred<SessionReadHistoryPage>()
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockReturnValueOnce(bridge.promise)
    const recovery = h.history.reconcileHistory()
    await vi.waitFor(() => expect(h.after).toHaveBeenCalledOnce())
    if (change === 'session') h.sessionKey.value = 'B'
    else if (change === 'lease') h.replaceLease()
    else h.deliveryIdentity.value = 'gateway:another-owner'
    h.messages.value = [{ role: 'user', messageId: 'new-owner', text: 'new owner', ts: null }]
    bridge.resolve(page(['m5', 'user-live', 'answer-live'], false, true))
    expect(await recovery).toMatchObject({ ok: false, cancelled: true })
    expect(h.messages.value.map(message => message.messageId)).toEqual(['new-owner'])
    expect(h.retirePreview).not.toHaveBeenCalled()
  })

  it('does not end a successor stream when its predecessor history finishes', async () => {
    const h = await harness()
    const bridge = deferred<SessionReadHistoryPage>()
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockReturnValueOnce(bridge.promise)
    const recovery = h.history.reconcileHistory()
    await vi.waitFor(() => expect(h.after).toHaveBeenCalledOnce())
    h.activeStreamTaskId.value = 'task-next'
    h.taskOwnership.noteRunning('task-next')
    h.runStatus.value = { status: 'running', label: '', task: { task_id: 'task-next' } }
    h.messages.value.push({ role: 'user', messageId: 'user-next', turnId: 'turn-next', text: 'next question', ts: null })
    h.stream.startStreaming()
    h.stream.setAssistantMessageId('answer-next')
    h.stream.appendDelta('next answer')
    bridge.resolve(page(['m5', 'user-live', 'answer-live'], false, true))
    expect((await recovery)?.ok).toBe(true)
    expect(h.stream.isStreaming.value).toBe(true)
    expect(h.taskOwnership.runningTaskId.value).toBe('task-next')
    expect(h.activeStreamTaskId.value).toBe('task-next')
    expect(h.runStatus.value.status).toBe('running')
    expect(h.messages.value.filter(message => message.messageId === 'user-next')).toHaveLength(1)
  })

  it('keeps the prior answer before a new input through a partial bridge', async () => {
    vi.useFakeTimers()
    const h = await harness()
    const bridge = deferred<SessionReadHistoryPage>()
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockReturnValueOnce(bridge.promise).mockResolvedValueOnce(page(['m6'], true))
    const recovery = h.history.reconcileHistory()
    await vi.waitFor(() => expect(h.after).toHaveBeenCalledOnce())
    h.activeStreamTaskId.value = 'task-next'
    h.taskOwnership.noteRunning('task-next')
    h.runStatus.value = { status: 'running', label: '', task: { task_id: 'task-next' } }
    h.messages.value.push({ role: 'user', messageId: 'user-next', turnId: 'turn-next', text: 'next question', ts: null })
    h.stream.startStreaming()
    h.stream.appendDelta('next answer')
    bridge.resolve(page(['m5'], true))
    expect(await recovery).toMatchObject({ ok: true, historyContinuation: true })
    expect(h.messages.value.map(message => message.messageId)).toEqual([
      'm3', 'm4', 'm5', 'm6', 'user-live', 'answer-live', 'user-next',
    ])
    expect(h.stream.isStreaming.value).toBe(true)
    expect(h.retirePreview).not.toHaveBeenCalled()
  })

  it.each(['done', 'task.succeeded', 'task.cancelled'])(
    'retains output detached by %s through successive partial bridges and canonical replacement', async terminal => {
      vi.useFakeTimers()
      const h = await harness()
      const cancelled = terminal === 'task.cancelled'
      // Rich Done binds completedTurnId; lifecycle terminal fallbacks can
      // detach the same durable assistant id without a turn id or outcome.
      h.stream.endStreaming(cancelled ? { reason: 'aborted' } : undefined)
      if (terminal === 'done') h.messages.value[h.messages.value.length - 1]!.turnId = 'turn-live'
      h.activeStreamTaskId.value = 'finished'
      h.taskOwnership.noteTerminal('task-live')
      h.runStatus.value = { status: cancelled ? 'cancelled' : 'idle', label: '',
        task: { task_id: 'task-live', status: cancelled ? 'cancelled' : 'succeeded' } }
      const completedPage = page(['user-live', 'answer-live'], true, true)
      const terminalPage: SessionReadHistoryPage = cancelled
        ? { ...completedPage, turnOutcomes: completedPage.turnOutcomes.map(outcome => ({ ...outcome, status: 'cancelled' })) }
        : completedPage
      h.latest.mockResolvedValueOnce(terminalPage)
      const bridge = deferred<SessionReadHistoryPage>()
      h.after.mockReturnValueOnce(bridge.promise).mockResolvedValueOnce(page(['m6'], true))
      const recovery = h.history.reconcileHistory()
      await vi.waitFor(() => expect(h.after).toHaveBeenCalledOnce())
      h.activeStreamTaskId.value = 'task-next'
      h.taskOwnership.noteRunning('task-next')
      h.runStatus.value = { status: 'running', label: '', task: { task_id: 'task-next' } }
      h.messages.value.push({ role: 'user', messageId: 'user-next', turnId: 'turn-next', text: 'next question', ts: null })
      h.stream.startStreaming()
      h.stream.appendDelta('next answer')
      bridge.resolve(page(['m5'], true))
      expect(await recovery).toMatchObject({ ok: true, historyContinuation: true })
      expect(h.messages.value.map(message => message.messageId)).toEqual([
        'm3', 'm4', 'm5', 'm6', 'user-live', 'answer-live', 'user-next',
      ])

      const successorPage = page(['user-next'], true)
      const nextPage: SessionReadHistoryPage = {
        ...successorPage,
        messages: successorPage.messages.map(message => ({
          ...message, role: 'user',
          turnContext: {
            turnId: 'turn-next', promotedTurnId: null, appliedIteration: null, activityMarkers: [], additional: {},
          },
        })),
      }
      // A's terminal fact falls outside the latest window on the next read.
      // Its retained row must carry the evidence across further continuations.
      h.latest.mockResolvedValueOnce(nextPage)
      h.after.mockResolvedValueOnce(page(['m7'], true)).mockResolvedValueOnce(page(['m8'], true))
      await vi.advanceTimersByTimeAsync(50)
      expect(h.messages.value.map(message => message.messageId)).toEqual([
        'm3', 'm4', 'm5', 'm6', 'm7', 'm8', 'user-live', 'answer-live', 'user-next',
      ])
      expect(h.messages.value.find(message => message.messageId === 'answer-live')).toMatchObject({
        text: 'visible partial answer', turnId: 'turn-live',
        turnOutcome: { taskId: 'task-live', status: cancelled ? 'cancelled' : 'succeeded' },
      })
      expect(h.historyInstalled).not.toHaveBeenCalled()

      h.latest.mockResolvedValueOnce(nextPage)
      h.after.mockResolvedValueOnce({ ...terminalPage, hasMore: false })
      await vi.advanceTimersByTimeAsync(50)
      expect(h.messages.value.filter(message => message.messageId === 'answer-live')).toMatchObject([
        { text: 'complete answer', restoredFromHistory: true },
      ])
      expect(h.messages.value.findIndex(message => message.messageId === 'answer-live'))
        .toBeLessThan(h.messages.value.findIndex(message => message.messageId === 'user-next'))
      expect(h.messages.value.filter(message => message.messageId === 'user-next')).toHaveLength(1)
      expect(h.stream.isStreaming.value).toBe(true)
      expect(h.activeStreamTaskId.value).toBe('task-next')
      expect(h.taskOwnership.runningTaskId.value).toBe('task-next')
      expect(h.runStatus.value.status).toBe('running')
    },
  )

  it.each(['latest', 'bridge page'])('remembers a %s terminal when the bridge fails before a successor retry', async terminalSource => {
    vi.useFakeTimers()
    const h = await harness()
    h.stream.endStreaming()
    h.messages.value[h.messages.value.length - 1]!.turnId = 'turn-live'
    h.activeStreamTaskId.value = 'finished'
    h.taskOwnership.noteTerminal('task-live')
    h.runStatus.value = { status: 'idle', label: '', task: { task_id: 'task-live', status: 'succeeded' } }
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, terminalSource === 'latest'))
    if (terminalSource === 'bridge page') h.after.mockResolvedValueOnce(page(['m5'], true, true))
    h.after.mockRejectedValueOnce(new SessionReadFailure('busy', 'bridge busy', true))
    expect((await h.history.reconcileHistory())?.ok).toBe(false)
    expect(h.messages.value.find(message => message.messageId === 'answer-live')?.turnOutcome?.taskId)
      .toBe('task-live')

    h.activeStreamTaskId.value = 'task-next'
    h.taskOwnership.noteRunning('task-next')
    h.runStatus.value = { status: 'running', label: '', task: { task_id: 'task-next' } }
    h.messages.value.push({ role: 'user', messageId: 'user-next', turnId: 'turn-next', text: 'next question', ts: null })
    h.stream.startStreaming()
    const successorPage = page(['user-next'], true)
    const nextPage: SessionReadHistoryPage = {
      ...successorPage,
      messages: successorPage.messages.map(message => ({
        ...message, role: 'user',
        turnContext: {
          turnId: 'turn-next', promotedTurnId: null, appliedIteration: null, activityMarkers: [], additional: {},
        },
      })),
    }
    h.latest.mockResolvedValueOnce(nextPage)
    h.after.mockResolvedValueOnce(page(['m5'], true)).mockResolvedValueOnce(page(['m6'], true))
    expect(await h.history.reconcileHistory()).toMatchObject({ ok: true, historyContinuation: true })
    expect(h.messages.value.map(message => message.messageId)).toEqual([
      'm3', 'm4', 'm5', 'm6', 'user-live', 'answer-live', 'user-next',
    ])
    expect(h.messages.value.find(message => message.messageId === 'user-next')?.turnOutcome).toBeUndefined()
    expect(h.activeStreamTaskId.value).toBe('task-next')
    expect(h.taskOwnership.runningTaskId.value).toBe('task-next')
  })

  it.each(['failed', 'cancelled', 'timeout', 'abandoned', 'interrupted'])(
    'keeps the %s outcome and partial output without leaving Stop active', async status => {
      const h = await harness()
      const latest = page(['user-live', 'answer-live'], true, true)
      h.latest.mockResolvedValueOnce({ ...latest, turnOutcomes: [{ ...latest.turnOutcomes[0]!, status }] })
      h.after.mockRejectedValueOnce(new SessionReadFailure('timeout', 'bridge timed out', true))
      expect((await h.history.reconcileHistory())?.ok).toBe(false)
      expect(h.messages.value.find(message => message.messageId === 'answer-live')).toMatchObject({
        text: 'visible partial answer', turnId: 'turn-live', turnOutcome: { taskId: 'task-live', status },
      })
      expect(h.stream.isStreaming.value).toBe(false)
      expect(h.taskOwnership.stopTargetTaskId.value).toBe('')
    },
  )

  it('replaces a retained legacy answer without a message id only after the bridge succeeds', async () => {
    const h = await harness()
    h.stream.setAssistantMessageId('')
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockRejectedValueOnce(new SessionReadFailure('busy', 'bridge busy', true))
    await h.history.reconcileHistory()
    expect(h.messages.value.filter(message => message.text === 'visible partial answer')).toHaveLength(1)
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    h.after.mockResolvedValueOnce(page(['m5', 'user-live', 'answer-live'], false, true))
    await h.history.reconcileHistory()
    expect(h.messages.value.filter(message => message.turnId === 'turn-live' && message.role === 'assistant')).toMatchObject([
      { messageId: 'answer-live', text: 'complete answer' },
    ])
  })

  it('does not retain a second legacy answer when a partial bridge already contains its canonical row', async () => {
    vi.useFakeTimers()
    const h = await harness()
    h.stream.setAssistantMessageId('')
    h.latest.mockResolvedValueOnce(page(['user-live', 'answer-live'], true, true))
    const successorPage = page(['next-user'], true)
    const successor: SessionReadHistoryPage = {
      ...successorPage,
      messages: successorPage.messages.map(message => ({
        ...message, role: 'user',
        turnContext: {
          turnId: 'turn-next', promotedTurnId: null, appliedIteration: null, activityMarkers: [], additional: {},
        },
      })),
    }
    h.after.mockResolvedValueOnce(page(['m5', 'user-live', 'answer-live'], true, true))
      .mockResolvedValueOnce(successor)
    expect(await h.history.reconcileHistory()).toMatchObject({ ok: true, historyContinuation: true })
    expect(h.messages.value.filter(message => message.turnId === 'turn-live' && message.role === 'assistant')).toMatchObject([
      { messageId: 'answer-live', text: 'complete answer', restoredFromHistory: true },
    ])
  })
})
