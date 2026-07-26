// Issue #344: a stale task's late stream events bled into the current turn.
//
// Repro: task-A (PPTX→PDF) failed, the user then sent task-B (image→HTML), but
// task-A's late tool_use_start / artifact / terminal-error kept rendering in —
// and ending — the turn that was supposed to belong to task-B. The events were
// only filtered by session key, so same-session stale events passed through.
//
// The fix binds the live stream to a single `activeStreamTaskId` and drops
// events tagged with a different task. These tests drive the real handler entry
// points and assert task-A's events no longer touch task-B's turn, while
// task-B's own events (and legacy untagged events) still flow.
import { describe, expect, it, vi } from 'vitest'
import { effectScope, ref, type Ref } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { ChatMessage, ChatRunStatus, ChatRunStatusSource } from '@/types/chat'
import type { ToolUsePayload } from '@/types/rpc'
import {
  useChatRpcEventHandlers,
  type ChatRpcStreamApi,
  type UseChatRpcEventHandlersOptions,
} from './useChatRpcEventHandlers'
import { FINISHED_STREAM_TASK_ID, PENDING_STREAM_TASK_ID } from '@/utils/chat/streamEvents'

const SESSION = 'agent:main:webchat:issue344'

function makeStream(): ChatRpcStreamApi {
  return {
    isStreaming: ref(true),
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
    useReducer: ref(false),
  }
}

function makeHarness(activeStreamTaskId = '') {
  const stream = makeStream()
  const messages: Ref<ChatMessage[]> = ref([])
  const activeTaskId = ref(activeStreamTaskId)
  const options: UseChatRpcEventHandlersOptions = {
    sessionKey: ref(SESSION),
    currentEpoch: ref(0),
    lastStreamSeq: ref(0),
    activeTaskGroups: ref(new Set<string>()),
    activeStreamTaskId: activeTaskId,
    aborted: ref(false),
    messages,
    pendingQueue: ref([]),
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
    normalizeRunStatus: (s: string) => s,
    sessionRunStatus: () => ({ status: 'idle', label: 'Idle', task: null }),
    applySessionRunState: vi.fn(),
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
    subscribeSession: vi.fn(),
    loadHistory: vi.fn(),
    loadCurrentSessionUsage: vi.fn(),
  }
  const scope = effectScope()
  const api = scope.run(() => useChatRpcEventHandlers(options))!
  return { api, options, stream, messages, activeTaskId, scope }
}

function toolUse(taskId: string | undefined, toolName: string): ToolUsePayload {
  return {
    session_key: SESSION,
    stream_seq: 1,
    task_id: taskId,
    tool_use_id: `${toolName}-id`,
    tool_name: toolName,
  } as unknown as ToolUsePayload
}

