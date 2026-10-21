// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactContentAccess, type ArtifactWorkbench, type AttachmentTextPreview } from '@/modules/artifactWorkbench'
import type { ToolFileSnapshot } from '@/types/chat'
import { readDisplayAttachmentPreview } from '@/adapters/gateway/attachmentAccessV4'
import { createPrivateHttpTransport } from '@/adapters/gateway/privateHttpTransport'
import { useInlineFileSnapshot } from './useInlineFileSnapshot'

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.useRealTimers() })
function result(body: string): AttachmentTextPreview {
  return { content: body, truncated: false, totalBytes: new TextEncoder().encode(body).length }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { resolve, promise }
}
function mount(readAttachmentPreview: NonNullable<ArtifactContentAccess['readAttachmentPreview']>, enabled = true) {
  const state = reactive({ sessionKey: 'session-a', toolId: 'tool-one', enabled,
    snapshot: { kind: 'write', path: 'a.txt', content: 'preview', sha256_ref: 'a'.repeat(64), bytes: 8 } as ToolFileSnapshot })
  let snapshot!: ReturnType<typeof useInlineFileSnapshot>
  const app = createApp({ setup() {
    snapshot = useInlineFileSnapshot({ sessionKey: () => state.sessionKey, toolId: () => state.toolId,
      snapshot: () => state.snapshot, enabled: () => state.enabled })
    return () => h('div')
  } })
  app.provide(ARTIFACT_WORKBENCH_KEY, { content: { readAttachmentPreview } } as ArtifactWorkbench)
  app.mount(document.createElement('div'))
  let mounted = true
  const unmount = () => { if (mounted) { mounted = false; app.unmount() } }
  cleanups.push(unmount)
  return { state, snapshot, unmount }
}

