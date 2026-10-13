import { afterEach, describe, expect, it, vi } from 'vitest'

import { createCoalescedRefresh } from './coalescedRefresh'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>(done => { resolve = done })
  return { promise, resolve }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('createCoalescedRefresh', () => {
  it('consumes a queued refresh when a failed load is retried immediately', async () => {
    vi.useFakeTimers()
    const run = vi.fn().mockRejectedValueOnce(new Error('failed')).mockResolvedValue(undefined)
    const refresh = createCoalescedRefresh({ run, allowed: () => true, delayMs: 150 })

    await expect(refresh.load()).rejects.toThrow('failed')
    refresh.schedule()
    await refresh.load()
    await vi.advanceTimersByTimeAsync(1_000)

    expect(run).toHaveBeenCalledTimes(2)
    refresh.dispose()
  })

  it('does not let an older queued timer request a repeat of a slow manual load', async () => {
    vi.useFakeTimers()
    const first = deferred()
    const run = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(undefined)
    const refresh = createCoalescedRefresh({ run, allowed: () => true, delayMs: 150 })

    refresh.schedule()
    const loading = refresh.load()
    await vi.advanceTimersByTimeAsync(500)
    first.resolve()
    await loading
    await vi.advanceTimersByTimeAsync(500)

    expect(run).toHaveBeenCalledOnce()
    refresh.dispose()
  })

  it('keeps new invalidations received after a manual load consumed the old queue', async () => {
    vi.useFakeTimers()
    const first = deferred()
    const run = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(undefined)
    const refresh = createCoalescedRefresh({ run, allowed: () => true, delayMs: 150 })

    refresh.schedule()
    const loading = refresh.load()
    refresh.schedule()
    refresh.schedule()
    await vi.advanceTimersByTimeAsync(150)
    expect(run).toHaveBeenCalledOnce()
    first.resolve()
    await loading
    await vi.advanceTimersByTimeAsync(1_000)

    expect(run).toHaveBeenCalledTimes(2)
    refresh.dispose()
  })

  it('refreshes within the first event window while invalidations keep arriving', async () => {
    vi.useFakeTimers()
    const run = vi.fn().mockResolvedValue(undefined)
    const refresh = createCoalescedRefresh({ run, allowed: () => true, delayMs: 150 })

    for (let event = 0; event < 100; event++) {
      refresh.schedule()
      await vi.advanceTimersByTimeAsync(100)
    }

    expect(run).toHaveBeenCalledTimes(50)
    refresh.dispose()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(run).toHaveBeenCalledTimes(50)
  })

  it('runs a new authoritative refresh when an event arrives during a load', async () => {
    vi.useFakeTimers()
    const first = deferred()
    const run = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(undefined)
    const refresh = createCoalescedRefresh({
      run,
      allowed: () => true,
      delayMs: 150,
    })

    const initial = refresh.load()
    refresh.schedule()
    await vi.advanceTimersByTimeAsync(150)
    expect(run).toHaveBeenCalledTimes(1)

    first.resolve()
    await initial
    await vi.waitFor(() => expect(run).toHaveBeenCalledTimes(2))
  })

  it('reruns when an authoritative load is requested during an older load', async () => {
    const first = deferred()
    const run = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce(undefined)
    const refresh = createCoalescedRefresh({
      run,
      allowed: () => true,
      delayMs: 150,
    })

    const initial = refresh.load()
    expect(refresh.load()).toBe(initial)
    first.resolve()
    await initial

    await vi.waitFor(() => expect(run).toHaveBeenCalledTimes(2))
  })

  it('does not rerun a pending authoritative load after disposal', async () => {
    const first = deferred()
    const run = vi.fn().mockReturnValue(first.promise)
    const refresh = createCoalescedRefresh({
      run,
      allowed: () => true,
      delayMs: 150,
    })

    const initial = refresh.load()
    void refresh.load()
    refresh.dispose()
    first.resolve()
    await initial
    await Promise.resolve()

    expect(run).toHaveBeenCalledOnce()
  })

  it('keeps a deferred refresh until admission resumes', async () => {
    let admitted = false
    const run = vi.fn().mockResolvedValue(undefined)
    const refresh = createCoalescedRefresh({
      run,
      allowed: () => admitted,
      delayMs: 150,
    })

    refresh.defer()
    refresh.flush()
    expect(run).not.toHaveBeenCalled()

    admitted = true
    refresh.flush()
    await vi.waitFor(() => expect(run).toHaveBeenCalledOnce())
  })
})
