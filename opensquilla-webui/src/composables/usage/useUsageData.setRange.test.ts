// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'

import { useUsageData } from './useUsageData'
import { requestUsageSnapshot } from './useUsageQuery'
import type { UsageSnapshot } from '@/types/usage'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({}),
}))

vi.mock('./useUsageQuery', () => ({
  requestUsageSnapshot: vi.fn(),
  naturalRangeStartMs: () => null,
}))

const RANGE_KEY = 'opensquilla-usage-range'

function snapshotFor(preset: string): UsageSnapshot {
  return {
    source: 'usage_ledger',
    mode: 'ledger_exact',
    asOfMs: 0,
    timezone: 'UTC',
    timezoneFallback: null,
    range: { preset, fromMs: null, toMs: null },
    totals: {
      input: 0,
      output: 0,
      cost: 0,
      cacheRead: 0,
      cacheWrite: 0,
      sessions: 0,
      totalTokens: 0,
      billedCost: 0,
      estimatedCost: 0,
      estimatedEventCount: 0,
      missingCostEntries: 0,
      eventCount: 0,
      costSource: 'none',
      costSourceCounts: {},
    },
    sessions: [],
    models: [],
    days: [],
    coverage: {
      status: 'complete',
      timeAttribution: 'complete',
      pricing: 'complete',
      exactFromMs: null,
      backfill: 'complete',
      reasonCodes: [],
      anomalyCount: 0,
      legacyIncludedInTotals: false,
      legacyTotals: null,
      nativeBilling: {
        status: 'unavailable',
        exactFromMs: null,
        reasonCodes: [],
        missingConfirmedReceiptCount: 0,
        pendingReceiptCount: 0,
      },
    },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function mountUsageData() {
  const scope = effectScope()
  const api = scope.run(() => useUsageData())!
  return { api, scope }
}

async function flushMicrotasks() {
  // Two ticks: one for the awaited request, one for the .then() callback.
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

let scopes: Array<ReturnType<typeof effectScope>> = []

beforeEach(() => {
  localStorage.setItem(RANGE_KEY, '7')
  vi.mocked(requestUsageSnapshot).mockReset()
})

afterEach(() => {
  scopes.forEach(scope => scope.stop())
  scopes = []
  localStorage.clear()
})

describe('useUsageData range selection under concurrent refreshes', () => {
  it('keeps the new range when a concurrent refresh supersedes the range-change load', async () => {
    const rangeLoad = deferred<UsageSnapshot>()
    const refreshLoad = deferred<UsageSnapshot>()
    vi.mocked(requestUsageSnapshot)
      .mockResolvedValueOnce(snapshotFor('last_7_calendar_days'))
      .mockReturnValueOnce(rangeLoad.promise)
      .mockReturnValueOnce(refreshLoad.promise)

    const { api, scope } = mountUsageData()
    scopes.push(scope)
    await api.loadData()

    api.setRange('30')
    expect(api.range.value).toBe('30')

    // A 60s auto-refresh (or visibilitychange) tick fires before the
    // range-change request settles; it fetches with the NEW range and
    // publishes its snapshot first.
    const refreshDone = api.loadData()
    refreshLoad.resolve(snapshotFor('last_30_calendar_days'))
    expect(await refreshDone).toBe(true)

    // The superseded range-change load settles afterwards. It must not roll
    // the selector back to 7d while 30-day data is on screen.
    rangeLoad.resolve(snapshotFor('last_30_calendar_days'))
    await flushMicrotasks()

    expect(api.range.value).toBe('30')
    expect(localStorage.getItem(RANGE_KEY)).toBe('30')
  })

  it('still reverts the selection when the range-change load itself fails', async () => {
    vi.mocked(requestUsageSnapshot)
      .mockResolvedValueOnce(snapshotFor('last_7_calendar_days'))
      .mockRejectedValueOnce(new Error('gateway unavailable'))

    const { api, scope } = mountUsageData()
    scopes.push(scope)
    await api.loadData()

    api.setRange('30')
    await flushMicrotasks()

    expect(api.range.value).toBe('7')
    expect(localStorage.getItem(RANGE_KEY)).toBe('7')
    // The cached 7d snapshot is still rendered, so no page-level error.
    expect(api.usageError.value).toBeNull()
  })
})
