// @vitest-environment happy-dom

import { createApp, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { OBSERVABILITY_KEY, type Observability } from '@/modules/observability'
import { usageSnapshot, usageTotals } from '@/testing/usage.test-helper'
import type { UsageSnapshot } from '@/types/usage'
import { useTodayUsage } from './useTodayUsage'

const mounted: Array<ReturnType<typeof createApp>> = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
})

function deferred() {
  let resolve!: (snapshot: UsageSnapshot) => void
  let reject!: (error: Error) => void
  const promise = new Promise<UsageSnapshot>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function mountUsage(usage?: Observability['usage']) {
  const open = ref(false)
  let api!: ReturnType<typeof useTodayUsage>
  const app = createApp({
    setup() { api = useTodayUsage(open); return () => null },
  })
  if (usage) app.provide(OBSERVABILITY_KEY, { usage } as Observability)
  const root = document.createElement('div')
  document.body.appendChild(root)
  app.mount(root)
  mounted.push(app)
  return { open, api, app }
}

describe('account menu local today usage', () => {
  it('does no startup work, then requests only today totals using the Usage page token math', async () => {
    const pending = deferred()
    const usage = vi.fn<Observability['usage']>().mockReturnValue(pending.promise)
    const { open, api } = mountUsage(usage)
    expect(usage).not.toHaveBeenCalled()
    expect(api.tokens.value).toBeNull()

    open.value = true
    expect(api.loading.value).toBe(true)
    expect(usage).toHaveBeenCalledExactlyOnceWith('today', {
      days: false, models: false, sessions: false, signal: expect.any(AbortSignal),
    })
    pending.resolve(usageSnapshot({ totals: usageTotals({ input: 120, output: 30, totalTokens: 999 }) }))
    await nextTick()
    expect(api.tokens.value).toBe(150)
    expect(api.loading.value).toBe(false)
    expect(api.error.value).toBe(false)
    expect(api.approximate.value).toBe(false)
  })

  it('aborts closing requests and ignores old responses after reopening', async () => {
    const first = deferred()
    const second = deferred()
    const usage = vi.fn<Observability['usage']>()
      .mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { open, api } = mountUsage(usage)
    open.value = true
    const signal = usage.mock.calls[0]![1]!.signal!
    open.value = false
    expect(signal.aborted).toBe(true)
    expect(api.loading.value).toBe(false)
    open.value = true

    first.resolve(usageSnapshot({ totals: usageTotals({ input: 999 }) }))
    await nextTick()
    expect(api.tokens.value).toBeNull()
    expect(api.loading.value).toBe(true)
    second.resolve(usageSnapshot({ totals: usageTotals({ input: 10, output: 2 }) }))
    await nextTick()
    expect(api.tokens.value).toBe(12)
    expect(api.loading.value).toBe(false)
  })

  it('does not reuse yesterday totals if a fresh opening fails', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T23:59:00Z'))
    const usage = vi.fn<Observability['usage']>()
      .mockResolvedValueOnce(usageSnapshot({ totals: usageTotals({ input: 500 }) }))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(usageSnapshot())
    const { open, api } = mountUsage(usage)
    open.value = true
    await nextTick()
    expect(api.tokens.value).toBe(500)
    open.value = false
    vi.setSystemTime(new Date('2026-01-02T00:01:00Z'))
    open.value = true
    expect(api.tokens.value).toBeNull()
    await nextTick()
    expect(usage.mock.calls[1]![1]).not.toHaveProperty('cachedSnapshot')
    expect(api.tokens.value).toBeNull()
    expect(api.error.value).toBe(true)

    open.value = false
    open.value = true
    await nextTick()
    expect(api.tokens.value).toBe(0)
    expect(api.error.value).toBe(false)
  })

  it.each(['ledger_partial', 'session_approximation'] as const)('marks %s totals as approximate', async mode => {
    const { open, api } = mountUsage(vi.fn().mockResolvedValue(usageSnapshot({ mode })))
    open.value = true
    await nextTick()
    expect(api.approximate.value).toBe(true)
  })

  it('refreshes an open menu at local midnight and stops the day timer on close', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 0, 1, 23, 59, 59, 900))
    const nextDay = deferred()
    const usage = vi.fn<Observability['usage']>()
      .mockResolvedValueOnce(usageSnapshot({ totals: usageTotals({ input: 500 }) }))
      .mockReturnValueOnce(nextDay.promise)
    const { open, api } = mountUsage(usage)
    open.value = true
    await nextTick()
    expect(api.tokens.value).toBe(500)

    await vi.advanceTimersByTimeAsync(100)
    expect(usage).toHaveBeenCalledTimes(2)
    expect(api.tokens.value).toBeNull()
    expect(api.loading.value).toBe(true)
    nextDay.resolve(usageSnapshot({ totals: usageTotals({ input: 3, output: 2 }) }))
    await nextTick()
    expect(api.tokens.value).toBe(5)

    open.value = false
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000)
    expect(usage).toHaveBeenCalledTimes(2)
  })

  it('leaves usage unavailable when the gateway capability is not provided', () => {
    const { open, api } = mountUsage()
    expect(api.error.value).toBe(false)
    open.value = true
    expect(api.tokens.value).toBeNull()
    expect(api.loading.value).toBe(false)
    expect(api.error.value).toBe(true)
  })

  it('aborts pending usage when its owner is removed', async () => {
    const pending = deferred()
    const usage = vi.fn<Observability['usage']>().mockReturnValue(pending.promise)
    const { open, api, app } = mountUsage(usage)
    open.value = true
    app.unmount()
    mounted.splice(mounted.indexOf(app), 1)
    expect(usage.mock.calls[0]![1]!.signal!.aborted).toBe(true)
    pending.reject(new Error('aborted'))
    await nextTick()
    expect(api.error.value).toBe(false)
  })
})
