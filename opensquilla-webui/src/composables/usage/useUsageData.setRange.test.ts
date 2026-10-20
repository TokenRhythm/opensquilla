// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'

import i18n, { loadLocaleMessages, type LocaleCode } from '@/i18n'
import { useUsageData } from './useUsageData'
import { requestUsageSnapshot } from './useUsageQuery'
import type { UsageSnapshot } from '@/types/usage'
import { usageSession, usageTotals } from '@/testing/usage.test-helper'
import type { SessionDirectory } from '@/modules/sessionDirectory'
import type { Observability } from '@/modules/observability'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
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
  const directory: SessionDirectory = {
    listPage: vi.fn().mockResolvedValue({ items: [], hasMore: false, nextCursor: null }),
    count: vi.fn().mockResolvedValue({ value: 0, exact: true }),
    resolve: vi.fn().mockResolvedValue({ key: 'agent:main:webchat:default', id: 'default' }),
    search: vi.fn().mockResolvedValue({ sessions: [], messages: [] }),
  }
  const api = scope.run(() => useUsageData(directory, {} as Observability))!
  return { api, directory, scope }
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
  it('renders parent rows and counts without adding the raw child projection or changing server money', async () => {
    const { api, scope } = mountUsageData()
    scopes.push(scope)
    const totals = usageTotals({ cost: 0.7, eventCount: 5, sessions: 4, taskCount: 1 })
    const snapshot = { ...snapshotFor('all'), totals,
      sessions: [usageSession({ sessionId: 'child', title: 'Assistant chat', costUsd: 0.3 })],
      taskGroups: [{ taskGroupId: 'task', rootSessionId: 'root', rootSessionEpoch: 1,
        sessionKey: 'root-key', title: 'Renamed parent', totals, attributionStatus: 'complete',
        firstUsageAtMs: 100, lastUsageAtMs: 200,
        modelBreakdown: [{ provider: 'tokenrhythm', model: 'image', costUsd: 0.7,
          capability: 'image_generation' }],
        branches: { self: totals, inlineSubagents: usageTotals(), subsessions: usageTotals(),
          sideChats: usageTotals(), unattributed: usageTotals() }, runs: [] }] }
    vi.mocked(requestUsageSnapshot).mockResolvedValue(snapshot)
    await api.loadData()
    expect(api.sortedRows.value).toHaveLength(1)
    expect(api.sortedRows.value[0]).toMatchObject({ sessionKey: 'root-key', rowIdentity: 'task:task',
      sessionLabel: 'Renamed parent', cost: 0.7, hasModelBreakdown: true })
    expect(api.usageTotals.value.cost).toBe(0.7)
    expect(api.sessionCountDisplay.value).toBe('1')
    expect(api.avgCostDisplay.value).toBe('$0.7000')
    expect(api.tableColumns.value.map(column => column.key)).toEqual([
      'session', 'updated_at', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cost_usd', 'model',
    ])

    vi.mocked(requestUsageSnapshot).mockResolvedValue({ ...snapshot, taskGroups: [],
      totals: { ...totals, taskCount: 0 } })
    await api.loadData()
    expect(api.sortedRows.value).toEqual([])
    expect(api.sessionCountDisplay.value).toBe('0')
    expect(api.usageTotals.value.cost).toBe(0.7)
  })

  it('keeps provider identity, meters and unknown money in expanded session rows', () => {
    const { api, scope } = mountUsageData()
    scopes.push(scope)
    const rows = api.rowBreakdown(usageSession({ modelBreakdown: [
      { provider: 'elevenlabs', model: 'eleven_flash_v2_5', capability: 'text_to_speech',
        missingCostEntries: 1, costSource: 'none', costUsd: 0, tokenUnknownItemCount: 1,
        meters: [{ metric: 'character_credits', unit: 'credit', quantity: '2', dimensions: {}, unknownCount: 0, measurementCount: 1 }] },
      { model: 'provider/text-model', costUsd: 0.008 },
    ] }))
    expect(rows[0]).toMatchObject({ provider: 'elevenlabs', name: 'eleven_flash_v2_5',
      capability: 'text_to_speech', missingCostEntries: 1, tokenUnknownItemCount: 1,
      meters: [{ quantity: '2', unit: 'credit' }] })
    expect(rows[1]).toMatchObject({ provider: 'provider', name: 'text-model', cost: 0.008 })
  })

  it.each([
    { name: 'empty legacy totals', legacy: usageTotals(), included: true, shown: false },
    { name: 'absent legacy totals', legacy: null, included: true, shown: false },
    { name: 'excluded legacy usage', legacy: usageTotals({ cost: 1 }), included: false, shown: false },
    { name: 'legacy money', legacy: usageTotals({ cost: 1 }), included: true, shown: true },
    { name: 'legacy tokens', legacy: usageTotals({ input: 1 }), included: true, shown: true },
    { name: 'unpriced legacy usage', legacy: usageTotals({ missingCostEntries: 1 }), included: true, shown: true },
    { name: 'zero-cost legacy calls', legacy: usageTotals({ eventCount: 1 }), included: true, shown: true },
  ])('shows the pre-ledger notice only for an included residual: $name', async ({ legacy, included, shown }) => {
    const snapshot = snapshotFor('all')
    snapshot.coverage.legacyIncludedInTotals = included
    snapshot.coverage.legacyTotals = legacy
    vi.mocked(requestUsageSnapshot).mockResolvedValueOnce(snapshot)
    const { api, scope } = mountUsageData()
    scopes.push(scope)
    await api.loadData()
    expect(api.rangeHiddenHint.value.includes('Pre-ledger usage')).toBe(shown)
  })

  it('retains the missing-price notice for calls without receipts', async () => {
    const snapshot = snapshotFor('all')
    snapshot.totals = usageTotals({
      eventCount: 11, missingCostEntries: 6,
      costSourceCounts: { provider_billed: 4, opensquilla_estimate: 1, unavailable: 5, none: 1 },
    })
    snapshot.sessions = [usageSession({ sessionId: 'bocha-403-owner', costSource: 'none' })]
    vi.mocked(requestUsageSnapshot).mockResolvedValueOnce(snapshot)
    const { api, scope } = mountUsageData()
    scopes.push(scope)
    await api.loadData()

    expect(api.rangeHiddenHint.value).toContain('6 calls are unpriced')
    expect(api.sessionsMeta.value).toBe('1 tasks')
  })

  it('delegates task-title connection ownership to SessionDirectory', async () => {
    vi.mocked(requestUsageSnapshot).mockResolvedValueOnce(snapshotFor('last_7_calendar_days'))
    const { api, directory, scope } = mountUsageData()
    scopes.push(scope)

    await api.loadData()

    expect(directory.listPage).toHaveBeenCalledWith({ limit: 200 })
  })

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

      const label = api.modelDisplayLabel(usageSession({
        modelBreakdown: [
          { model: 'provider/primary-model' },
          { model: 'provider/helper-model' },
        ],
      }))

      expect(label).toBe(expected)
      expect(label).not.toMatch(/auto|自动|自動/i)
    },
  )

  it('keeps the model name for a single-model breakdown', () => {
    const { api, scope } = mountUsageData()
    scopes.push(scope)

    expect(api.modelDisplayLabel(usageSession({
      modelBreakdown: [{ model: 'provider/only-model' }],
    }))).toBe('provider/only-model')
  })
})
