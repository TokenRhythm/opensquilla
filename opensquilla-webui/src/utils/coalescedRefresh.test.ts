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
