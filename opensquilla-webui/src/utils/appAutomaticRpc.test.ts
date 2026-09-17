import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createV4SessionDirectory } from '@/adapters/gateway/sessionDirectoryV4'
import { useSessions } from '@/composables/useSessions'
import { createAppAutomaticRpc } from './appAutomaticRpc'

function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

const page = (title: string) => ({
  count: 1, ts: 1,
  sessions: [{ key: 'agent:main:webchat:one', title, updatedAt: 1 }],
})

function setup(initiallyAvailable = false) {
  const state = { available: initiallyAvailable, admitted: true }
  const ready = vi.fn((options?: { timeoutMs?: number }) => {
    if (state.available) return Promise.resolve()
    return new Promise<void>((_, reject) => {
      setTimeout(() => reject(new Error(`ready timed out after ${options?.timeoutMs}ms`)), options?.timeoutMs)
    })
  })
  const request = vi.fn().mockResolvedValue(page('Current'))
  const sessions = useSessions(createV4SessionDirectory({ ready, request }))
  const resumeDirectory = vi.fn().mockResolvedValue(undefined)
  const loadAgents = vi.fn().mockResolvedValue(undefined)
  const subscribeCron = vi.fn()
  const lifecycle = createAppAutomaticRpc({
    available: () => state.available,
    admitted: () => state.admitted,
    resumeDirectory,
    subscribeCron,
    loadAgents,
    loadSidebar: sessions.loadSessions,
    cancelSidebar: sessions.cancelPendingRequests,
  })
  return { state, ready, request, sessions, resumeDirectory, loadAgents, subscribeCron, lifecycle }
}