describe('retained file snapshot reads', () => {
  it('does not read until enabled, then publishes only the complete Unicode body', async () => {
    const response = deferred<AttachmentTextPreview>()
    const read = vi.fn().mockReturnValue(response.promise)
    const { state, snapshot } = mount(read, false)
    expect(read).not.toHaveBeenCalled()
    state.enabled = true
    await nextTick()
    expect(snapshot.loading.value).toBe(true)
    expect(snapshot.content.value).toBeNull()
    expect(read).toHaveBeenCalledWith(expect.objectContaining({ sha256_ref: 'a'.repeat(64) }),
      expect.objectContaining({ sessionKey: 'session-a', signal: expect.any(AbortSignal) }))
    const complete = snapshot.ensureComplete()
    response.resolve(result('中🙂x'))
    expect(await complete).toBe(true)
    expect(snapshot.content.value).toBe('中🙂x')
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('aborts on close and settles callers even when the transport ignores abort', async () => {
    const response = deferred<AttachmentTextPreview>()
    const read = vi.fn().mockReturnValue(response.promise)
    const { snapshot, unmount } = mount(read)
    await vi.waitFor(() => expect(read).toHaveBeenCalled())
    const complete = snapshot.ensureComplete()
    const signal = read.mock.calls[0]![1].signal as AbortSignal
    unmount()
    expect(signal.aborted).toBe(true)
    expect(await complete).toBe(false)
    response.resolve(result('old body'))
    await nextTick()
    expect(snapshot.content.value).toBeNull()
  })

  it('preserves an explicit BOM and CRLF exactly', async () => {
    const body = '\uFEFFa\r\nbc'
    const read = vi.fn().mockResolvedValue(result(body))
    const { snapshot } = mount(read)
    await vi.waitFor(() => expect(snapshot.content.value).toBe(body))
    expect(snapshot.content.value?.charCodeAt(0)).toBe(0xfeff)
  })

  it('ignores stale responses across session or tool changes', async () => {
    const previous = deferred<AttachmentTextPreview>()
    const read = vi.fn().mockReturnValueOnce(previous.promise).mockResolvedValueOnce(result('new body'))
    const { state, snapshot } = mount(read)
    await vi.waitFor(() => expect(read).toHaveBeenCalled())
    state.sessionKey = 'session-b'
    state.toolId = 'tool-two'
    await nextTick()
    await vi.waitFor(() => expect(snapshot.content.value).toBe('new body'))
    expect(read.mock.calls[0]![1].signal.aborted).toBe(true)
    previous.resolve(result('old body'))
    await nextTick()
    expect(snapshot.content.value).toBe('new body')
  })

  it('rejects incomplete bytes and retries without exposing the saved preview as complete', async () => {
    const read = vi.fn().mockResolvedValueOnce(result('short')).mockResolvedValueOnce(result('all body'))
    const { snapshot } = mount(read)
    await vi.waitFor(() => expect(snapshot.error.value).toBe(true))
    expect(snapshot.content.value).toBeNull()
    expect(await snapshot.ensureComplete()).toBe(false)
    expect(await snapshot.retry()).toBe(true)
    expect(snapshot.error.value).toBe(false)
    expect(snapshot.content.value).toBe('all body')
  })

  it('marks bounded content as a preview and never claims it is complete', async () => {
    const read = vi.fn().mockResolvedValue({ content: 'prefix', truncated: true, totalBytes: 8 })
    const { snapshot } = mount(read)
    expect(await snapshot.ensurePreview()).toBe(true)
    expect(snapshot.previewReady.value).toBe(true)
    expect(snapshot.truncated.value).toBe(true)
    expect(await snapshot.ensureComplete()).toBe(false)
    expect(snapshot.error.value).toBe(false)
  })

  it('limits concurrent reads to two and removes cancelled queued work', async () => {
    const response = deferred<AttachmentTextPreview>()
    const read = vi.fn().mockReturnValue(response.promise)
    const first = mount(read)
    const second = mount(read)
    const third = mount(read)
    const fourth = mount(read)
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    third.unmount()
    first.unmount()
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(3))
    response.resolve(result('all body'))
    expect(await second.snapshot.ensureComplete()).toBe(true)
    expect(await fourth.snapshot.ensureComplete()).toBe(true)
  })

  it('retains many small previews without reporting a capacity failure', async () => {
    const read = vi.fn().mockResolvedValue(result('all body'))
    const previews = Array.from({ length: 24 }, () => mount(read))
    expect(await Promise.all(previews.map(value => value.snapshot.ensurePreview()))).toEqual(Array(24).fill(true))
    expect(previews.every(value => !value.snapshot.error.value)).toBe(true)
    expect(read).toHaveBeenCalledTimes(24)
  })

  it('recovers a legacy snapshot only on expansion, using the exact saved message and tool identity', async () => {
    const read = vi.fn().mockResolvedValue(result('recovered historical body'))
    const { state, snapshot } = mount(read, false)
    state.snapshot = { kind: 'write', path: 'old.html', content: '', legacy_ref: {
      message_id: 'saved-message', tool_use_id: 'old-call', sha256: 'b'.repeat(64), original_chars: 25,
    } }
    await nextTick()
    expect(read).not.toHaveBeenCalled()
    state.enabled = true
    await nextTick()
    expect(await snapshot.ensureComplete()).toBe(true)
    expect(read).toHaveBeenCalledWith(expect.objectContaining({
      name: 'old.html', fileSnapshotRef: { messageId: 'saved-message', toolUseId: 'old-call' }, sha256_ref: 'b'.repeat(64),
    }), expect.objectContaining({ sessionKey: 'session-a', signal: expect.any(AbortSignal) }))
    expect(snapshot.content.value).toBe('recovered historical body')
    expect(snapshot.error.value).toBe(false)
  })

  it('applies the same bounded preview to recovered content whose original byte length is unknown', async () => {
    const read = vi.fn().mockResolvedValue(result('x'.repeat(20_000)))
    const { state, snapshot } = mount(read, false)
    state.snapshot = { kind: 'write', path: 'old.html', content: '', legacy_ref: {
      message_id: 'saved-message', tool_use_id: 'old-call', sha256: 'b'.repeat(64), original_chars: 20_000,
    } }
    state.enabled = true
    await nextTick()
    expect(await snapshot.ensurePreview()).toBe(true)
    expect(snapshot.content.value).toHaveLength(8192)
    expect(snapshot.truncated.value).toBe(true)
    expect(await snapshot.ensureComplete()).toBe(false)
  })

  it('fails a stalled preview, releases read and retention budgets, and permits retry', async () => {
    vi.useFakeTimers()
    const signals: AbortSignal[] = []
    const http = createPrivateHttpTransport({ baseUrl: 'http://localhost', authToken: () => '',
      fetch: vi.fn((_url, init) => new Promise<Response>((_resolve, reject) => {
        const signal = init!.signal!
        signals.push(signal)
        signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })) })
    const read = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>()
      .mockImplementationOnce((attachment, request) => readDisplayAttachmentPreview(http, attachment,
        { ...request, baseOrigin: 'http://localhost' }))
      .mockImplementationOnce((attachment, request) => readDisplayAttachmentPreview(http, attachment,
        { ...request, baseOrigin: 'http://localhost' }))
      .mockResolvedValue(result('all body'))
    const first = mount(read)
    const second = mount(read)
    const queued = mount(read)
    const outcomes = Promise.all([first.snapshot.ensurePreview(), second.snapshot.ensurePreview(), queued.snapshot.ensurePreview()])
    expect(read).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(14_999)
    expect(first.snapshot.loading.value).toBe(true)
    expect(first.snapshot.error.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(await outcomes).toEqual([false, false, true])
    expect(signals.every(signal => signal.aborted)).toBe(true)
    expect(first.snapshot.loading.value).toBe(false)
    expect(first.snapshot.error.value).toBe(true)
    expect(first.snapshot.content.value).toBeNull()
    // The two failed mounted rows must not consume the sixteen-body budget.
    const retained = Array.from({ length: 15 }, () => mount(read))
    expect(await Promise.all(retained.map(value => value.snapshot.ensurePreview()))).toEqual(Array(15).fill(true))
    queued.unmount()
    expect(await first.snapshot.retry()).toBe(true)
    expect(first.snapshot.error.value).toBe(false)
    expect(first.snapshot.content.value).toBe('all body')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('cancels the deadline on collapse without exposing a retry error', async () => {
    vi.useFakeTimers()
    let signal!: AbortSignal
    const http = createPrivateHttpTransport({ baseUrl: 'http://localhost', authToken: () => '',
      fetch: vi.fn((_url, init) => new Promise<Response>((_resolve, reject) => {
        signal = init!.signal!
        signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })) })
    const { state, snapshot } = mount((attachment, request) => readDisplayAttachmentPreview(http, attachment,
      { ...request, baseOrigin: 'http://localhost' }))
    const pending = snapshot.ensurePreview()
    state.enabled = false
    await nextTick()
    expect(await pending).toBe(false)
    expect(signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(snapshot.loading.value).toBe(false)
    expect(snapshot.error.value).toBe(false)
    expect(snapshot.content.value).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })
})
