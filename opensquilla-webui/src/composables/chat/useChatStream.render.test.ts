import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref, watchEffect } from 'vue'
import {
  DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  streamIdleTimeoutFromPolicy,
  useChatStream,
} from './useChatStream'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'
import type { InterruptViewState } from '@/types/parts'

// Focused coverage for the streaming render coalescer: stream deltas are
// batched onto the frame clock (requestAnimationFrame) and the live reveal
// renders with syntax highlighting deferred. The test env is `node`, so rAF is
// stubbed and driven manually; fake timers cover the Date.now() flush throttle.
function makeStream(
  renderMarkdown = vi.fn((t: string, _o?: { highlight?: boolean }) => `<p>${t}</p>`),
  rpcPolicy?: () => Record<string, unknown> | undefined,
  interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map()),
) {
  const scrollToBottom = vi.fn()
  const messages = ref<ChatMessage[]>([])
  const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
  const applySessionRunState = vi.fn()
  const api = useChatStream({
    messages,
    lastHeaderRole: ref(''),
    aborted: ref(false),
    autoScroll: ref(true),
    runStatus,
    applySessionRunState,
    renderMarkdown: renderMarkdown as never,
    stripDirectiveTags: (t: string) => t,
    stripGeneratedArtifactMarkers: (t: string) => t,
    scrollToBottom,
    rpcPolicy,
    interruptState,
  })
  return {
    api,
    messages,
    runStatus,
    applySessionRunState,
    scrollToBottom,
    renderMarkdown,
  }
}