describe('issue #344 — live stream is bound to a single task', () => {
  it("drops a stale task's tool_use_start while another task owns the live stream", () => {
    const { api, stream } = makeHarness('task-B')
    api.handlers.onToolUseStart(toolUse('task-A', 'create_pdf.py'))
    expect(stream.appendToolCall).not.toHaveBeenCalled()
  })

  it("appends the active task's own tool_use_start", () => {
    const { api, stream } = makeHarness('task-B')
    api.handlers.onToolUseStart(toolUse('task-B', 'write_html'))
    expect(stream.appendToolCall).toHaveBeenCalledTimes(1)
  })

  it('still appends untagged events so a legacy backend keeps working', () => {
    const { api, stream } = makeHarness('task-B')
    api.handlers.onToolUseStart(toolUse(undefined, 'shell'))
    expect(stream.appendToolCall).toHaveBeenCalledTimes(1)
  })

  it("does not end the current stream on a stale task's terminal error", () => {
    const { api, stream, messages } = makeHarness('task-B')
    api.handlers.onAny('task.failed', {
      task_id: 'task-A',
      session_key: SESSION,
      terminal_message: '图片转文字PDF错误',
    })
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(messages.value.some((m) => m.role === 'error')).toBe(false)
  })

  it("does not let a stale task's heartbeat open a new work card", () => {
    const { api, stream } = makeHarness('task-B')
    stream.isStreaming.value = false

    api.handlers.onRunHeartbeat({
      task_id: 'task-A',
      session_key: SESSION,
      stream_seq: 1,
    })

    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it("does not let a stale task's router replay reopen the live turn", () => {
    const { api, options } = makeHarness('task-B')

    api.handlers.onRouterControlReplay({
      task_id: 'task-A',
      session_key: SESSION,
      stream_seq: 1,
    })

    expect(options.handleRouterControlReplay).not.toHaveBeenCalled()
  })

  it('does not reopen a completed task when a same-task heartbeat arrives after done', () => {
    const { api, stream, activeTaskId } = makeHarness('task-B')
    vi.mocked(stream.endStreaming).mockImplementation(() => {
      stream.isStreaming.value = false
    })

    api.handlers.onAny('session.event.done', {
      task_id: 'task-B',
      session_key: SESSION,
      stream_seq: 1,
      text: 'finished answer',
    })
    api.handlers.onRunHeartbeat({
      task_id: 'task-B',
      session_key: SESSION,
      stream_seq: 2,
    })

    expect(activeTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
    expect(stream.startStreaming).not.toHaveBeenCalled()
  })

  it('uses task.succeeded as a fallback when the rich done frame is missing', () => {
    const { api, stream, messages, activeTaskId } = makeHarness('task-B')
    vi.mocked(stream.endStreaming).mockImplementation(() => {
      messages.value.push({ role: 'assistant', text: 'finished answer', ts: 'now' })
    })

    api.handlers.onAny('task.succeeded', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_reason: 'completed',
    })

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(activeTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
    expect(messages.value[0]?.usage).toBeUndefined()
  })

  it("does not end the current stream on a stale task's terminal sessions.changed", () => {
    const { api, stream, options } = makeHarness('task-B')
    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'cancelled',
      last_task: { task_id: 'task-A', status: 'cancelled' },
    } as never)
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(options.applySessionRunState).not.toHaveBeenCalled()
  })

  it("ends the current stream on the active task's terminal error", () => {
    const { api, stream, messages } = makeHarness('task-B')
    api.handlers.onAny('task.failed', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'HTML generation failed',
    })
    expect(stream.endStreaming).toHaveBeenCalled()
    expect(messages.value.some((m) => m.role === 'error')).toBe(true)
  })

  it('localizes the stable Ensemble image error code and keeps the code attached', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const { api, messages, scope } = makeHarness('task-B')

    api.handlers.onAny('task.failed', {
      task_id: 'task-B',
      session_key: SESSION,
      code: 'ensemble_multimodal_unsupported',
      terminal_message: 'server fallback text',
    })

    expect(messages.value[messages.value.length - 1]).toMatchObject({
      role: 'error',
      errorCode: 'ensemble_multimodal_unsupported',
      text: 'Ensemble 暂不支持图片输入，请切换到单模型路由后重试。',
    })
    scope.stop()
    i18n.global.locale.value = 'en'
  })

  it('binds activeStreamTaskId from task.running, then filters the prior task', () => {
    const { api, options, stream } = makeHarness('')
    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })
    expect(options.activeStreamTaskId.value).toBe('task-B')
    api.handlers.onToolUseStart(toolUse('task-A', 'create_pdf.py'))
    expect(stream.appendToolCall).not.toHaveBeenCalled()
  })

  it('buffers early cancellation until the send response binds the queued task', () => {
    const { api, options, stream } = makeHarness(PENDING_STREAM_TASK_ID)

    api.handlers.onTaskQueued({ task_id: 'task-B', session_key: SESSION })
    expect(options.activeStreamTaskId.value).toBe(PENDING_STREAM_TASK_ID)

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'The task was cancelled before it finished.',
    })

    expect(stream.endStreaming).not.toHaveBeenCalled()

    api.bindActiveStreamTask('task-B')

    expect(stream.endStreaming).toHaveBeenCalled()
  })

  it('buffers a tagged terminal event while the accepted task id is pending', () => {
    const { api, options, stream, messages, activeTaskId } = makeHarness(PENDING_STREAM_TASK_ID)

    api.handlers.onAny('task.failed', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'The accepted task failed before the response arrived.',
    })

    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(messages.value).toEqual([])

    api.bindActiveStreamTask('task-B')

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(messages.value[messages.value.length - 1]).toMatchObject({
      role: 'error',
      text: 'The accepted task failed before the response arrived.',
    })
    expect(options.scheduleHistorySync).toHaveBeenCalledTimes(1)
    expect(activeTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
  })

  it('consumes only the buffered terminal event matching the response task id', () => {
    const { api, stream, messages, activeTaskId } = makeHarness(PENDING_STREAM_TASK_ID)

    api.handlers.onAny('task.failed', {
      task_id: 'task-A',
      session_key: SESSION,
      terminal_message: 'Stale task A failed.',
    })
    api.handlers.onAny('task.succeeded', {
      task_id: 'task-B',
      session_key: SESSION,
    })

    api.bindActiveStreamTask('task-B')

    expect(stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(messages.value.some(message => message.text.includes('Stale task A'))).toBe(false)
    expect(activeTaskId.value).toBe(FINISHED_STREAM_TASK_ID)
  })

  it('drops a buffered stale terminal event when the response binds another task', () => {
    const { api, stream, messages, activeTaskId } = makeHarness(PENDING_STREAM_TASK_ID)

    api.handlers.onAny('task.failed', {
      task_id: 'task-A',
      session_key: SESSION,
      terminal_message: 'Stale task A failed.',
    })

    api.bindActiveStreamTask('task-B')

    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(messages.value).toEqual([])
    expect(activeTaskId.value).toBe('task-B')
  })

  it('does not let another running task claim a pending send before its response', () => {
    const { api, stream, messages, activeTaskId } = makeHarness(PENDING_STREAM_TASK_ID)

    api.handlers.onTaskRunning({ task_id: 'task-A', session_key: SESSION })
    api.handlers.onAny('task.failed', {
      task_id: 'task-A',
      session_key: SESSION,
      terminal_message: 'Unrelated task A failed.',
    })

    expect(activeTaskId.value).toBe(PENDING_STREAM_TASK_ID)
    expect(stream.endStreaming).not.toHaveBeenCalled()

    api.bindActiveStreamTask('task-B')

    expect(activeTaskId.value).toBe('task-B')
    expect(stream.endStreaming).not.toHaveBeenCalled()
    expect(messages.value.some(message => message.text.includes('task A'))).toBe(false)
  })

  it('replays early stream frames only after the response binds their task', () => {
    const { api, stream, activeTaskId } = makeHarness(PENDING_STREAM_TASK_ID)
    const earlyTool = toolUse('task-B', 'write_report')

    api.handlers.onTaskRunning({ task_id: 'task-B', session_key: SESSION })
    api.handlers.onToolUseStart(earlyTool)

    expect(activeTaskId.value).toBe(PENDING_STREAM_TASK_ID)
    expect(stream.appendToolCall).not.toHaveBeenCalled()

    api.bindActiveStreamTask('task-B')

    expect(activeTaskId.value).toBe('task-B')
    expect(stream.appendToolCall).toHaveBeenCalledTimes(1)
    expect(stream.appendToolCall).toHaveBeenCalledWith(earlyTool)
  })

  it('bounds early stream buffering while preserving the newest frames', () => {
    const { api, stream } = makeHarness(PENDING_STREAM_TASK_ID)

    for (let index = 0; index < 70; index++) {
      api.handlers.onTextDelta({
        task_id: 'task-B',
        session_key: SESSION,
        stream_seq: index + 1,
        text: `delta-${index}`,
      })
    }

    api.bindActiveStreamTask('task-B')

    expect(stream.appendDelta).toHaveBeenCalledTimes(64)
    const calls = vi.mocked(stream.appendDelta).mock.calls
    expect(calls[0]?.[0]).toBe('delta-6')
    expect(calls[calls.length - 1]?.[0]).toBe('delta-69')
  })

  it('bounds pending terminal task buckets and retains the newest tasks', () => {
    const oldest = makeHarness(PENDING_STREAM_TASK_ID)
    for (let index = 0; index < 9; index++) {
      oldest.api.handlers.onAny('task.failed', {
        task_id: `task-${index}`,
        session_key: SESSION,
        terminal_message: `Task ${index} failed.`,
      })
    }

    oldest.api.bindActiveStreamTask('task-0')
    expect(oldest.stream.endStreaming).not.toHaveBeenCalled()

    const newest = makeHarness(PENDING_STREAM_TASK_ID)
    for (let index = 0; index < 9; index++) {
      newest.api.handlers.onAny('task.failed', {
        task_id: `task-${index}`,
        session_key: SESSION,
        terminal_message: `Task ${index} failed.`,
      })
    }

    newest.api.bindActiveStreamTask('task-8')
    expect(newest.stream.endStreaming).toHaveBeenCalledTimes(1)
    expect(newest.messages.value[newest.messages.value.length - 1]?.text).toBe('Task 8 failed.')
  })

  it("accepts the stopped task's cancelled terminal event after Stop poisoned the active id", () => {
    const { api, options, stream } = makeHarness('__opensquilla_stopped_stream_task__')
    stream.isStreaming.value = false

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'The task was cancelled before it finished.',
    })

    expect(options.applySessionRunState).toHaveBeenCalledWith(expect.objectContaining({
      run_status: 'cancelled',
      last_task: expect.objectContaining({
        task_id: 'task-B',
        status: 'cancelled',
      }),
    }))
  })

  it("accepts the stopped task's terminal sessions.changed payload", () => {
    const { api, options, stream } = makeHarness('__opensquilla_stopped_stream_task__')
    stream.isStreaming.value = false
    const cancelledPayload = {
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'cancelled',
      last_task: { task_id: 'task-B', status: 'cancelled' },
    } satisfies ChatRunStatusSource & { session_key: string; reason: string }
    options.sessionRunStatus = vi.fn((source: ChatRunStatusSource | null | undefined): ChatRunStatus => {
      const isCancelled = source?.run_status === 'cancelled'
      return {
        status: isCancelled ? 'cancelled' : 'idle',
        label: isCancelled ? 'Cancelled' : 'Idle',
        task: source?.last_task ?? null,
      }
    })

    api.handlers.onSessionsChanged(cancelledPayload as never)

    expect(options.applySessionRunState).toHaveBeenCalledWith(
      expect.objectContaining({
        run_status: 'cancelled',
        last_task: expect.objectContaining({
          task_id: 'task-B',
          status: 'cancelled',
        }),
      }),
    )
  })

  it('keeps the stopped-output notice as a local turn result when the next user message is added', () => {
    const { api, options, stream, messages } = makeHarness('__opensquilla_stopped_stream_task__')
    stream.isStreaming.value = true
    messages.value = [
      { role: 'user', text: 'stop immediately', ts: 1_000, messageId: 'user-1' },
    ]
    options.sessionRunStatus = vi.fn((source: ChatRunStatusSource | null | undefined): ChatRunStatus => ({
      status: source?.run_status === 'cancelled' ? 'cancelled' : 'idle',
      label: source?.run_status === 'cancelled' ? 'Stopped after 1s' : 'Idle',
      task: source?.last_task ?? null,
    }))

    api.handlers.onSessionsChanged({
      session_key: SESSION,
      reason: 'task_terminal',
      run_status: 'cancelled',
      last_task: { task_id: 'task-B', status: 'cancelled', finished_at: 2_000 },
    } as never)
    messages.value.push({ role: 'user', text: 'next question', ts: 3_000, messageId: 'user-2' })

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', 'stop immediately'],
      ['assistant', 'Stopped after 1s'],
      ['user', 'next question'],
    ])
    expect(messages.value[1]?.stopNotice).toBe(true)
  })

  it('does not insert a stopped-output notice before a cancelled partial assistant output is finalized', () => {
    const { api, options, stream, messages } = makeHarness('task-B')
    stream.isStreaming.value = true
    messages.value = [
      { role: 'user', text: 'stop after partial output', ts: 1_000, messageId: 'user-1' },
    ]
    stream.endStreaming = vi.fn(() => {
      stream.isStreaming.value = false
      messages.value.push({
        role: 'assistant',
        text: 'partial output',
        ts: 2_000,
        interrupted: true,
        messageId: 'assistant-1',
      })
    })
    options.sessionRunStatus = vi.fn((source: ChatRunStatusSource | null | undefined): ChatRunStatus => ({
      status: source?.run_status === 'cancelled' ? 'cancelled' : 'idle',
      label: source?.run_status === 'cancelled' ? 'Stopped after 1s' : 'Idle',
      task: source?.last_task ?? null,
    }))

    api.handlers.onAny('task.cancelled', {
      task_id: 'task-B',
      session_key: SESSION,
      terminal_message: 'The task was cancelled before it finished.',
    })

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['user', 'stop after partial output'],
      ['assistant', 'partial output'],
    ])
    expect(messages.value.some(message => message.stopNotice)).toBe(false)
  })
})
