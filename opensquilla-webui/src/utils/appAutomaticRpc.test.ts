import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createV4SessionDirectory } from '@/adapters/gateway/sessionDirectoryV4'
import { createV4SessionDirectoryChanges } from '@/adapters/gateway/sessionDirectoryChangesV4'
import { useSessions } from '@/composables/useSessions'
import { createAppAutomaticRpc } from './appAutomaticRpc'

function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

const page = (title: string, runStatus = 'idle') => ({
  count: 1, ts: 1,
  sessions: [{ key: 'agent:main:webchat:one', title, updatedAt: 1, runStatus }],
})

const titleReadBusy = () => Object.assign(new Error('Session storage is busy'), {
  code: 'STORAGE_BUSY', retryable: true, retry_after_ms: 100, accepted: false,
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

function directoryBinding(app: ReturnType<typeof setup>, code = 'UNAVAILABLE') {
  const error = Object.assign(new Error('Directory subscription rejected'), {
    code, retryable: code === 'UNAVAILABLE', accepted: false,
  })
  const request = vi.fn().mockRejectedValue(error)
  const changes = createV4SessionDirectoryChanges({
    generation: 1, request, ready: async () => {},
  }, { subscribe: () => ({ close() {} }) }, { warn: vi.fn() })
  changes.subscribe(() => app.lifecycle.schedule())
  app.resumeDirectory.mockImplementation(() => changes.resume())
  return { request, changes }
}

describe('App automatic RPC lifecycle with the real directory adapter', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('keeps the initial snapshot available after rejected subscription and refreshes on successful retry', async () => {
    const app = setup(true)
    const binding = directoryBinding(app)
    app.request.mockResolvedValue(page('Task', 'running'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('running')
    expect(binding.request).toHaveBeenCalledOnce()

    binding.request.mockResolvedValue(undefined)
    app.request.mockResolvedValue(page('Task', 'idle'))
    await vi.advanceTimersByTimeAsync(499)
    expect(binding.request).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(binding.request).toHaveBeenCalledTimes(2)
    expect(app.request).toHaveBeenCalledTimes(2)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('idle')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(binding.request).toHaveBeenCalledTimes(2)
    app.lifecycle.foreground()
    await vi.advanceTimersByTimeAsync(150)
    expect(binding.request).toHaveBeenCalledTimes(2)
    expect(app.request).toHaveBeenCalledTimes(3)
    app.lifecycle.dispose()
    binding.changes.dispose()
  })

  it('bounds subscription retries and retries the lease on foreground after other reads recover', async () => {
    const app = setup(true)
    const binding = directoryBinding(app)
    app.request.mockResolvedValue(page('Task', 'running'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(binding.request).toHaveBeenCalledTimes(4)
    expect(app.request).toHaveBeenCalledOnce()
    app.request.mockResolvedValue(page('Task', 'idle'))
    await app.lifecycle.load()
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('idle')
    expect(binding.request).toHaveBeenCalledTimes(4)

    binding.request.mockResolvedValue(undefined)
    app.lifecycle.foreground()
    app.lifecycle.foreground()
    await vi.advanceTimersByTimeAsync(150)
    expect(binding.request).toHaveBeenCalledTimes(5)
    expect(app.request).toHaveBeenCalledTimes(3)
    app.lifecycle.dispose()
    binding.changes.dispose()
  })

  it('refreshes after an older snapshot completes if rebinding succeeded during its read', async () => {
    const app = setup(true)
    const binding = directoryBinding(app)
    const oldRead = deferred<ReturnType<typeof page>>()
    app.request.mockReturnValueOnce(oldRead.promise).mockResolvedValue(page('Task', 'idle'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    binding.request.mockResolvedValue(undefined)
    await vi.advanceTimersByTimeAsync(500)
    expect(binding.request).toHaveBeenCalledTimes(2)
    expect(app.request).toHaveBeenCalledOnce()
    oldRead.resolve(page('Task', 'running'))
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledTimes(2)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('idle')
    app.lifecycle.dispose()
    binding.changes.dispose()
  })

  it.each(['UNAUTHORIZED', 'FORBIDDEN', 'METHOD_NOT_FOUND', 'UNSUPPORTED'])(
    'keeps snapshot reads available without retrying permanent subscription failure %s', async code => {
      const app = setup(true)
      const binding = directoryBinding(app, code)
      await app.lifecycle.mount()
      await vi.advanceTimersByTimeAsync(60_000)
      expect(binding.request).toHaveBeenCalledOnce()
      expect(app.request).toHaveBeenCalledOnce()
      app.lifecycle.foreground()
      await vi.advanceTimersByTimeAsync(150)
      expect(binding.request).toHaveBeenCalledOnce()
      expect(app.request).toHaveBeenCalledTimes(2)
      app.lifecycle.dispose()
      binding.changes.dispose()
    },
  )

  it('retires subscription retries while admission is closed and after disposal', async () => {
    const app = setup(true)
    app.resumeDirectory.mockRejectedValue(new Error('Transient subscription failure'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledOnce()
    app.state.admitted = false
    await app.lifecycle.admissionChanged()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.resumeDirectory).toHaveBeenCalledOnce()
    app.state.admitted = true
    await app.lifecycle.admissionChanged()
    expect(app.resumeDirectory).toHaveBeenCalledTimes(2)
    app.lifecycle.dispose()
    app.lifecycle.foreground()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.resumeDirectory).toHaveBeenCalledTimes(2)
  })

  it('ignores a subscription rejection after a replacement connection has bound', async () => {
    const app = setup(true)
    const old = deferred()
    app.resumeDirectory.mockReturnValueOnce(old.promise)
    const mounting = app.lifecycle.mount()
    app.state.available = false
    await app.lifecycle.availabilityChanged()
    app.state.available = true
    await app.lifecycle.availabilityChanged()
    old.reject(new Error('Old connection rejection'))
    await mounting
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.resumeDirectory).toHaveBeenCalledTimes(2)
    expect(app.request).toHaveBeenCalledOnce()
    app.lifecycle.dispose()
  })

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

  it('retries a failed terminal refresh and replaces the stale running row without another event', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    app.request.mockResolvedValueOnce(page('Active task', 'running'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    app.request.mockRejectedValueOnce(new Error('Temporary read failure'))
      .mockResolvedValueOnce(page('Stopped task', 'killed'))
    app.lifecycle.schedule()
    await vi.advanceTimersByTimeAsync(150)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('running')
    expect(app.sessions.sessionListError.value).toBe(true)
    await vi.advanceTimersByTimeAsync(499)
    expect(app.request).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(app.request).toHaveBeenCalledTimes(3)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('cancelled')
    expect(app.sessions.sessionListError.value).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.request).toHaveBeenCalledTimes(3)
    app.lifecycle.dispose()
  })

  it('recovers cold-start title storage contention without publishing fallback names or waiting for another event', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    app.request.mockRejectedValueOnce(titleReadBusy())
      .mockResolvedValueOnce(page('First user message title'))

    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.sessions.sessionsList.value).toEqual([])
    expect(app.sessions.sessionListError.value).toBe(true)
    expect(app.sessions.isLoading.value).toBe(false)
    await vi.advanceTimersByTimeAsync(499)
    expect(app.request).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(app.request).toHaveBeenCalledTimes(2)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('First user message title')
    expect(app.sessions.sessionListError.value).toBe(false)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(app.request).toHaveBeenCalledTimes(2)
    app.lifecycle.dispose()
  })

  it.each(['First user message title', 'My explicit session name'])(
    'retains the known title %s through storage contention and accepts the recovered authoritative name',
    async title => {
      vi.spyOn(console, 'error').mockImplementation(() => {})
      const app = setup(true)
      app.request.mockResolvedValueOnce(page(title))
      await app.lifecycle.mount()
      await vi.advanceTimersByTimeAsync(0)
      const previous = app.sessions.sessionsList.value
      app.request.mockRejectedValueOnce(titleReadBusy())
        // Even a deliberate rename to the generic-looking name is authoritative.
        .mockResolvedValueOnce(page('WebChat'))
      app.lifecycle.schedule()
      await vi.advanceTimersByTimeAsync(150)
      expect(app.sessions.sessionsList.value).toBe(previous)
      expect(app.sessions.sessionsList.value[0]?.title).toBe(title)
      expect(app.sessions.sessionListError.value).toBe(true)
      await vi.advanceTimersByTimeAsync(500)
      expect(app.sessions.sessionsList.value[0]?.title).toBe('WebChat')
      expect(app.sessions.sessionListError.value).toBe(false)
      expect(app.request).toHaveBeenCalledTimes(3)
      app.lifecycle.dispose()
    },
  )

  it('retains every loaded page when later title enrichment fails, then atomically applies renames and deletions on retry', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    const first = { ...page('Known first page'), hasMore: true, nextCursor: 'old-second' }
    const second = { count: 1, ts: 1, hasMore: false, nextCursor: null, sessions: [
      { key: 'agent:main:webchat:deleted', title: 'Known second page', updatedAt: 1 },
    ] }
    app.request.mockResolvedValueOnce(first).mockResolvedValueOnce(second)
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    await app.sessions.loadMoreSessions()
    const complete = app.sessions.sessionsList.value
    expect(complete).toHaveLength(2)

    app.request.mockResolvedValueOnce({ ...page('Partial newer name'), hasMore: true, nextCursor: 'new-second' })
      .mockRejectedValueOnce(titleReadBusy())
      .mockResolvedValueOnce(page('Confirmed rename'))
    app.lifecycle.schedule()
    await vi.advanceTimersByTimeAsync(150)
    expect(app.request).toHaveBeenCalledTimes(4)
    expect(app.sessions.sessionsList.value).toBe(complete)
    expect(app.sessions.sessionsList.value.map(row => row.title)).toEqual(['Known first page', 'Known second page'])
    expect(app.sessions.hasMore.value).toBe(false)
    expect(app.sessions.sessionListError.value).toBe(true)

    await vi.advanceTimersByTimeAsync(500)
    expect(app.request).toHaveBeenCalledTimes(5)
    expect(app.request).toHaveBeenLastCalledWith('sessions.list', {
      limit: 200, view: 'session-list-v1',
    }, expect.objectContaining({ timeoutAction: 'reject' }))
    expect(app.sessions.sessionsList.value.map(({ key, title }) => ({ key, title }))).toEqual([
      { key: 'agent:main:webchat:one', title: 'Confirmed rename' },
    ])
    expect(app.sessions.sessionListError.value).toBe(false)
    app.lifecycle.dispose()
  })

  it('bounds cold-start title failure retries and permits the existing explicit retry to recover', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    app.request.mockRejectedValue(titleReadBusy())
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(app.request).toHaveBeenCalledTimes(4)
    expect(app.sessions.sessionsList.value).toEqual([])
    expect(app.sessions.sessionListError.value).toBe(true)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(app.request).toHaveBeenCalledTimes(4)

    app.request.mockResolvedValue(page('Recovered by existing retry'))
    await app.lifecycle.load()
    expect(app.request).toHaveBeenCalledTimes(5)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Recovered by existing retry')
    expect(app.sessions.sessionListError.value).toBe(false)
    app.lifecycle.dispose()
  })

  it('bounds retries, retains the useful directory, and permits a new foreground attempt', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    app.request.mockRejectedValue(new Error('Still offline'))
    app.lifecycle.schedule()
    await vi.advanceTimersByTimeAsync(30_000)
    // Initial successful read, one invalidation read, and three retries.
    expect(app.request).toHaveBeenCalledTimes(5)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Current')
    expect(app.sessions.sessionListError.value).toBe(true)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.request).toHaveBeenCalledTimes(5)
    app.request.mockResolvedValue(page('Recovered'))
    app.lifecycle.foreground()
    app.lifecycle.foreground()
    await vi.advanceTimersByTimeAsync(150)
    expect(app.request).toHaveBeenCalledTimes(6)
    expect(app.sessions.sessionsList.value[0]?.title).toBe('Recovered')
    expect(app.sessions.sessionListError.value).toBe(false)
    app.lifecycle.dispose()
  })

  it('refreshes a stale running row when transport health recovers', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    app.request.mockResolvedValueOnce(page('Active task', 'running'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    app.request.mockRejectedValue(new Error('Transport suspect'))
    app.lifecycle.schedule()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.request).toHaveBeenCalledTimes(5)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('running')

    app.request.mockResolvedValue(page('Stopped task', 'killed'))
    app.lifecycle.connectionHealthChanged('suspect')
    await vi.advanceTimersByTimeAsync(150)
    expect(app.request).toHaveBeenCalledTimes(5)
    app.lifecycle.connectionHealthChanged('healthy')
    await vi.advanceTimersByTimeAsync(150)

    expect(app.request).toHaveBeenCalledTimes(6)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('cancelled')
    expect(app.sessions.sessionListError.value).toBe(false)
    app.lifecycle.dispose()
  })

  it('recovers a missed invalidation on foreground while respecting chat admission', async () => {
    const app = setup(true)
    app.request.mockResolvedValueOnce(page('Active task', 'running'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    app.state.admitted = false
    await app.lifecycle.admissionChanged()
    app.request.mockResolvedValue(page('Stopped task', 'killed'))
    app.lifecycle.foreground()
    app.lifecycle.foreground()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(app.request).toHaveBeenCalledOnce()
    app.state.admitted = true
    await app.lifecycle.admissionChanged()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledTimes(2)
    expect(app.sessions.sessionsList.value[0]?.runStatus).toBe('cancelled')
    app.lifecycle.dispose()
  })

  it('retires retry timers across disconnect and disposal', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const app = setup(true)
    app.request.mockRejectedValue(new Error('Read failed'))
    await app.lifecycle.mount()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledOnce()
    app.state.available = false
    await app.lifecycle.availabilityChanged()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.request).toHaveBeenCalledOnce()
    app.state.available = true
    await app.lifecycle.availabilityChanged()
    await vi.advanceTimersByTimeAsync(0)
    expect(app.request).toHaveBeenCalledTimes(2)
    app.lifecycle.dispose()
    app.lifecycle.foreground()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(app.request).toHaveBeenCalledTimes(2)
  })
})
