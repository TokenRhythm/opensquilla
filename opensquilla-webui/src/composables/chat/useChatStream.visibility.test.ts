import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref, watchSyncEffect, type Ref } from 'vue'
import { useChatStream } from './useChatStream'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'

describe('stream rendering across visibility changes', () => {
  let hidden: boolean
  let frames: Map<number, FrameRequestCallback>
  let frameId: number
  let listeners: Map<string, EventListener>
  let stream: ReturnType<typeof useChatStream> | undefined
  let stopWatching: (() => void) | undefined

  beforeEach(() => {
    vi.useFakeTimers()
    hidden = false
    frames = new Map()
    frameId = 0
    listeners = new Map()
    vi.stubGlobal('document', {
      get hidden() { return hidden },
      addEventListener: (name: string, callback: EventListener) => listeners.set(name, callback),
      removeEventListener: (name: string) => listeners.delete(name),
    })
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.set(++frameId, callback)
      return frameId
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id))
  })

  afterEach(() => {
    stream?.cleanup()
    stopWatching?.()
    stream = undefined
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function visibility(value: boolean) {
    hidden = value
    listeners.get('visibilitychange')?.(new Event('visibilitychange'))
  }

  function runFrame() {
    const pending = [...frames.values()]
    frames.clear()
    for (const callback of pending) callback(performance.now())
  }

  function createStream(presentationVisible?: Ref<boolean>) {
    const messages = ref<ChatMessage[]>([])
    const scroll = vi.fn()
    stream = useChatStream({
      presentationVisible,
      messages, lastHeaderRole: ref(''), aborted: ref(false), autoScroll: ref(true),
      runStatus: ref<ChatRunStatus>({ status: 'idle', label: '', task: null }),
      applySessionRunState: vi.fn(), renderMarkdown: (text: string) => text,
      stripDirectiveTags: (text: string) => text,
      stripGeneratedArtifactMarkers: (text: string) => text, scrollToBottom: scroll,
    })
    const api = stream
    let published = ''
    // Model an active UI consumer. A first lazy computed read after deltas
    // would see the mutable log even if no render was actually published.
    stopWatching = watchSyncEffect(() => { published = api.foldedTurn.value.rawText })
    return { api, messages, scroll, published: () => published }
  }

  it('publishes dirty text within the hidden fallback after cancelling a queued frame', () => {
    const { api, published } = createStream()
    api.appendDelta('a')
    expect(frames.size).toBe(1)
    visibility(true)
    expect(frames.size).toBe(0)
    api.appendDelta('b')
    vi.advanceTimersByTime(249)
    expect(published()).toBe('')
    vi.advanceTimersByTime(1)
    expect(published()).toBe('ab')
  })

  it('schedules existing dirty text on hiding even without another incoming delta', () => {
    const { api, published } = createStream()
    api.appendDelta('a')
    visibility(true)
    vi.advanceTimersByTime(250)
    expect(published()).toBe('a')
  })

  it('moves a pending hidden timer to a visible frame without a duplicate flush', () => {
    hidden = true
    const { api, published, scroll } = createStream()
    api.appendDelta('a')
    vi.advanceTimersByTime(50)
    visibility(false)
    expect(frames.size).toBe(1)
    runFrame()
    expect(published()).toBe('a')
    const callsAfterFrame = scroll.mock.calls.length
    vi.advanceTimersByTime(250)
    expect(scroll).toHaveBeenCalledTimes(callsAfterFrame)
  })

  it('retains the latest text through rapid visibility transitions', () => {
    const { api, published } = createStream()
    api.appendDelta('a')
    visibility(true)
    api.appendDelta('b')
    visibility(false)
    api.appendDelta('c')
    visibility(true)
    vi.advanceTimersByTime(250)
    expect(published()).toBe('abc')
    expect(frames.size).toBe(0)
  })

  it('commits terminal text before the hidden fallback and cancels pending work', () => {
    const { api, messages, scroll } = createStream()
    api.appendDelta('a')
    visibility(true)
    api.appendDelta('b')
    api.endStreaming()
    expect(messages.value.some(message => message.role === 'assistant' && message.text === 'ab')).toBe(true)
    const callsAfterEnd = scroll.mock.calls.length
    vi.advanceTimersByTime(1_000)
    expect(scroll).toHaveBeenCalledTimes(callsAfterEnd)
    expect(frames.size).toBe(0)
  })

  it('does not publish or reschedule after cleanup', () => {
    const { api, published } = createStream()
    api.appendDelta('a')
    visibility(true)
    api.cleanup()
    vi.advanceTimersByTime(1_000)
    visibility(false)
    expect(published()).toBe('')
    expect(frames.size).toBe(0)
    expect(listeners.has('visibilitychange')).toBe(false)
  })

  it('parks hidden panel clocks and scrolling without stopping its stream owner', () => {
    const visible = ref(true)
    const { api, messages, scroll, published } = createStream(visible)
    api.appendDelta('before ')
    runFrame()
    const runningTimers = vi.getTimerCount()
    visible.value = false
    expect(vi.getTimerCount()).toBe(runningTimers - 1)
    const elapsed = api.streamTurnElapsed.value
    scroll.mockClear()
    vi.advanceTimersByTime(5_000)
    expect(api.streamTurnElapsed.value).toBe(elapsed)
    api.appendDelta('while hidden')
    expect(frames.size).toBe(0)
    expect(published()).toBe('before ')
    vi.advanceTimersByTime(250)
    expect(published()).toBe('before while hidden')
    expect(api.isStreaming.value).toBe(true)
    expect(scroll).not.toHaveBeenCalled()
    visible.value = true
    expect(api.streamTurnElapsed.value).not.toBe(elapsed)
    expect(scroll).toHaveBeenCalledTimes(1)
    visible.value = false
    api.endStreaming()
    expect(messages.value.some(message => message.text === 'before while hidden')).toBe(true)
    expect(scroll).toHaveBeenCalledTimes(1)
    expect(api.isStreaming.value).toBe(false)
  })

  it('does not start a hidden panel thinking clock and catches up on return', () => {
    const visible = ref(false)
    const { api } = createStream(visible)
    api.showThinkingIndicator()
    vi.advanceTimersByTime(400)
    expect(api.thinkingVisible.value).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(4_000)
    visible.value = true
    expect(api.thinkingText.value).toContain('4s')
    expect(vi.getTimerCount()).toBe(1)
    visible.value = false
    expect(vi.getTimerCount()).toBe(0)
    vi.advanceTimersByTime(60_000)
    visible.value = true
    expect(api.thinkingVisible.value).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })
})