describe('App automatic RPC lifecycle with the real directory adapter', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('waits through slow startup, gates direct/event refreshes, and loads once on first readiness', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup()
    void app.lifecycle.load()
    await app.lifecycle.mount()
    app.lifecycle.schedule()
    void app.lifecycle.load()
    await vi.advanceTimersByTimeAsync(42_586)
    expect(app.ready).not.toHaveBeenCalled()
    expect(app.request).not.toHaveBeenCalled()
    expect(app.resumeDirectory).not.toHaveBeenCalled()
    expect(app.loadAgents).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()

    app.state.available = true
    await app.lifecycle.availabilityChanged()
    await vi.advanceTimersByTimeAsync(150)
    expect(app.ready).toHaveBeenCalledOnce()
    expect(app.request).toHaveBeenCalledExactlyOnceWith('sessions.list', {
      limit: 200, view: 'session-list-v1',
    }, expect.objectContaining({ timeoutMs: 10_000, timeoutAction: 'reject', abortAction: 'reject' }))
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Current')
    expect(app.loadAgents).toHaveBeenCalledOnce()
    expect(app.subscribeCron).toHaveBeenCalledOnce()
    expect(error).not.toHaveBeenCalled()
    app.lifecycle.dispose()
  })

  it('starts after readiness that arrived while chat bootstrap still held admission', async () => {
    const app = setup()
    app.state.admitted = false
    await app.lifecycle.mount()
    app.state.available = true
    await app.lifecycle.availabilityChanged()
    app.lifecycle.schedule()
    await app.lifecycle.load()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(app.ready).not.toHaveBeenCalled()
    app.state.admitted = true
    await app.lifecycle.admissionChanged()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledOnce()
    expect(app.loadAgents).toHaveBeenCalledOnce()
    app.lifecycle.dispose()
  })

  it('coalesces direct and scheduled refreshes while the first directory lease is still binding', async () => {
    const app = setup(true)
    const lease = deferred()
    app.resumeDirectory.mockReturnValueOnce(lease.promise)
    const mounting = app.lifecycle.mount()
    app.lifecycle.schedule()
    void app.lifecycle.load()
    await vi.advanceTimersByTimeAsync(500)
    expect(app.request).not.toHaveBeenCalled()
    expect(app.ready).not.toHaveBeenCalled()
    lease.resolve()
    await mounting
    await vi.advanceTimersByTimeAsync(150)
    expect(app.request).toHaveBeenCalledOnce()
    expect(app.loadAgents).toHaveBeenCalledOnce()
    app.lifecycle.dispose()
  })

  it('cannot start work after unmount, even when a directory lease resolves later', async () => {
    const app = setup(true)
    const lease = deferred()
    app.resumeDirectory.mockReturnValueOnce(lease.promise)
    const mounting = app.lifecycle.mount()
    app.lifecycle.dispose()
    lease.resolve()
    await mounting
    await app.lifecycle.load()
    app.lifecycle.schedule()
    await app.lifecycle.availabilityChanged()
    await app.lifecycle.admissionChanged()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(app.ready).not.toHaveBeenCalled()
    expect(app.request).not.toHaveBeenCalled()
    expect(app.loadAgents).not.toHaveBeenCalled()
    expect(app.resumeDirectory).toHaveBeenCalledOnce()
  })

  it('ignores an old connection lease and starts only the current generation', async () => {
    const app = setup(true)
    const oldLease = deferred()
    const currentLease = deferred()
    app.resumeDirectory.mockReturnValueOnce(oldLease.promise).mockReturnValueOnce(currentLease.promise)
    const oldMount = app.lifecycle.mount()
    app.state.available = false
    await app.lifecycle.availabilityChanged()
    app.state.available = true
    const reconnect = app.lifecycle.availabilityChanged()
    oldLease.resolve()
    await oldMount
    expect(app.request).not.toHaveBeenCalled()
    currentLease.resolve()
    await reconnect
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledOnce()
    expect(app.loadAgents).toHaveBeenCalledOnce()
    app.lifecycle.dispose()
  })

  it('invalidates a pending lease when admission closes and reopens', async () => {
    const app = setup(true)
    const oldLease = deferred()
    const currentLease = deferred()
    app.resumeDirectory.mockReturnValueOnce(oldLease.promise).mockReturnValueOnce(currentLease.promise)
    const mounting = app.lifecycle.mount()
    app.state.admitted = false
    await app.lifecycle.admissionChanged()
    app.state.admitted = true
    const resumed = app.lifecycle.admissionChanged()
    oldLease.resolve()
    await mounting
    expect(app.request).not.toHaveBeenCalled()
    currentLease.resolve()
    await resumed
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledOnce()
    app.lifecycle.dispose()
  })

  it('aborts an old directory request and never publishes its late result after reconnect', async () => {
    const app = setup(true)
    const oldRead = deferred<ReturnType<typeof page>>()
    app.request.mockReturnValueOnce(oldRead.promise).mockResolvedValueOnce(page('New connection'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    const signal = app.request.mock.calls[0]?.[2].signal as AbortSignal
    app.state.available = false
    await app.lifecycle.availabilityChanged()
    expect(signal.aborted).toBe(true)
    app.state.available = true
    await app.lifecycle.availabilityChanged()
    oldRead.resolve(page('Retired connection'))
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledTimes(2)
    expect(app.sessions.sessionsList.value.map(item => item.title)).toEqual(['New connection'])
    expect(app.loadAgents).toHaveBeenCalledOnce()
    app.lifecycle.dispose()
  })

  it('cancels the active read on unmount without publishing a late rejection', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    const read = deferred()
    app.request.mockReturnValueOnce(read.promise)
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    const signal = app.request.mock.calls[0]?.[2].signal as AbortSignal
    app.lifecycle.dispose()
    expect(signal.aborted).toBe(true)
    read.reject(new Error('Retired request'))
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value).toEqual([])
    expect(app.sessions.sessionListError.value).toBe(false)
    expect(error).not.toHaveBeenCalled()
  })

  it('keeps a real current-generation request failure visible', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    app.request.mockRejectedValueOnce(new Error('sessions.list failed'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionListError.value).toBe(true)
    expect(error).toHaveBeenCalledExactlyOnceWith('[useSessions] session directory error:', 'sessions.list failed')
    expect(app.request.mock.calls[0]?.[2].timeoutMs).toBe(10_000)
    app.lifecycle.dispose()
  })

  it('preserves explicit directory reads and their existing 10-second ready error', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup()
    const explicitRead = app.sessions.loadSessions()
    await vi.advanceTimersByTimeAsync(10_000)
    await explicitRead
    expect(app.request).not.toHaveBeenCalled()
    expect(app.sessions.sessionListError.value).toBe(true)
    expect(error).toHaveBeenCalledExactlyOnceWith('[useSessions] session directory error:', 'ready timed out after 10000ms')
    app.lifecycle.dispose()
  })
})
