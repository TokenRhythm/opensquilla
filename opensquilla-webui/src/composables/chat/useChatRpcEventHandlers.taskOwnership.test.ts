import { describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatRunStatus,
  ChatRunStatusSource,
  ChatRunStatusState,
} from '@/types/chat'
import {
  useChatRpcEventHandlers,
  type ChatRpcStreamApi,
  type UseChatRpcEventHandlersOptions,
} from './useChatRpcEventHandlers'
import { chatTaskId, useChatTaskOwnership } from './useChatTaskOwnership'

const SESSION = 'agent:main:webchat:task-ownership'

function makeStream(): ChatRpcStreamApi {
  const isStreaming = ref(true)
  return {
    isStreaming,
    streamBubble: ref(false),
    streamHasVisibleOutput: ref(false),
    startStreaming: vi.fn(() => { isStreaming.value = true }),
    endStreaming: vi.fn(() => { isStreaming.value = false }),
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
    useReducer: ref(false),
  }
}

function projectedRunState(
  source: ChatRunStatusSource | null | undefined,
): ChatRunStatus {
  const active = source?.active_task || source?.activeTask || null
  const last = source?.last_task || source?.lastTask || null
  const rawStatus = String(
    source?.run_status
    || source?.runStatus
    || active?.status
    || last?.status
    || 'idle',
  ).toLowerCase()
  const status = rawStatus === 'succeeded'
    ? 'idle'
    : rawStatus === 'abandoned'
      ? 'interrupted'
      : rawStatus
  return {
    status: status as ChatRunStatusState,
    label: status,
    task: ['queued', 'running', 'approval_pending'].includes(status) ? active : last,
  }
}

function makeHarness() {
  const stream = makeStream()
  const messages: Ref<ChatMessage[]> = ref([])
  const activeStreamTaskId = ref('task-A')
  const taskOwnership = useChatTaskOwnership()
  taskOwnership.noteRunning('task-A')

  const applySessionRunState = vi.fn((source: ChatRunStatusSource | null | undefined) => {
    const active = source?.active_task || source?.activeTask || null
    const last = source?.last_task || source?.lastTask || null
    const activeStatus = String(active?.status || '').toLowerCase()
    if (activeStatus === 'running' || activeStatus === 'approval_pending') {
      taskOwnership.noteRunning(active || '')
    } else if (activeStatus === 'queued') {
      taskOwnership.noteQueued(active || '')
    }
    const lastStatus = String(last?.status || '').toLowerCase()
    if (
      chatTaskId(last)
      && ['succeeded', 'failed', 'cancelled', 'timeout', 'abandoned', 'interrupted']
        .includes(lastStatus)
    ) {
      taskOwnership.noteTerminal(chatTaskId(last))
    }
  })

  const options: UseChatRpcEventHandlersOptions = {
    sessionKey: ref(SESSION),
    currentEpoch: ref(0),
    lastStreamSeq: ref(0),
    activeTaskGroups: ref(new Set<string>()),
    taskOwnership,
    activeStreamTaskId,
    aborted: ref(false),
    messages,
    pendingQueue: ref([{ id: 'pending-C', text: 'C', status: 'queued' }] as never),
    usageAccum: ref({
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      cost: null,
      routedTurns: 0,
      sessionSaved: 0,
    }),
    usageModel: ref(''),
    stream,
    normalizeRunStatus: status => status.toLowerCase(),
    sessionRunStatus: projectedRunState,
    applySessionRunState,
    queueRouterDecision: vi.fn(),
    appendEnsembleProgress: vi.fn(),
    markEnsembleHandoff: vi.fn(),
    flushPendingRouterDecision: vi.fn(),
    clearPendingRouterDecision: vi.fn(),
    handleRouterControlReplay: vi.fn(),
    showCompactionToast: vi.fn(),
    showWarningToast: vi.fn(),
    scheduleHistorySync: vi.fn(),
    schedulePendingDrainAfterTerminal: vi.fn(),
    popAllPendingIntoComposer: vi.fn(() => false),
    saveWidgetState: vi.fn(),
    handleSessionConnectionState: vi.fn(),
    loadCurrentSessionUsage: vi.fn(),
  }
  const scope = effectScope()
  const api = scope.run(() => useChatRpcEventHandlers(options))!
  return { api, options, stream, activeStreamTaskId, taskOwnership, scope }
}

