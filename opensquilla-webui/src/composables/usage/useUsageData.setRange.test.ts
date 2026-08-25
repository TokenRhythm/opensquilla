// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'

import i18n, { loadLocaleMessages, type LocaleCode } from '@/i18n'
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
  i18n.global.locale.value = 'en'
  localStorage.setItem(RANGE_KEY, '7')
  vi.mocked(requestUsageSnapshot).mockReset()
})

afterEach(() => {
  scopes.forEach(scope => scope.stop())
  scopes = []
  i18n.global.locale.value = 'en'
  localStorage.clear()
})

describe('useUsageData range selection under concurrent refreshes', () => {
  it('does not describe complete all-time task totals as a date-range approximation', async () => {
    localStorage.setItem(RANGE_KEY, 'all')
    const snapshot = snapshotFor('all')
    snapshot.source = 'usage_status'
    snapshot.mode = 'session_approximation'
    vi.mocked(requestUsageSnapshot).mockResolvedValueOnce(snapshot)

    const { api, scope } = mountUsageData()
    scopes.push(scope)
    await api.loadData()

    expect(api.range.value).toBe('all')
    expect(api.rangeHiddenHint.value).toBe('')
  })

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

describe('useUsageData model labels', () => {
  const localizedModelCounts: ReadonlyArray<{
    locale: LocaleCode
    expected: string
  }> = [
    { locale: 'en', expected: '2 models' },
    { locale: 'zh-Hans', expected: '2 个模型' },
    { locale: 'ja', expected: '2 モデル' },
    { locale: 'fr', expected: '2 modèles' },
    { locale: 'de', expected: '2 Modelle' },
    { locale: 'es', expected: '2 modelos' },
  ]

  it.each(localizedModelCounts)(
    'describes multiple models neutrally in $locale',
    async ({ locale, expected }) => {
      await loadLocaleMessages(locale)
      i18n.global.locale.value = locale
      const { api, scope } = mountUsageData()
      scopes.push(scope)

      const label = api.modelDisplayLabel({
        modelBreakdown: [
          { model: 'provider/primary-model' },
          { model: 'provider/helper-model' },
        ],
      })

      expect(label).toBe(expected)
      expect(label).not.toMatch(/auto|自动|自動/i)
    },
  )

  it('keeps the model name for a single-model breakdown', () => {
    const { api, scope } = mountUsageData()
    scopes.push(scope)

    expect(api.modelDisplayLabel({
      modelBreakdown: [{ model: 'provider/only-model' }],
    })).toBe('provider/only-model')
  })
})
