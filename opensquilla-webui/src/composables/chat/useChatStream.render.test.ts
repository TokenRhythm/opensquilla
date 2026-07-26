import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref } from 'vue'
import {
  DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  streamIdleTimeoutFromPolicy,
  useChatStream,
} from './useChatStream'
import type { ChatMessage } from '@/types/chat'

// Focused coverage for the streaming render coalescer: stream deltas are
// batched onto the frame clock (requestAnimationFrame) and the live reveal
// renders with syntax highlighting deferred. The test env is `node`, so rAF is
// stubbed and driven manually; fake timers cover the Date.now() flush throttle.
function makeStream(
  renderMarkdown = vi.fn((t: string, _o?: { highlight?: boolean }) => `<p>${t}</p>`),
  rpcPolicy?: () => Record<string, unknown> | undefined,
) {
  const scrollToBottom = vi.fn()
  const messages = ref<ChatMessage[]>([])
  const api = useChatStream({
    messages,
    lastHeaderRole: ref(''),
    aborted: ref(false),
    autoScroll: ref(true),
    applySessionRunState: vi.fn(),
    renderMarkdown: renderMarkdown as never,
    stripDirectiveTags: (t: string) => t,
    stripGeneratedArtifactMarkers: (t: string) => t,
    scrollToBottom,
    rpcPolicy,
  })
  return { api, messages, scrollToBottom, renderMarkdown }
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
    expect(renderMarkdown).toHaveBeenCalledWith('abc', { highlight: false })
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

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
    expect(renderMarkdown).toHaveBeenLastCalledWith('ab', { highlight: false })

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
      { type: 'text', raw: prefix },
      { type: 'tool-group', groupId: 'stream:tool-group:web.search:0', operationKey: 'web.search' },
      { type: 'text', raw: suffix },
    ])
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
      { type: 'text', raw: 'Canonical answer' },
    ])
    expect(messages.value[0]?.tool_calls?.[0]).toMatchObject({
      tool_use_id: 'tool-1',
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
    expect(messages.value[0]?.timeline).toEqual([{ type: 'text', raw: 'streamed fallback' }])
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
