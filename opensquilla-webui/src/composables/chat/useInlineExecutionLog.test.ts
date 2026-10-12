// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SESSION_INSPECTION_KEY, SessionInspectionLogNotReadyError, type ExecutionLogPage, type SessionInspection } from '@/modules/sessionInspection'
import { useInlineExecutionLog } from './useInlineExecutionLog'
import { saveByteStream } from '@/utils/streamDownload'

vi.mock('@/utils/streamDownload', () => ({ saveByteStream: vi.fn() }))

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks() })
function page(content = 'saved', patch: Partial<ExecutionLogPage> = {}): ExecutionLogPage {
  return { handle: 'log-one', content, offset: 0, nextOffset: null, chars: Array.from(content).length, complete: true, ...patch }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function mount(read?: SessionInspection['readExecutionLog'], enabled = true) {
  const state = reactive({ sessionKey: 'session-a', handle: 'log-one', toolId: 'tool-one', enabled })
  let result!: ReturnType<typeof useInlineExecutionLog>
  const app = createApp({ setup() {
    result = useInlineExecutionLog({ sessionKey: () => state.sessionKey, handle: () => state.handle,
      toolId: () => state.toolId, enabled: () => state.enabled })
    return () => h('div')
  } })
  if (read) app.provide(SESSION_INSPECTION_KEY, { readExecutionLog: read } as SessionInspection)
  app.mount(document.createElement('div'))
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount() } }
  cleanups.push(unmount)
  return { state, result, unmount }
}