describe('task ownership event races', () => {
  it('drops stopped-task text deltas while retaining terminal ownership', () => {
    const { api, stream, taskOwnership, scope } = makeHarness()
    expect(taskOwnership.beginStop()).toBe('task-A')

    api.handlers.onTextDelta({
      task_id: 'task-A',
      session_key: SESSION,
      stream_seq: 1,
      text: 'late provider text',
    })

    expect(stream.appendDelta).not.toHaveBeenCalled()
    expect(stream.resetStreamIdleTimer).toHaveBeenCalled()
    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(taskOwnership.stopRequestedTaskId.value).toBe('task-A')
    scope.stop()
  })

  it.each([
    ['session.event.done', { reason: 'completed', text: 'answer A' }, undefined],
    ['task.failed', { terminal_message: 'A failed' }, undefined],
    ['task.timeout', { terminal_message: 'A timed out' }, undefined],
    ['task.cancelled', { terminal_message: 'A was stopped' }, { reason: 'aborted' }],
  ])(
    'hands buffered B output over after A terminal %s without draining C',
    (event, extra, expectedEndArgument) => {
      const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
      if (event === 'task.cancelled') {
        expect(taskOwnership.beginStop()).toBe('task-A')
      }

      api.handlers.onTaskQueued({ task_id: 'task-B', session_key: SESSION })
      api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })
      api.handlers.onTextDelta({
        task_id: 'task-B',
        session_key: SESSION,
        stream_seq: 10,
        text: 'first B token',
      })

      expect(activeStreamTaskId.value).toBe('task-A')
      expect(stream.appendDelta).not.toHaveBeenCalled()

      api.handlers.onAny(event, {
        task_id: 'task-A',
        session_key: SESSION,
        stream_seq: 11,
        ...extra,
      })

      expect(stream.endStreaming).toHaveBeenCalledTimes(1)
      if (expectedEndArgument) {
        expect(stream.endStreaming).toHaveBeenCalledWith(expectedEndArgument)
      }
      expect(activeStreamTaskId.value).toBe('task-B')
      expect(taskOwnership.runningTaskId.value).toBe('task-B')
      expect(taskOwnership.stopRequestedTaskId.value).toBe('')
      expect(stream.appendDelta).toHaveBeenCalledWith('first B token')
      expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
      expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
      scope.stop()
    },
  )

  it('uses last_task A for terminal identity while active_task B remains running', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteQueued('task-B')
    expect(taskOwnership.beginStop()).toBe('task-A')
    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })
    api.handlers.onTextDelta({
      task_id: 'task-B',
      session_key: SESSION,
      stream_seq: 20,
      text: 'B started before A terminal broadcast',
    })

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'running',
      active_task: { task_id: 'task-B', status: 'running' },
      last_task: { task_id: 'task-A', status: 'cancelled' },
      stream_seq: 21,
    } as never)

    expect(stream.endStreaming).toHaveBeenCalledWith({ reason: 'aborted' })
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(taskOwnership.runningTaskId.value).toBe('task-B')
    expect(taskOwnership.stopRequestedTaskId.value).toBe('')
    expect(stream.appendDelta).toHaveBeenCalledWith('B started before A terminal broadcast')
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('clears A task groups and hands off to B when last_task A is cancelled', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    options.activeTaskGroups.value = new Set(['group-owned-by-A'])
    taskOwnership.noteQueued('task-B')
    expect(taskOwnership.beginStop()).toBe('task-A')
    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'running',
      active_task: { task_id: 'task-B', status: 'running' },
      last_task: { task_id: 'task-A', status: 'cancelled' },
    } as never)

    expect(options.activeTaskGroups.value.size).toBe(0)
    expect(taskOwnership.stopRequestedTaskId.value).toBe('')
    expect(taskOwnership.runningTaskId.value).toBe('task-B')
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(stream.endStreaming).toHaveBeenCalledWith({ reason: 'aborted' })
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    scope.stop()
  })

  it('hands off successful A to running B while retaining its background task group', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    options.activeTaskGroups.value = new Set(['background-group'])
    taskOwnership.noteQueued('task-B')
    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })
    api.handlers.onTextDelta({
      task_id: 'task-B',
      session_key: SESSION,
      stream_seq: 1,
      text: 'B while group continues',
    })

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'running',
      active_task: { task_id: 'task-B', status: 'running' },
      last_task: { task_id: 'task-A', status: 'succeeded' },
      stream_seq: 2,
    } as never)

    expect([...options.activeTaskGroups.value]).toEqual(['background-group'])
    expect(taskOwnership.runningTaskId.value).toBe('task-B')
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(stream.appendDelta).toHaveBeenCalledWith('B while group continues')
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()

    api.handlers.onTaskGroupDone({
      session_key: SESSION,
      stream_seq: 3,
      group_id: 'background-group',
    })

    expect(options.activeTaskGroups.value.size).toBe(0)
    expect(taskOwnership.runningTaskId.value).toBe('task-B')
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(options.applySessionRunState).not.toHaveBeenLastCalledWith(expect.objectContaining({
      run_status: 'idle',
    }))
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    scope.stop()
  })

  it('projects queued successor B when sessions.changed repeats completed A', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteQueued('task-B')

    api.handlers.onAny('session.event.done', {
      task_id: 'task-A',
      session_key: SESSION,
      stream_seq: 1,
      reason: 'completed',
      text: 'answer A',
    })
    vi.mocked(options.applySessionRunState).mockClear()

    const continuation = {
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'queued',
      active_task: { task_id: 'task-B', status: 'queued' },
      last_task: { task_id: 'task-A', status: 'succeeded' },
      stream_seq: 2,
    }
    api.handlers.onSessionsChanged(continuation as never)

    expect(options.applySessionRunState).toHaveBeenCalledWith(continuation)
    expect(taskOwnership.runningTaskId.value).toBe('')
    expect([...taskOwnership.queuedTaskIds.value]).toEqual(['task-B'])
    expect(activeStreamTaskId.value).not.toBe('task-B')
    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    scope.stop()
  })

  it('does not close successor B when sessions.changed repeats terminal A', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteQueued('task-B')
    expect(taskOwnership.beginStop()).toBe('task-A')
    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-A',
      session_key: SESSION,
      terminal_message: 'A stopped',
    })
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(stream.endStreaming).toHaveBeenCalledTimes(1)

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'running',
      active_task: { task_id: 'task-B', status: 'running' },
      last_task: { task_id: 'task-A', status: 'cancelled' },
    } as never)

    expect(taskOwnership.runningTaskId.value).toBe('task-B')
    expect(activeStreamTaskId.value).toBe('task-B')
    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('removes terminal queued B without ending running A', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteQueued('task-B')

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'B was cancelled while queued',
    })

    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(taskOwnership.queuedTaskIds.value.has('task-B')).toBe(false)
    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('does not let a changed_task-only terminal for queued B overwrite running A', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteQueued('task-B')

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      changed_task: { task_id: 'task-B', status: 'cancelled' },
    } as never)

    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(taskOwnership.queuedTaskIds.value.has('task-B')).toBe(false)
    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    scope.stop()
  })

  it('does not promote changed_task-only running B over authoritative running A', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_running',
      changed_task: { task_id: 'task-B', status: 'running' },
    } as never)

    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.applySessionRunState).not.toHaveBeenCalled()
    scope.stop()
  })

  it('keeps running A after queued B terminal is followed by its sessions.changed projection', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteQueued('task-B')

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'B was cancelled while queued',
    })
    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'running',
      active_task: { task_id: 'task-A', status: 'running' },
      last_task: { task_id: 'task-B', status: 'cancelled' },
    } as never)

    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(taskOwnership.queuedTaskIds.value.has('task-B')).toBe(false)
    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('replays a successor that finishes before the predecessor terminal without draining C', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()

    api.handlers.onTaskQueued({ task_id: 'task-B', session_key: SESSION })
    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })
    api.handlers.onTextDelta({
      task_id: 'task-B',
      session_key: SESSION,
      stream_seq: 10,
      text: 'complete B answer',
    })
    api.handlers.onAny('session.event.done', {
      task_id: 'task-B',
      session_key: SESSION,
      stream_seq: 11,
      reason: 'completed',
      text: 'complete B answer',
    })

    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.appendDelta).not.toHaveBeenCalled()
    expect(stream.endStreaming).not.toHaveBeenCalled()

    api.handlers.onAny('session.event.done', {
      task_id: 'task-A',
      session_key: SESSION,
      stream_seq: 12,
      reason: 'completed',
      text: 'complete A answer',
    })

    expect(stream.appendDelta).toHaveBeenCalledWith('complete B answer')
    expect(stream.endStreaming).toHaveBeenCalledTimes(2)
    expect(stream.reconcileFinalText).toHaveBeenCalledWith('complete B answer')
    expect(activeStreamTaskId.value).not.toBe('task-B')
    expect(taskOwnership.runningTaskId.value).toBe('')
    expect(taskOwnership.hasAuthoritativeWork.value).toBe(false)
    expect(options.schedulePendingDrainAfterTerminal).toHaveBeenCalledTimes(1)
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('accepts a queued-only Stop terminal despite an empty render owner', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    taskOwnership.noteTerminal('task-A')
    taskOwnership.noteQueued('task-B')
    activeStreamTaskId.value = ''
    stream.isStreaming.value = false
    expect(taskOwnership.beginStop()).toBe('task-B')

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'queued task cancelled',
    })

    expect(taskOwnership.queuedTaskIds.value.size).toBe(0)
    expect(taskOwnership.stopRequestedTaskId.value).toBe('')
    expect(taskOwnership.hasAuthoritativeWork.value).toBe(false)
    expect(options.applySessionRunState).toHaveBeenCalledWith(expect.objectContaining({
      run_status: 'cancelled',
      last_task: expect.objectContaining({ task_id: 'task-B', status: 'cancelled' }),
    }))
    expect(options.scheduleHistorySync).toHaveBeenCalled()
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('uses changed_task as the exact terminal when the Gateway snapshot failed', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    expect(taskOwnership.beginStop()).toBe('task-A')

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      changed_task: {
        task_id: 'task-A',
        status: 'cancelled',
        terminal_reason: 'user_abort',
      },
    } as never)

    expect(stream.endStreaming).toHaveBeenCalledWith({ reason: 'aborted' })
    expect(taskOwnership.runningTaskId.value).toBe('')
    expect(taskOwnership.stopRequestedTaskId.value).toBe('')
    expect(activeStreamTaskId.value).not.toBe('task-A')
    expect(options.scheduleHistorySync).toHaveBeenCalled()
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(options.popAllPendingIntoComposer).not.toHaveBeenCalled()
    scope.stop()
  })

  it('fails closed instead of draining C after a successful A terminal without a snapshot', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      changed_task: {
        task_id: 'task-A',
        status: 'succeeded',
      },
    } as never)

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(taskOwnership.runningTaskId.value).toBe('')
    expect(activeStreamTaskId.value).not.toBe('task-A')
    // Snapshot generation failed, so absence of active_task is not evidence
    // that queued B does not exist. Keep admission unresolved until hydrate
    // proves the session idle; otherwise staged C can overtake B.
    expect(taskOwnership.hydrationResolved.value).toBe(false)
    expect(taskOwnership.hasAuthoritativeWork.value).toBe(true)
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    expect(options.scheduleHistorySync).toHaveBeenCalled()
    scope.stop()
  })

  it('does not let a stale-epoch terminal clear the current task owner', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    options.currentEpoch.value = 5

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-A',
      session_key: SESSION,
      epoch: 4,
      terminal_message: 'late cancellation from an older gateway epoch',
    })

    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(taskOwnership.hasAuthoritativeWork.value).toBe(true)
    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.applySessionRunState).not.toHaveBeenCalled()
    expect(options.schedulePendingDrainAfterTerminal).not.toHaveBeenCalled()
    scope.stop()
  })

  it.each(['queued', 'running'])(
    'does not let a stale-epoch task.%s mutate current ownership',
    (status) => {
      const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
      options.currentEpoch.value = 5

      const payload = {
        task_id: 'task-B-old-epoch',
        session_key: SESSION,
        epoch: 4,
      }
      if (status === 'queued') api.handlers.onTaskQueued(payload)
      else api.handlers.onTaskRunning(payload)

      expect(taskOwnership.runningTaskId.value).toBe('task-A')
      expect(taskOwnership.queuedTaskIds.value.size).toBe(0)
      expect(activeStreamTaskId.value).toBe('task-A')
      expect(stream.endStreaming).not.toHaveBeenCalled()
      expect(options.applySessionRunState).not.toHaveBeenCalled()
      scope.stop()
    },
  )

  it('does not let a stale-epoch approval event replace the current run state', () => {
    const { api, options, stream, activeStreamTaskId, taskOwnership, scope } = makeHarness()
    options.currentEpoch.value = 5

    api.handlers.onAny('session.event.approval_required', {
      task_id: 'task-B-old-epoch',
      session_key: SESSION,
      epoch: 4,
      status: 'approval_pending',
    })

    expect(taskOwnership.runningTaskId.value).toBe('task-A')
    expect(activeStreamTaskId.value).toBe('task-A')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.applySessionRunState).not.toHaveBeenCalled()
    scope.stop()
  })
})