describe('useChatStream render coalescing', () => {
  let rafCbs: FrameRequestCallback[]
  let rafSeq: number

  beforeEach(() => {
    vi.useFakeTimers()
    rafCbs = []
    rafSeq = 0
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return ++rafSeq })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('uses valid negotiated idle grace and falls back to 630s for invalid policy', () => {
    expect(streamIdleTimeoutFromPolicy({ webui_stream_idle_grace_ms: 1_260_000 })).toBe(1_260_000)
    expect(streamIdleTimeoutFromPolicy(undefined)).toBe(DEFAULT_STREAM_IDLE_TIMEOUT_MS)
    expect(streamIdleTimeoutFromPolicy({ webui_stream_idle_grace_ms: 0 })).toBe(DEFAULT_STREAM_IDLE_TIMEOUT_MS)
    expect(streamIdleTimeoutFromPolicy({ webui_stream_idle_grace_ms: '1260000' })).toBe(DEFAULT_STREAM_IDLE_TIMEOUT_MS)
  })

  it('re-reads policy whenever the hard idle timer is reset', () => {
    let policy = { webui_stream_idle_grace_ms: 1_260_000 }
    const { api } = makeStream(undefined, () => policy)

    api.startStreaming()
    api.resetStreamIdleTimer()
    expect(api.streamIdleTimeoutMs.value).toBe(1_260_000)

    policy = { webui_stream_idle_grace_ms: 900_000 }
    api.resetStreamIdleTimer()
    expect(api.streamIdleTimeoutMs.value).toBe(900_000)
    api.cleanup()
  })

  it('keeps one hard-idle timer across a high-frequency delta burst', () => {
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout')
    const { api } = makeStream()

    api.startStreaming()
    const timersAfterStart = setTimeoutSpy.mock.calls.length
    for (let index = 0; index < 10_000; index += 1) {
      api.resetStreamIdleTimer()
    }

    expect(setTimeoutSpy.mock.calls.length).toBe(timersAfterStart)
    api.cleanup()
  })

  it('extends the single hard-idle deadline from the latest heartbeat', () => {
    const { api } = makeStream(undefined, () => ({ webui_stream_idle_grace_ms: 1_000 }))

    api.startStreaming()
    vi.advanceTimersByTime(750)
    api.resetStreamIdleTimer({ progress: false })
    vi.advanceTimersByTime(750)
    expect(api.isStreaming.value).toBe(true)
    vi.advanceTimersByTime(251)
    expect(api.isStreaming.value).toBe(false)
    api.cleanup()
  })

  it('pauses the hard-idle deadline while the live connection is unavailable', () => {
    const { api } = makeStream()

    api.startStreaming()
    api.resetStreamIdleTimer()
    api.setStreamConnectionAvailable(false)
    vi.advanceTimersByTime(DEFAULT_STREAM_IDLE_TIMEOUT_MS + 1)
    expect(api.isStreaming.value).toBe(true)

    api.setStreamConnectionAvailable(true)
    vi.advanceTimersByTime(DEFAULT_STREAM_IDLE_TIMEOUT_MS + 1)
    expect(api.isStreaming.value).toBe(false)
    api.cleanup()
  })

  it('pauses the hard-idle deadline while the page is hidden', () => {
    const listeners = new Map<string, EventListener>()
    const fakeDocument = {
      hidden: true,
      addEventListener: (name: string, listener: EventListener) => listeners.set(name, listener),
      removeEventListener: (name: string) => listeners.delete(name),
    }
    vi.stubGlobal('document', fakeDocument)
    const { api } = makeStream()

    api.startStreaming()
    api.resetStreamIdleTimer()
    vi.advanceTimersByTime(DEFAULT_STREAM_IDLE_TIMEOUT_MS + 1)
    expect(api.isStreaming.value).toBe(true)

    fakeDocument.hidden = false
    listeners.get('visibilitychange')?.(new Event('visibilitychange'))
    vi.advanceTimersByTime(DEFAULT_STREAM_IDLE_TIMEOUT_MS + 1)
    expect(api.isStreaming.value).toBe(false)
    api.cleanup()
  })

  it('keeps a durable queued task in the queue phase without model narration', () => {
    const { api, runStatus } = makeStream()
    api.startStreaming()
    runStatus.value = {
      status: 'queued',
      label: 'Queued',
      task: { task_id: 'queued-task', status: 'queued' },
    }

    expect(api.streamPhaseLabel.value).toBe('Queued')
    expect(api.streamPhaseElapsed.value).toBe('')
    vi.advanceTimersByTime(15_000)
    expect(api.streamPhaseLabel.value).toBe('Queued')
    expect(api.streamPhaseLabel.value).not.toContain('model')
    api.cleanup()
  })

  it('keeps the turn elapsed clock running across phase transitions', () => {
    const { api } = makeStream()
    api.startStreaming()

    vi.advanceTimersByTime(5_000)
    expect(api.streamTurnElapsed.value).toBe('5s')
    expect(api.streamPhaseElapsed.value).toBe('5s')

    api.setStreamActivity('Waiting for model', 'provider:requesting')
    expect(api.streamTurnElapsed.value).toBe('5s')
    expect(api.streamPhaseElapsed.value).toBe('0s')

    vi.advanceTimersByTime(3_000)
    api.setStreamActivity('Thinking deeply', 'provider:reasoning')
    expect(api.streamTurnElapsed.value).toBe('8s')
    expect(api.streamPhaseElapsed.value).toBe('0s')
    api.cleanup()
  })

  it('restores the turn elapsed clock from a replayed reasoning boundary', () => {
    vi.setSystemTime(20_000)
    const { api } = makeStream()
    api.startStreaming()
    api.appendFrame({
      kind: 'thinking-start',
      blockId: 'replayed-reasoning',
      blockIndex: 0,
      at: 8_000,
      contentKind: 'reasoning',
    })
    api.scheduleRender()
    vi.advanceTimersByTime(50)
    rafCbs[0](0)

    expect(api.streamTurnElapsed.value).toBe('12s')
    api.cleanup()
  })

  it('reconciles the turn clock from authoritative live-task hydration', () => {
    vi.setSystemTime(120_000)
    const { api } = makeStream()
    api.startStreaming()

    expect(api.streamTurnElapsed.value).toBe('0s')
    expect(api.reconcileStreamTaskClock({
      sessionKey: 'agent:main:webchat:a',
      taskId: 'task-live',
      startedAt: '90000',
    })).toBe(true)
    expect(api.streamTurnElapsed.value).toBe('30s')

    vi.advanceTimersByTime(5_000)
    expect(api.streamTurnElapsed.value).toBe('35s')
    api.cleanup()
  })

  it('never moves the same task clock forward and resets for a successor task', () => {
    vi.setSystemTime(120_000)
    const { api } = makeStream()
    api.startStreaming()

    api.reconcileStreamTaskClock({
      sessionKey: 'agent:main:webchat:a',
      taskId: 'task-live',
      startedAt: 90_000,
    })
    api.reconcileStreamTaskClock({
      sessionKey: 'agent:main:webchat:a',
      taskId: 'task-live',
      startedAt: 100_000,
    })
    expect(api.streamTurnElapsed.value).toBe('30s')

    api.reconcileStreamTaskClock({
      sessionKey: 'agent:main:webchat:a',
      taskId: 'task-successor',
      startedAt: 115_000,
    })
    expect(api.streamTurnElapsed.value).toBe('5s')
    api.cleanup()
  })

  it('ignores invalid task timestamps and clears task identity on a live reset', () => {
    vi.setSystemTime(120_000)
    const { api } = makeStream()
    api.startStreaming()

    expect(api.reconcileStreamTaskClock({
      sessionKey: 'agent:main:webchat:a',
      taskId: 'task-live',
      startedAt: 'not-a-timestamp',
    })).toBe(false)
    expect(api.streamTurnElapsed.value).toBe('0s')

    api.reconcileStreamTaskClock({
      sessionKey: 'agent:main:webchat:a',
      taskId: 'task-live',
      startedAt: 90_000,
    })
    api.resetLiveTurnState()
    api.startStreaming()
    expect(api.streamTurnElapsed.value).toBe('0s')
    api.cleanup()
  })

  it('preserves the authoritative active-task steer capability when streaming starts late', () => {
    const { api, runStatus, applySessionRunState } = makeStream()
    runStatus.value = {
      status: 'running',
      label: 'Running',
      task: {
        status: 'running',
        task_id: 'turn-current',
        steer_capability: {
          mode: 'same_turn',
          expected_turn_id: 'turn-current',
          input_kinds: ['text'],
        },
      },
    }

    api.startStreaming()

    expect(applySessionRunState).toHaveBeenLastCalledWith({
      run_status: 'running',
      active_task: expect.objectContaining({
        status: 'running',
        task_id: 'turn-current',
        steer_capability: expect.objectContaining({
          mode: 'same_turn',
          expected_turn_id: 'turn-current',
        }),
      }),
    })
    api.cleanup()
  })

  it('does not carry a completed task capability into a fresh stream', () => {
    const { api, runStatus, applySessionRunState } = makeStream()
    runStatus.value = {
      status: 'idle',
      label: 'Completed',
      task: {
        status: 'succeeded',
        task_id: 'turn-old',
        steer_capability: {
          mode: 'same_turn',
          expected_turn_id: 'turn-old',
          input_kinds: ['text'],
        },
      },
    }

    api.startStreaming()

    expect(applySessionRunState).toHaveBeenLastCalledWith({
      run_status: 'running',
      active_task: { status: 'running' },
    })
    api.cleanup()
  })

  it('coalesces rapid deltas into a single frame flush and defers highlighting', () => {
    const { api, scrollToBottom, renderMarkdown } = makeStream()

    api.appendDelta('a')
    api.appendDelta('b')
    api.appendDelta('c')

    // One frame requested for three deltas; nothing renders until it fires.
    expect(rafCbs.length).toBe(1)
    expect(renderMarkdown).not.toHaveBeenCalled()

    vi.advanceTimersByTime(50) // past MIN_FLUSH_INTERVAL_MS
    rafCbs[0](0)

    // Rendered once over the combined text, with highlighting deferred.
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(renderMarkdown).toHaveBeenCalledWith('abc', {
      highlight: false,
      cache: 'none',
      math: 'defer',
    })
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    api.cleanup()
  })

  it('publishes a large burst once instead of folding every accepted delta', () => {
    const { api, renderMarkdown, scrollToBottom } = makeStream()

    for (let index = 0; index < 2_048; index += 1) api.appendDelta('x')
    expect(rafCbs).toHaveLength(1)
    expect(renderMarkdown).not.toHaveBeenCalled()

    vi.advanceTimersByTime(50)
    rafCbs[0](0)

    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(api.foldedTurn.value.rawText).toHaveLength(2_048)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)
    api.cleanup()
  })

  it('publishes a provider phase even when no text or tool delta follows it', () => {
    const { api } = makeStream()

    api.setStreamActivity('Waiting for model', 'provider:requesting')

    // A status-only upstream wait must reach the non-reactive accumulator's
    // publication clock; otherwise the visible phase remains generic Working.
    expect(rafCbs).toHaveLength(1)
    vi.advanceTimersByTime(50)
    rafCbs[0](0)
    expect(api.foldedTurn.value.statusHistory).toEqual([
      expect.objectContaining({
        action: 'provider:requesting',
        label: 'Waiting for model',
      }),
    ])
    api.cleanup()
  })

  it('persists a status-only turn when a terminal error ends the stream', () => {
    const { api, messages } = makeStream()

    api.startStreaming()
    api.setStreamActivity('Waiting for model', 'provider:requesting')
    vi.advanceTimersByTime(50)
    rafCbs[0](0)
    api.endStreaming()

    expect(messages.value).toEqual([
      expect.objectContaining({
        role: 'assistant',
        text: '',
        statusHistory: expect.arrayContaining([
          expect.objectContaining({ action: 'provider:requesting' }),
        ]),
      }),
    ])
    api.cleanup()
  })

  it('merges durable activity occurrences without dropping repeated retry phases', () => {
    const { api } = makeStream()

    api.startStreaming()
    api.setStreamActivity('Waiting for model', 'provider:requesting')
    vi.advanceTimersByTime(50)
    rafCbs[0](0)
    api.restoreStatusHistory([
      { action: 'provider:requesting', label: 'Waiting for model', at: 1_000 },
      { action: 'provider:retry_wait:0', label: 'Waiting to retry', at: 2_000 },
      { action: 'provider:retrying:0:0', label: 'Retrying', at: 3_000 },
      { action: 'provider:retry_wait:0', label: 'Waiting to retry', at: 4_000 },
    ])

    expect(api.foldedTurn.value.statusHistory.map(entry => entry.action)).toEqual([
      'Sending',
      'provider:requesting',
      'provider:retry_wait:0',
      'provider:retrying:0:0',
      'provider:retry_wait:0',
    ])
    api.cleanup()
  })

  it('does not invalidate the activity surface for same-phase progress deltas', () => {
    const { api } = makeStream()
    let activityRuns = 0
    const stop = watchEffect(() => {
      void api.streamPhaseLabel.value
      void api.streamPhaseElapsed.value
      activityRuns++
    }, { flush: 'sync' })

    api.setStreamActivity('Waiting for model', 'provider:requesting')
    const runsAfterPhaseChange = activityRuns
    for (let index = 0; index < 1_000; index += 1) {
      api.setStreamActivity('Waiting for model', 'provider:requesting')
    }

    expect(activityRuns).toBe(runsAfterPhaseChange)
    stop()
    api.cleanup()
  })

  it('clears a stale activity warning immediately on same-phase progress', () => {
    const { api } = makeStream()

    api.setStreamActivity('Waiting for model', 'provider:requesting')
    vi.advanceTimersByTime(20_001)
    expect(api.streamActivityStale.value).toBe(true)

    api.setStreamActivity('Waiting for model', 'provider:requesting')
    expect(api.streamActivityStale.value).toBe(false)
    api.cleanup()
  })

  it('does not parse the growing answer in the production reducer path', () => {
    const { api, renderMarkdown } = makeStream()
    api.useReducer.value = true

    for (let index = 0; index < 2_048; index += 1) api.appendDelta('x')
    vi.advanceTimersByTime(50)
    rafCbs[0](0)

    expect(renderMarkdown).not.toHaveBeenCalled()
    expect(api.foldedTurn.value.rawText).toHaveLength(2_048)
    expect(api.foldedTurn.value.timelineItems).toEqual([
      expect.objectContaining({
        type: 'text',
        html: '',
        rawText: 'x'.repeat(2_048),
        presentation: 'answer',
      }),
    ])
    api.cleanup()
  })

  it('re-arms a fresh frame after a flush', () => {
    const { api, renderMarkdown } = makeStream()

    api.appendDelta('a')
    expect(rafCbs.length).toBe(1)
    vi.advanceTimersByTime(50)
    rafCbs[0](0)
    expect(renderMarkdown).toHaveBeenCalledTimes(1)

    api.appendDelta('b')
    expect(rafCbs.length).toBe(2) // a new frame is scheduled after the prior flush
    vi.advanceTimersByTime(50)
    rafCbs[1](0)
    expect(renderMarkdown).toHaveBeenCalledTimes(2)
    expect(renderMarkdown).toHaveBeenLastCalledWith('ab', {
      highlight: false,
      cache: 'none',
      math: 'defer',
    })

    api.cleanup()
  })

  it('does not render a stale frame after cleanup', () => {
    const { api, renderMarkdown } = makeStream()

    api.appendDelta('a')
    expect(rafCbs.length).toBe(1)
    api.cleanup()

    vi.advanceTimersByTime(50)
    rafCbs[0](0) // firing the cancelled frame must not render

    expect(renderMarkdown).not.toHaveBeenCalled()
  })

  it('renders cumulative post-tool text snapshots as only the new suffix', () => {
    const { api, messages } = makeStream()
    const prefix = 'prefix'
    const suffix = 'suffix'

    api.appendDelta(prefix)
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({ tool_use_id: 'tool-1', tool_name: 'web_search', result: 'ok' })
    api.appendDelta(prefix + suffix)

    expect(api.foldedTurn.value.rawText).toBe(prefix + suffix)

    api.endStreaming()

    expect(messages.value[0]?.text).toBe(prefix + suffix)
    expect(messages.value[0]?.timeline).toEqual([
      { type: 'text', raw: prefix, presentation: 'answer' },
      { type: 'tool-group', groupId: 'stream:tool-group:web.search:0', operationKey: 'web.search' },
      { type: 'text', raw: suffix, presentation: 'answer' },
    ])
    api.cleanup()
  })

  it('retains structured reasoning blocks on the completed assistant row', () => {
    vi.setSystemTime(5_000)
    const { api, messages } = makeStream()
    api.startStreaming()
    api.appendFrame({
      kind: 'thinking-start',
      blockId: 'reasoning-1',
      blockIndex: 0,
      at: 1_000,
      contentKind: 'reasoning',
    })
    api.appendFrame({
      kind: 'thinking',
      blockId: 'reasoning-1',
      blockIndex: 0,
      text: 'inspect',
      at: 2_000,
    })
    api.appendFrame({
      kind: 'thinking-end',
      blockId: 'reasoning-1',
      blockIndex: 0,
      status: 'completed',
      at: 3_000,
    })
    api.appendDelta('answer')
    vi.advanceTimersByTime(50)
    rafCbs.shift()?.(0)

    api.endStreaming()

    expect(messages.value[0]?.reasoningBlocks).toEqual([expect.objectContaining({
      id: 'reasoning-1',
      text: 'inspect',
      status: 'completed',
      startedAt: 1_000,
      endedAt: 3_000,
    })])
    api.cleanup()
  })

  it('keeps an interrupted reasoning-only turn instead of dropping it as empty', () => {
    vi.setSystemTime(5_000)
    const { api, messages } = makeStream()
    api.startStreaming()
    api.appendFrame({
      kind: 'thinking-start',
      blockId: 'reasoning-only',
      blockIndex: 0,
      at: 1_000,
      contentKind: 'reasoning',
    })
    api.appendFrame({
      kind: 'thinking',
      blockId: 'reasoning-only',
      blockIndex: 0,
      text: 'partial thought',
      at: 2_000,
    })
    api.scheduleRender()
    vi.advanceTimersByTime(50)
    rafCbs.shift()?.(0)

    api.endStreaming({ reason: 'aborted' })

    expect(messages.value).toHaveLength(1)
    expect(messages.value[0]).toMatchObject({
      role: 'assistant',
      text: '',
      interrupted: true,
      reasoningBlocks: [{
        id: 'reasoning-only',
        text: 'partial thought',
        status: 'interrupted',
      }],
    })
    api.cleanup()
  })

  it('hands an unfinished coarse reveal to the settled assistant row', () => {
    const { api, messages } = makeStream()
    const coarse = 'reasoning '.repeat(80)
    api.startStreaming()
    api.noteReasoningPresentationDelta(coarse)
    api.appendFrame({
      kind: 'thinking-start',
      blockId: 'coarse-reasoning',
      blockIndex: 0,
      at: 1_000,
      contentKind: 'reasoning',
    })
    api.appendFrame({
      kind: 'thinking',
      blockId: 'coarse-reasoning',
      blockIndex: 0,
      text: coarse,
      at: 1_000,
    })
    api.appendFrame({
      kind: 'thinking-end',
      blockId: 'coarse-reasoning',
      blockIndex: 0,
      status: 'completed',
      at: 2_000,
    })
    api.appendDelta('answer')
    api.endStreaming()

    expect(messages.value[0]?.reasoningPresentationPending).toBe(true)
    expect(messages.value[0]?.reasoningBlocks?.[0]?.text).toBe(coarse)
    api.cleanup()
  })

  it('replaces one answer generation while preserving completed tools and artifacts', () => {
    const { api, messages } = makeStream()

    api.startStreaming()
    api.setAssistantMessageId('assistant-1')
    api.appendDelta('partial old')
    api.appendFrame({ kind: 'thinking', text: 'old reasoning', at: 1 })
    api.appendToolCall({ tool_use_id: 'tool-completed', tool_name: 'web_search' })
    api.appendToolResult({
      tool_use_id: 'tool-completed',
      tool_name: 'web_search',
      result: 'completed result',
    })
    api.appendToolCall({ tool_use_id: 'tool-pending', tool_name: 'exec_command' })
    api.appendArtifact({ id: 'artifact-1', name: 'kept.txt' })

    api.resetAnswerGeneration({
      textSnapshot: '',
      preserveCompletedTools: true,
    })

    expect(api.isStreaming.value).toBe(true)
    expect(api.foldedTurn.value.rawText).toBe('')
    expect(api.foldedTurn.value.thinkingText).toBe('')
    expect(api.foldedTurn.value.toolCalls).toEqual([
      expect.objectContaining({
        toolId: 'tool-completed',
        isRunning: false,
        result: 'completed result',
      }),
    ])
    expect(api.foldedTurn.value.toolCalls.some(call => call.toolId === 'tool-pending')).toBe(false)
    expect(api.foldedTurn.value.artifacts).toEqual([
      expect.objectContaining({ id: 'artifact-1' }),
    ])

    api.appendDelta('fixed text')
    api.reconcileFinalText('fixed text')
    api.endStreaming()

    expect(messages.value).toHaveLength(1)
    expect(messages.value[0]).toMatchObject({
      role: 'assistant',
      messageId: 'assistant-1',
      text: 'fixed text',
      artifacts: [expect.objectContaining({ id: 'artifact-1' })],
      tool_calls: [expect.objectContaining({
        tool_use_id: 'tool-completed',
        result: 'completed result',
      })],
    })
    expect(messages.value[0]?.tool_calls?.some(call => call.tool_use_id === 'tool-pending')).toBe(false)
    api.cleanup()
  })

  it('keeps additive post-tool text deltas unchanged', () => {
    const { api, messages } = makeStream()

    api.appendDelta('prefix')
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({ tool_use_id: 'tool-1', tool_name: 'web_search', result: 'ok' })
    api.appendDelta('suffix')

    expect(api.foldedTurn.value.rawText).toBe('prefixsuffix')

    api.endStreaming()

    expect(messages.value[0]?.text).toBe('prefixsuffix')
    api.cleanup()
  })

  it('clears suppressed answer text while preserving tools and artifacts', () => {
    const { api, messages } = makeStream()

    api.appendDelta('stale streamed answer')
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({
      tool_use_id: 'tool-1',
      tool_name: 'web_search',
      result: 'found',
    })
    api.appendArtifact({ id: 'artifact-1', name: 'result.txt', mime: 'text/plain' })

    api.endStreaming({ suppressed: true })

    expect(messages.value).toHaveLength(1)
    expect(messages.value[0]).toMatchObject({
      role: 'assistant',
      text: '',
      artifacts: [{ id: 'artifact-1', name: 'result.txt', mime: 'text/plain' }],
    })
    expect(messages.value[0]?.tool_calls).toHaveLength(1)
    expect(messages.value[0]?.timeline?.some(segment => segment.type === 'text')).toBe(false)
    expect(messages.value[0]?.timeline?.some(segment => segment.type === 'tool-group')).toBe(true)
    api.cleanup()
  })

  it('drops a suppressed text-only bubble without losing the legacy exact fallback', () => {
    const suppressed = makeStream()
    suppressed.api.appendDelta('stale streamed answer')
    suppressed.api.endStreaming({ suppressed: true })
    expect(suppressed.messages.value).toEqual([])
    suppressed.api.cleanup()

    const legacy = makeStream()
    legacy.api.appendDelta('\nNO_REPLY\nHEARTBEAT_OK\n')
    legacy.api.endStreaming()
    expect(legacy.messages.value).toEqual([])
    legacy.api.cleanup()
  })

  it('keeps legacy sentinel-turn tools while removing the marker text', () => {
    const { api, messages } = makeStream()

    api.appendDelta('NO_REPLY')
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({
      tool_use_id: 'tool-1',
      tool_name: 'web_search',
      result: 'found',
    })
    api.endStreaming()

    expect(messages.value).toHaveLength(1)
    expect(messages.value[0]?.text).toBe('')
    expect(messages.value[0]?.tool_calls).toHaveLength(1)
    expect(messages.value[0]?.timeline?.some(segment => segment.type === 'text')).toBe(false)
    api.cleanup()
  })

  it('keeps intermediate and answer text in separate live segments', () => {
    const { api, messages } = makeStream()

    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({ tool_use_id: 'tool-1', tool_name: 'web_search', result: 'ok' })
    api.appendDelta('Checking.', 'intermediate')
    api.appendDelta('Verified answer.', 'answer')

    expect(api.streamTimelineItems.value).toEqual([
      expect.objectContaining({ type: 'tool-group' }),
      expect.objectContaining({ type: 'text', rawText: 'Checking.', presentation: 'intermediate' }),
      expect.objectContaining({ type: 'text', rawText: 'Verified answer.', presentation: 'answer' }),
    ])
    expect(api.foldedTurn.value.timelineItems.map(item => ({
      type: item.type,
      presentation: item.type === 'text' ? item.presentation : undefined,
      rawText: item.type === 'text' ? item.rawText : undefined,
    }))).toEqual(api.streamTimelineItems.value.map(item => ({
      type: item.type,
      presentation: item.type === 'text' ? item.presentation : undefined,
      rawText: item.type === 'text' ? item.rawText : undefined,
    })))

    api.endStreaming()

    expect(messages.value[0]?.timeline).toEqual([
      expect.objectContaining({ type: 'tool-group' }),
      { type: 'text', raw: 'Checking.', presentation: 'intermediate' },
      { type: 'text', raw: 'Verified answer.', presentation: 'answer' },
    ])
    api.cleanup()
  })

  it('records compaction outcomes with terminal maintenance states', () => {
    const { api } = makeStream()

    for (const [status, id] of [
      ['completed', 'cmp-completed'],
      ['skipped', 'cmp-skipped'],
      ['stale', 'cmp-stale'],
      ['cancelled', 'cmp-cancelled'],
      ['failed', 'cmp-failed'],
    ] as const) {
      api.recordCompactionActivity({ status, compaction_id: id, source: 'automatic' })
    }

    expect(api.foldedTurn.value.statusHistory.map(entry => [entry.id, entry.state])).toEqual([
      ['cmp-completed', 'completed'],
      ['cmp-skipped', 'skipped'],
      ['cmp-stale', 'stale'],
      ['cmp-cancelled', 'cancelled'],
      ['cmp-failed', 'failed'],
    ])
    api.cleanup()
  })

  it('checkpoints visible output before a same-turn steer without duplicating final text', () => {
    const { api, messages } = makeStream()

    api.appendDelta('before')
    api.checkpointForUserMessage('turn-steered')
    messages.value.push({
      role: 'user',
      text: 'adjust',
      ts: new Date().toISOString(),
      turnId: 'turn-steered',
      inputDisposition: 'steering',
    })
    api.appendDelta('after')
    api.reconcileFinalText('beforeafter')
    api.endStreaming()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['assistant', 'before'],
      ['user', 'adjust'],
      ['assistant', 'after'],
    ])
    expect(messages.value.every(message => message.turnId === 'turn-steered')).toBe(true)
    api.cleanup()
  })

  it('keeps the live activity timeline visible across a same-turn steer checkpoint', () => {
    const { api, messages } = makeStream()

    api.appendDelta('before')
    api.appendToolCall({
      tool_use_id: 'tool-running',
      tool_name: 'web_search',
      input: { query: 'before steer' },
    })
    const phaseBeforeSteer = api.streamPhaseLabel.value

    api.checkpointForUserMessage('turn-steered')

    expect(messages.value).toHaveLength(1)
    expect(messages.value[0]).toMatchObject({
      role: 'assistant',
      text: 'before',
      turnId: 'turn-steered',
      timeline: [{ type: 'text', raw: 'before' }],
    })
    expect(messages.value[0]?.tool_calls).toBeUndefined()
    expect(messages.value[0]?.statusHistory).toBeUndefined()
    expect(api.streamHasVisibleOutput.value).toBe(true)
    expect(api.streamPhaseLabel.value).toBe(phaseBeforeSteer)
    expect(api.foldedTurn.value.rawText).toBe('')
    expect(api.foldedTurn.value.toolCalls).toEqual([
      expect.objectContaining({
        toolId: 'tool-running',
        name: 'web_search',
        isRunning: true,
      }),
    ])
    expect(api.foldedTurn.value.statusHistory.length).toBeGreaterThan(0)
    expect(api.streamTimelineItems.value).toEqual([
      expect.objectContaining({ type: 'tool-group' }),
    ])

    api.appendToolResult({
      tool_use_id: 'tool-running',
      tool_name: 'web_search',
      result: 'ok',
    })
    api.appendDelta('after')
    api.reconcileFinalText('beforeafter')
    api.endStreaming()

    expect(messages.value.map(message => [message.role, message.text])).toEqual([
      ['assistant', 'before'],
      ['assistant', 'after'],
    ])
    expect(messages.value[1]?.tool_calls).toEqual([
      expect.objectContaining({
        tool_use_id: 'tool-running',
        result: 'ok',
      }),
    ])
    api.cleanup()
  })

  it('does not create an empty assistant row when a steer lands during a tool-only segment', () => {
    const { api, messages } = makeStream()

    api.appendToolCall({
      tool_use_id: 'tool-only',
      tool_name: 'exec_command',
      input: { cmd: 'sleep 30' },
    })

    api.checkpointForUserMessage('turn-tool-only')

    expect(messages.value).toEqual([])
    expect(api.streamHasVisibleOutput.value).toBe(true)
    expect(api.foldedTurn.value.toolCalls).toEqual([
      expect.objectContaining({
        toolId: 'tool-only',
        isRunning: true,
      }),
    ])
    api.cleanup()
  })

  it('commits resolved approvals into the finished assistant timeline', () => {
    const interruptState = ref<ReadonlyMap<string, InterruptViewState>>(new Map([
      ['approval-1', { resolution: null, busy: false, error: '' }],
    ]))
    const { api, messages } = makeStream(undefined, undefined, interruptState)

    api.startStreaming()
    api.appendDelta('before')
    api.appendInterruptFrame({
      interruptKind: 'approval',
      approvalId: 'approval-1',
      data: {
        approvalId: 'approval-1',
        namespace: 'exec',
        toolName: 'sandbox elevation',
        command: 'python -c pass',
        approvalKind: 'sandbox_elevation',
        args: null,
        warning: '',
        agent: 'main',
        sessionKey: 'agent:main:web',
        deadline: 0,
      },
      at: 1000,
    })
    interruptState.value = new Map([
      ['approval-1', { resolution: 'approved', busy: false, error: '' }],
    ])
    api.appendDelta('after')
    api.endStreaming()

    expect(messages.value[0]?.timeline?.map(segment => segment.type)).toEqual([
      'text',
      'interrupt',
      'text',
    ])
    expect((messages.value[0] as any)?.interrupts).toMatchObject([
      { interruptKind: 'approval', resolution: 'approved' },
    ])
    api.cleanup()
  })

  it('keeps cumulative-looking text before a tool boundary unchanged', () => {
    const { api, messages } = makeStream()

    api.appendDelta('prefix')
    api.appendDelta('prefixsuffix')

    expect(api.foldedTurn.value.rawText).toBe('prefixprefixsuffix')

    api.endStreaming()

    expect(messages.value[0]?.text).toBe('prefixprefixsuffix')
    api.cleanup()
  })

  it('commits a conflicting terminal snapshot into the tool timeline', () => {
    const { api, messages } = makeStream()

    api.appendDelta('stale preface')
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({ tool_use_id: 'tool-1', tool_name: 'web_search', result: 'ok' })
    api.appendDelta('stale retry')
    api.reconcileFinalText('Canonical answer')

    expect(api.foldedTurn.value.rawText).toBe('Canonical answer')
    expect(api.foldedTurn.value.timelineItems.map(item => item.type)).toEqual(['tool-group', 'text'])

    api.endStreaming()

    expect(messages.value[0]?.text).toBe('Canonical answer')
    expect(messages.value[0]?.timeline).toEqual([
      { type: 'tool-group', groupId: 'stream:tool-group:web.search:0', operationKey: 'web.search' },
      { type: 'text', raw: 'Canonical answer', presentation: 'answer' },
    ])
    expect(messages.value[0]?.tool_calls?.[0]).toMatchObject({
      tool_use_id: 'tool-1',
      result: 'ok',
    })
    api.cleanup()
  })

  it('keeps production text solely in the accumulator across reconcile and steer', () => {
    const { api, messages } = makeStream()
    api.useReducer.value = true

    api.appendDelta('before')
    expect(api.streamTimelineItems.value).toEqual([])
    expect(api.foldedTurn.value.rawText).toBe('before')
    api.checkpointForUserMessage('turn-production-steer')
    expect(messages.value[0]).toMatchObject({ role: 'assistant', text: 'before' })
    expect(api.foldedTurn.value.rawText).toBe('')

    api.appendDelta('stale')
    api.reconcileFinalText('canonical')
    expect(api.streamTimelineItems.value).toEqual([])
    expect(api.foldedTurn.value.rawText).toBe('canonical')
    api.endStreaming()
    expect(messages.value[1]).toMatchObject({ role: 'assistant', text: 'canonical' })
    api.cleanup()
  })

  it('commits the complete production tool input from the accumulator', () => {
    const { api, messages } = makeStream()
    api.useReducer.value = true

    api.appendToolCall({ tool_use_id: 'tool-long', tool_name: 'web_search' })
    for (let index = 0; index < 1_000; index += 1) {
      api.appendToolDelta({
        tool_use_id: 'tool-long',
        tool_name: 'web_search',
        fragment: 'x',
      })
    }
    const liveTool = api.foldedTurn.value.toolCalls[0]
    expect(liveTool).toBeDefined()
    expect(String(liveTool!.inputRaw || '').length).toBeLessThan(1_000)
    expect(liveTool!.inputPreview).toHaveLength(200)

    api.appendToolResult({
      tool_use_id: 'tool-long',
      tool_name: 'web_search',
      result: 'ok',
    })
    expect(api.foldedTurn.value.toolCalls[0]?.inputRaw).toHaveLength(1_000)
    api.endStreaming()

    expect(messages.value[0]?.tool_calls?.[0]).toMatchObject({
      tool_use_id: 'tool-long',
      input: 'x'.repeat(1_000),
      result: 'ok',
    })
    api.cleanup()
  })

  it('clears stale text on an authoritative empty snapshot but keeps tools', () => {
    const { api, messages } = makeStream()

    api.appendDelta('stale text')
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search' })
    api.appendToolResult({ tool_use_id: 'tool-1', tool_name: 'web_search', result: 'ok' })
    api.reconcileFinalText('')

    expect(api.foldedTurn.value.rawText).toBe('')
    expect(api.foldedTurn.value.timelineItems.map(item => item.type)).toEqual(['tool-group'])

    api.endStreaming()

    expect(messages.value[0]?.text).toBe('')
    expect(messages.value[0]?.timeline).toEqual([
      { type: 'tool-group', groupId: 'stream:tool-group:web.search:0', operationKey: 'web.search' },
    ])
    expect(messages.value[0]?.tool_calls?.[0]).toMatchObject({ tool_use_id: 'tool-1' })
    api.cleanup()
  })

  it('keeps streamed text when the terminal event has no snapshot', () => {
    const { api, messages } = makeStream()

    api.appendDelta('streamed fallback')
    api.reconcileFinalText(null)
    api.endStreaming()

    expect(messages.value[0]?.text).toBe('streamed fallback')
    expect(messages.value[0]?.timeline).toEqual([
      { type: 'text', raw: 'streamed fallback', presentation: 'answer' },
    ])
    api.cleanup()
  })

  it.each([
    'Document the literal `<tool_calls>` marker and keep this suffix.',
    '```xml\n<tool_calls><invoke name="demo"></invoke></tool_calls>\n```\nAfter the fence.',
    'Keep `<｜DSML｜tool_calls><｜DSML｜invoke name="demo">` and continue.',
    '<details><summary>View areas around line 10</summary>Visible note.</details>\n\nAfter details.',
  ])('commits canonical protocol-shaped text without destructive filtering: %s', (text) => {
    const { api, messages } = makeStream()
    const split = Math.max(1, Math.floor(text.length / 2))

    api.appendDelta(text.slice(0, split))
    api.appendDelta(text.slice(split))
    api.reconcileFinalText(text)
    api.endStreaming()

    expect(messages.value[0]?.text).toBe(text)
    api.cleanup()
  })

  // Issue #329: a running tool's elapsed timer must come from the server-stamped
  // start time so it survives a page switch / stream replay (where the component
  // remounts and replays tool_use_start) instead of restarting from the local clock.
  it('seeds a running tool elapsed timer from the server start time', () => {
    const { api } = makeStream()
    vi.setSystemTime(100_000)

    // Server says the tool started 5s before "now".
    api.appendToolCall({ tool_use_id: 'tool-1', tool_name: 'web_search', started_at: 95_000 })

    expect(api.streamToolElapsedText({ toolId: 'tool-1' })).toBe('5s')
    api.cleanup()
  })

  it('falls back to the local clock when the server start time is absent or sentinel', () => {
    const { api } = makeStream()
    vi.setSystemTime(100_000)

    // No started_at, and the 0 "unstamped" sentinel: both fall back to now -> 0s.
    api.appendToolCall({ tool_use_id: 'tool-2', tool_name: 'web_search' })
    api.appendToolCall({ tool_use_id: 'tool-3', tool_name: 'web_search', started_at: 0 })

    expect(api.streamToolElapsedText({ toolId: 'tool-2' })).toBe('0s')
    expect(api.streamToolElapsedText({ toolId: 'tool-3' })).toBe('0s')
    api.cleanup()
  })

  // Clock-skew guard: a server start in the future or implausibly far in the past
  // (skewed gateway clock) is distrusted and falls back to the local clock, so the
  // timer can't render a wildly wrong duration. "now" is set well past
  // MAX_TRUSTED_TOOL_AGE_MS so the stale branch is exercised.
  it('ignores a skewed server start time and falls back to the local clock', () => {
    const { api } = makeStream()
    vi.setSystemTime(5_000_000)

    // Future start (server clock ahead) -> distrusted -> local clock -> 0s.
    api.appendToolCall({ tool_use_id: 'tool-4', tool_name: 'web_search', started_at: 5_100_000 })
    // Implausibly old start (server far behind / garbage) -> distrusted -> 0s.
    api.appendToolCall({ tool_use_id: 'tool-5', tool_name: 'web_search', started_at: 1_000 })

    expect(api.streamToolElapsedText({ toolId: 'tool-4' })).toBe('0s')
    expect(api.streamToolElapsedText({ toolId: 'tool-5' })).toBe('0s')
    api.cleanup()
  })
})