describe('inline retained execution log', () => {
  it('stops retaining pages at the preview limit and never reports the preview as complete', async () => {
    const body = 'x'.repeat(12_000)
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(page(body, { chars: 50_000, nextOffset: body.length }))
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.truncated.value).toBe(true))
    expect(result.content.value?.length).toBe(8192)
    expect(result.complete.value).toBe(false)
    expect(result.hasMore.value).toBe(false)
    expect(await result.ensureComplete()).toBe(false)
    await result.loadMore()
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('streams full log pages to a download without growing the bounded card content', async () => {
    const first = 'a'.repeat(12_000)
    const last = 'END'
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockImplementation(async (_session, _handle, offset) =>
      offset === 0 ? page(first, { chars: 12_003, nextOffset: 12_000 }) : page(last, { offset: 12_000, chars: 12_003 }))
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.truncated.value).toBe(true))
    let saved = ''
    vi.mocked(saveByteStream).mockImplementation(async (_name, source) => {
      const reader = (await source()).getReader()
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        saved += new TextDecoder().decode(value)
      }
      return 'saved'
    })
    expect(await result.download()).toBe('saved')
    expect(saved).toBe(first + last)
    expect(result.content.value).toBe(first.slice(0, 8192))
    expect(read.mock.calls.map(call => call[2])).toEqual([0, 0, 12_000])
  })

  it('reads only the first page on expand, then publishes one Unicode page per demand', async () => {
    const tail = deferred<ExecutionLogPage>()
    const read = vi.fn<SessionInspection['readExecutionLog']>()
      .mockResolvedValueOnce(page('a🙂', { nextOffset: 2, chars: 5 }))
      .mockReturnValueOnce(tail.promise)
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.content.value).toBe('a🙂'))
    expect(read).toHaveBeenCalledTimes(1)
    expect(result.loading.value).toBe(false)
    expect(result.hasMore.value).toBe(true)
    expect(result.complete.value).toBe(false)
    const next = result.loadMore()
    const same = result.loadMore()
    expect(next).toBe(same)
    expect(read).toHaveBeenCalledTimes(2)
    expect(read.mock.calls[1]?.slice(0, 3)).toEqual(['session-a', 'log-one', 2])
    expect(result.loading.value).toBe(true)
    expect(result.content.value).toBe('a🙂')
    expect(result.complete.value).toBe(false)
    tail.resolve(page('end', { offset: 2, chars: 5 }))
    expect(await next).toBe(true)
    expect(result.complete.value).toBe(true)
    expect(result.hasMore.value).toBe(false)
    expect(result.content.value).toBe('a🙂end')
    expect(result.partial.value).toBe(false)
    expect(result.loading.value).toBe(false)
    await result.loadMore()
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('fetches remaining pages only for explicit full-text demand and shares an in-flight read', async () => {
    const head = deferred<ExecutionLogPage>()
    const middle = deferred<ExecutionLogPage>()
    const read = vi.fn<SessionInspection['readExecutionLog']>()
      .mockReturnValueOnce(head.promise)
      .mockReturnValueOnce(middle.promise)
      .mockResolvedValueOnce(page('c', { offset: 2, chars: 3 }))
    const { result } = mount(read)
    const copy = result.ensureComplete()
    expect(read).toHaveBeenCalledTimes(1)
    head.resolve(page('a', { nextOffset: 1, chars: 3 }))
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    const scroll = result.loadMore()
    expect(read).toHaveBeenCalledTimes(2)
    middle.resolve(page('b', { offset: 1, nextOffset: 2, chars: 3 }))
    expect(await scroll).toBe(true)
    expect(await copy).toBe(true)
    expect(read.mock.calls.map(call => call[2])).toEqual([0, 1, 2])
    expect(result.content.value).toBe('abc')
    expect(result.complete.value).toBe(true)
  })

  it('preserves producer-incomplete status and a legitimately empty retained log', async () => {
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValueOnce(page('', { complete: false }))
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.complete.value).toBe(true))
    expect(result.content.value).toBe('')
    expect(result.partial.value).toBe(true)
  })

  it.each(['sessionKey', 'handle', 'toolId'] as const)('aborts on %s change and ignores late pages even if transport ignores abort', async field => {
    const old = deferred<ExecutionLogPage>()
    const fresh = deferred<ExecutionLogPage>()
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise)
    const { state, result } = mount(read)
    const signal = read.mock.calls[0]?.[3]?.signal
    state[field] = `${state[field]}-new`
    await nextTick()
    expect(signal?.aborted).toBe(true)
    old.resolve(page('old'))
    await nextTick()
    expect(result.content.value).toBeNull()
    fresh.resolve(page('new', { handle: state.handle }))
    await vi.waitFor(() => expect(result.complete.value).toBe(true))
    expect(result.content.value).toBe('new')
  })

  it('aborts when details close and does not retain body or continue paging after unmount', async () => {
    const waiting = deferred<ExecutionLogPage>()
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockReturnValueOnce(waiting.promise)
    const { result, unmount } = mount(read)
    const signal = read.mock.calls[0]?.[3]?.signal
    unmount()
    waiting.resolve(page('old', { nextOffset: 3, chars: 8 }))
    await nextTick()
    expect(signal?.aborted).toBe(true)
    expect(read).toHaveBeenCalledTimes(1)
    expect(result.content.value).toBeNull()
    expect(result.loading.value).toBe(false)
  })

  it('settles full-text demand false immediately on close even if transport ignores abort', async () => {
    const waiting = deferred<ExecutionLogPage>()
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockReturnValueOnce(waiting.promise)
    const { result, unmount } = mount(read)
    const copying = result.ensureComplete()
    unmount()
    expect(await copying).toBe(false)
    expect(result.content.value).toBeNull()
    expect(read).toHaveBeenCalledTimes(1)
    waiting.resolve(page('late'))
    await nextTick()
    expect(result.content.value).toBeNull()
  })

  it('does not let full-text demand cross into a new session', async () => {
    const waiting = deferred<ExecutionLogPage>()
    const read = vi.fn<SessionInspection['readExecutionLog']>()
      .mockReturnValueOnce(waiting.promise).mockResolvedValueOnce(page('fresh'))
    const { result, state } = mount(read)
    const copying = result.ensureComplete()
    state.sessionKey = 'session-b'
    await nextTick()
    expect(await copying).toBe(false)
    await vi.waitFor(() => expect(result.content.value).toBe('fresh'))
    waiting.resolve(page('late', { nextOffset: 4, chars: 10 }))
    await nextTick()
    expect(read).toHaveBeenCalledTimes(2)
    expect(result.content.value).toBe('fresh')
  })

  it.each([
    [new SessionInspectionLogNotReadyError(), 'not-ready'],
    [new Error('read failed'), 'failed'],
  ] as const)('keeps preview ownership on read failure and recovers through explicit retry: %s', async (failure, error) => {
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockRejectedValueOnce(failure).mockResolvedValueOnce(page('recovered'))
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.error.value).toBe(error))
    expect(result.content.value).toBeNull()
    expect(result.complete.value).toBe(false)
    await result.retry()
    expect(result.content.value).toBe('recovered')
    expect(result.complete.value).toBe(true)
    expect(result.error.value).toBeNull()
  })

  it.each([
    page('a', { nextOffset: 0, chars: 2 }),
    page('', { nextOffset: 1, chars: 2 }),
    page('a', { offset: 1, chars: 2 }),
    page('a', { handle: 'wrong-handle' }),
  ])('rejects gaps and non-advancing pages without showing a fabricated complete log', async invalid => {
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(invalid)
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.error.value).toBe('failed'))
    expect(read).toHaveBeenCalledTimes(1)
    expect(result.content.value).toBeNull()
    expect(result.complete.value).toBe(false)
  })

  it('rejects a changing snapshot and preserves the already loaded prefix on a later page error', async () => {
    const read = vi.fn<SessionInspection['readExecutionLog']>()
      .mockResolvedValueOnce(page('a', { nextOffset: 1, chars: 2 }))
      .mockResolvedValueOnce(page('bc', { offset: 1, chars: 3 }))
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.content.value).toBe('a'))
    expect(await result.loadMore()).toBe(false)
    expect(result.error.value).toBe('failed')
    expect(result.content.value).toBe('a')
    expect(result.hasMore.value).toBe(true)
    expect(result.complete.value).toBe(false)
    expect(await result.ensureComplete()).toBe(false)
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('returns false for full-text demand on a failed tail without losing loaded text, then permits explicit retry', async () => {
    const read = vi.fn<SessionInspection['readExecutionLog']>()
      .mockResolvedValueOnce(page('a', { nextOffset: 1, chars: 2 }))
      .mockRejectedValueOnce(new Error('tail read failed'))
      .mockResolvedValueOnce(page('ab'))
    const { result } = mount(read)
    await vi.waitFor(() => expect(result.content.value).toBe('a'))
    expect(await result.ensureComplete()).toBe(false)
    expect(result.content.value).toBe('a')
    expect(result.complete.value).toBe(false)
    expect(result.hasMore.value).toBe(true)
    expect(result.error.value).toBe('failed')
    await result.retry()
    expect(result.content.value).toBe('ab')
    expect(result.complete.value).toBe(true)
    expect(result.hasMore.value).toBe(false)
    expect(result.error.value).toBeNull()
  })

  it('does not fetch without a provided inspection service or while disabled', async () => {
    expect(mount().result.available.value).toBe(false)
    const read = vi.fn<SessionInspection['readExecutionLog']>().mockResolvedValue(page())
    const { state, result } = mount(read, false)
    expect(read).not.toHaveBeenCalled()
    state.enabled = true
    await vi.waitFor(() => expect(result.complete.value).toBe(true))
    state.enabled = false
    await nextTick()
    expect(result.content.value).toBeNull()
    expect(result.available.value).toBe(false)
    expect(await result.ensureComplete()).toBe(false)
  })
})
